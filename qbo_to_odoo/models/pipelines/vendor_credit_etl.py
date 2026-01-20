"""QuickBooks Online VendorCredit ETL Pipeline

This module handles the migration of VendorCredits from QBO to Odoo
using the ETL framework. VendorCredits become in_refund account.move records.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, RETRYABLE_ERRORS

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.vendor.credit.importer",
    sap_source="VendorCredit",
    depends_on=["qbo.vendor.importer", "qbo.item.importer", "qbo.tax.importer"],
)
class QboVendorCreditImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO VendorCredits."""

    _name = "qbo.vendor.credit.importer"
    _description = "QBO Vendor Credit Importer"

    @ETL.extract("VendorCredit")
    def extract_vendor_credits(self, ctx: ETLContext) -> List[Dict]:
        """Extract vendor credits from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO vendor credit IDs
        ctx.env.cr.execute(
            "SELECT qbo_vendor_credit_id FROM account_move WHERE qbo_vendor_credit_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing vendor credits in Odoo")

        # Fetch all vendor credits from QBO
        vendor_credits = api_client.query_all(entity="VendorCredit", order_by="Id")

        # Filter out already imported
        new_vendor_credits = [
            vc for vc in vendor_credits if str(vc.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(vendor_credits)} vendor credits from QBO, "
            f"{len(new_vendor_credits)} are new"
        )
        return new_vendor_credits

    @ETL.transform()
    def transform_vendor_credits(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO vendor credits into Odoo account.move values."""
        vendor_credits = extracted.get("extract_vendor_credits", [])

        # Build lookups
        ctx.env.cr.execute(
            "SELECT qbo_vendor_id, id FROM res_partner WHERE qbo_vendor_id IS NOT NULL"
        )
        vendor_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_item_id, id FROM product_product WHERE qbo_item_id IS NOT NULL"
        )
        product_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_id, id FROM account_tax WHERE qbo_tax_id IS NOT NULL AND type_tax_use = 'purchase'"
        )
        tax_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_rate_id, id FROM account_tax WHERE qbo_tax_rate_id IS NOT NULL AND type_tax_use = 'purchase'"
        )
        tax_rate_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        # Find purchase journal
        journal = ctx.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not journal:
            raise ValueError("No purchase journal found for company")

        move_vals = []
        skipped = 0

        for vc in vendor_credits:
            # Get vendor
            vendor_ref = vc.get("VendorRef", {})
            qbo_vendor_id = int(vendor_ref.get("value", 0))
            partner_id = vendor_map.get(qbo_vendor_id)

            if not partner_id:
                _logger.warning(
                    f"Vendor not found for QBO ID {qbo_vendor_id} "
                    f"in vendor credit {vc.get('Id')}"
                )
                skipped += 1
                continue

            # Parse dates
            txn_date = vc.get("TxnDate")

            invoice_date = None
            if txn_date:
                try:
                    invoice_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                except ValueError:
                    invoice_date = datetime.now().date()

            # Get currency
            currency_id = company.currency_id.id
            currency_ref = vc.get("CurrencyRef", {})
            if currency_ref:
                currency_code = currency_ref.get("value")
                if currency_code:
                    currency = ctx.env["res.currency"].search(
                        [("name", "=", currency_code)], limit=1
                    )
                    if currency:
                        currency_id = currency.id

            # Build vendor credit lines
            line_vals = []
            for line in vc.get("Line", []):
                line_data = self._transform_vendor_credit_line(
                    line, product_map, account_map, tax_map, tax_rate_map, vc, ctx
                )
                if line_data:
                    line_vals.append((0, 0, line_data))

            if not line_vals:
                _logger.warning(f"No valid lines for vendor credit {vc.get('Id')}")
                skipped += 1
                continue

            vals = {
                "move_type": "in_refund",
                "journal_id": journal.id,
                "partner_id": partner_id,
                "invoice_date": invoice_date,
                "currency_id": currency_id,
                "ref": vc.get("DocNumber", ""),
                "narration": vc.get("Memo", ""),
                "invoice_line_ids": line_vals,
                "qbo_vendor_credit_id": int(vc.get("Id", 0)),
            }

            move_vals.append(vals)

        _logger.info(f"Transformed {len(move_vals)} vendor credits, skipped {skipped}")
        return move_vals

    def _transform_vendor_credit_line(
        self,
        line: Dict,
        product_map: Dict,
        account_map: Dict,
        tax_map: Dict,
        tax_rate_map: Dict,
        vendor_credit: Dict,
        ctx: ETLContext,
    ) -> Optional[Dict]:
        """Transform a single vendor credit line."""
        detail_type = line.get("DetailType", "")

        # Handle item-based expense lines
        if detail_type == "ItemBasedExpenseLineDetail":
            detail = line.get("ItemBasedExpenseLineDetail", {})
            if not detail:
                return None

            # Get product
            item_ref = detail.get("ItemRef", {})
            item_value = item_ref.get("value", "0") if item_ref else "0"
            try:
                qbo_item_id = int(item_value)
            except ValueError:
                qbo_item_id = 0
            product_id = product_map.get(qbo_item_id)

            # Get quantity and price
            qty = float(detail.get("Qty", 1) or 1)
            unit_price = float(detail.get("UnitPrice", 0) or 0)
            amount = float(line.get("Amount", 0) or 0)

            if not unit_price and amount and qty:
                unit_price = amount / qty

            # Vendor credits should have positive amounts in Odoo
            unit_price = abs(unit_price)

            # Get tax from TaxCodeRef
            tax_ids = []
            tax_code_ref = detail.get("TaxCodeRef", {})
            if tax_code_ref:
                tax_code_value = tax_code_ref.get("value")
                if tax_code_value and tax_code_value != "NON":
                    tax_id = tax_map.get(tax_code_value) or tax_rate_map.get(
                        tax_code_value
                    )
                    if tax_id:
                        tax_ids.append(tax_id)

            line_vals = {
                "name": line.get("Description", "") or item_ref.get("name", "") or "/",
                "quantity": qty,
                "price_unit": unit_price,
            }

            if product_id:
                line_vals["product_id"] = product_id
            if tax_ids:
                line_vals["tax_ids"] = [(6, 0, tax_ids)]

            return line_vals

        # Handle account-based expense lines
        elif detail_type == "AccountBasedExpenseLineDetail":
            detail = line.get("AccountBasedExpenseLineDetail", {})
            if not detail:
                return None

            # Get account
            account_ref = detail.get("AccountRef", {})
            qbo_account_id = int(account_ref.get("value", 0)) if account_ref else 0
            account_id = account_map.get(qbo_account_id)

            amount = float(line.get("Amount", 0) or 0)
            # Vendor credits should have positive amounts in Odoo
            amount = abs(amount)

            # Get tax from TaxCodeRef
            tax_ids = []
            tax_code_ref = detail.get("TaxCodeRef", {})
            if tax_code_ref:
                tax_code_value = tax_code_ref.get("value")
                if tax_code_value and tax_code_value != "NON":
                    tax_id = tax_map.get(tax_code_value) or tax_rate_map.get(
                        tax_code_value
                    )
                    if tax_id:
                        tax_ids.append(tax_id)

            line_vals = {
                "name": line.get("Description", "")
                or account_ref.get("name", "")
                or "/",
                "quantity": 1,
                "price_unit": amount,
            }

            if account_id:
                line_vals["account_id"] = account_id
            if tax_ids:
                line_vals["tax_ids"] = [(6, 0, tax_ids)]

            return line_vals

        return None

    @ETL.load()
    def load_vendor_credits(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load vendor credits into Odoo."""
        move_vals = transformed.get("transform_vendor_credits", [])

        if not move_vals:
            _logger.info("No new vendor credits to create")
            return

        created = 0
        posted = 0
        errors = 0

        for vals in move_vals:
            move = ctx.env["account.move"].create(vals)
            created += 1

            try:
                move.action_post()
                posted += 1
            except RETRYABLE_ERRORS:
                raise
            except Exception as e:
                _logger.warning(f"Could not post vendor credit {vals.get('ref')}: {e}")

        _logger.info(f"Created {created} vendor credits ({posted} posted)")
