"""QuickBooks Online CreditMemo ETL Pipeline

This module handles the migration of CreditMemos from QBO to Odoo
using the ETL framework. CreditMemos become out_refund account.move records.
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
    importer_name="qbo.credit.memo.importer",
    sap_source="CreditMemo",
    depends_on=["qbo.customer.importer", "qbo.item.importer", "qbo.tax.importer"],
)
class QboCreditMemoImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO CreditMemos."""

    _name = "qbo.credit.memo.importer"
    _description = "QBO Credit Memo Importer"

    @ETL.extract("CreditMemo")
    def extract_credit_memos(self, ctx: ETLContext) -> List[Dict]:
        """Extract credit memos from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO credit memo IDs
        ctx.env.cr.execute(
            "SELECT qbo_credit_memo_id FROM account_move WHERE qbo_credit_memo_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing credit memos in Odoo")

        # Fetch all credit memos from QBO
        credit_memos = api_client.query_all(entity="CreditMemo", order_by="Id")

        # Filter out already imported
        new_credit_memos = [
            cm for cm in credit_memos if str(cm.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(credit_memos)} credit memos from QBO, "
            f"{len(new_credit_memos)} are new"
        )
        return new_credit_memos

    @ETL.transform()
    def transform_credit_memos(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO credit memos into Odoo account.move values."""
        credit_memos = extracted.get("extract_credit_memos", [])

        # Build lookups
        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner WHERE qbo_customer_id IS NOT NULL"
        )
        customer_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_item_id, id FROM product_product WHERE qbo_item_id IS NOT NULL"
        )
        product_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_id, id FROM account_tax WHERE qbo_tax_id IS NOT NULL AND type_tax_use = 'sale'"
        )
        tax_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_rate_id, id FROM account_tax WHERE qbo_tax_rate_id IS NOT NULL AND type_tax_use = 'sale'"
        )
        tax_rate_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        # Find sale journal
        journal = ctx.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not journal:
            raise ValueError("No sale journal found for company")

        move_vals = []
        skipped = 0

        for cm in credit_memos:
            # Get customer
            customer_ref = cm.get("CustomerRef", {})
            qbo_customer_id = int(customer_ref.get("value", 0))
            partner_id = customer_map.get(qbo_customer_id)

            if not partner_id:
                _logger.warning(
                    f"Customer not found for QBO ID {qbo_customer_id} "
                    f"in credit memo {cm.get('Id')}"
                )
                skipped += 1
                continue

            # Parse dates
            txn_date = cm.get("TxnDate")

            invoice_date = None
            if txn_date:
                try:
                    invoice_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                except ValueError:
                    invoice_date = datetime.now().date()

            # Get currency
            currency_id = company.currency_id.id
            currency_ref = cm.get("CurrencyRef", {})
            if currency_ref:
                currency_code = currency_ref.get("value")
                if currency_code:
                    currency = ctx.env["res.currency"].search(
                        [("name", "=", currency_code)], limit=1
                    )
                    if currency:
                        currency_id = currency.id

            # Build credit memo lines
            line_vals = []
            for line in cm.get("Line", []):
                line_data = self._transform_credit_memo_line(
                    line, product_map, tax_map, tax_rate_map, cm, ctx
                )
                if line_data:
                    line_vals.append((0, 0, line_data))

            if not line_vals:
                _logger.warning(f"No valid lines for credit memo {cm.get('Id')}")
                skipped += 1
                continue

            vals = {
                "move_type": "out_refund",
                "journal_id": journal.id,
                "partner_id": partner_id,
                "invoice_date": invoice_date,
                "currency_id": currency_id,
                "ref": cm.get("DocNumber", ""),
                "narration": cm.get("CustomerMemo", {}).get("value", ""),
                "invoice_line_ids": line_vals,
                "qbo_credit_memo_id": int(cm.get("Id", 0)),
            }

            move_vals.append(vals)

        _logger.info(f"Transformed {len(move_vals)} credit memos, skipped {skipped}")
        return move_vals

    def _transform_credit_memo_line(
        self,
        line: Dict,
        product_map: Dict,
        tax_map: Dict,
        tax_rate_map: Dict,
        credit_memo: Dict,
        ctx: ETLContext,
    ) -> Optional[Dict]:
        """Transform a single credit memo line."""
        detail_type = line.get("DetailType", "")

        # Skip non-product lines
        if detail_type not in ("SalesItemLineDetail",):
            return None

        detail = line.get("SalesItemLineDetail", {})
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

        # If no unit price but has amount and qty, calculate
        if not unit_price and amount and qty:
            unit_price = amount / qty

        # Credit memos should have positive amounts in Odoo
        unit_price = abs(unit_price)

        # Get tax
        tax_ids = []
        tax_code_ref = detail.get("TaxCodeRef", {})
        if tax_code_ref:
            tax_code_value = tax_code_ref.get("value")
            if tax_code_value and tax_code_value not in ("NON", ""):
                tax_id = tax_map.get(tax_code_value)
                if tax_id:
                    tax_ids.append(tax_id)
                elif tax_code_value == "TAX":
                    txn_tax = credit_memo.get("TxnTaxDetail", {})
                    for tax_line in txn_tax.get("TaxLine", []):
                        tax_detail = tax_line.get("TaxLineDetail", {})
                        tax_rate_ref = tax_detail.get("TaxRateRef", {}).get("value")
                        if tax_rate_ref:
                            rate_tax_id = tax_rate_map.get(tax_rate_ref)
                            if rate_tax_id and rate_tax_id not in tax_ids:
                                tax_ids.append(rate_tax_id)

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

    @ETL.load()
    def load_credit_memos(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load credit memos into Odoo."""
        move_vals = transformed.get("transform_credit_memos", [])

        if not move_vals:
            _logger.info("No new credit memos to create")
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
                _logger.warning(f"Could not post credit memo {vals.get('ref')}: {e}")

        _logger.info(f"Created {created} credit memos ({posted} posted)")
