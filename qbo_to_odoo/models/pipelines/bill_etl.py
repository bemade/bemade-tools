"""QuickBooks Online Bill ETL Pipeline

This module handles the migration of Bills (Vendor Bills) from QBO to Odoo
using the ETL framework.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.bill.importer",
    sap_source="Bill",
    depends_on=[
        "qbo.vendor.importer",
        "qbo.item.importer",
        "qbo.tax.importer",
        "qbo.purchase.order.importer",
        "qbo.partner.account.linker",
    ],
)
class QboBillImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Bills."""

    _name = "qbo.bill.importer"
    _description = "QBO Bill Importer"

    @ETL.extract("Bill")
    def extract_bills(self, ctx: ETLContext) -> List[Dict]:
        """Extract bills from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO bill IDs
        ctx.env.cr.execute(
            "SELECT qbo_bill_id FROM account_move WHERE qbo_bill_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing bills in Odoo")

        # Fetch all bills from QBO
        bills = api_client.query_all(entity="Bill", order_by="Id")

        # Filter out already imported
        new_bills = [bill for bill in bills if str(bill.get("Id")) not in existing_ids]

        _logger.info(f"Extracted {len(bills)} bills from QBO, {len(new_bills)} are new")
        return new_bills

    @ETL.transform()
    def transform_bills(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO bills into Odoo account.move values."""
        bills = extracted.get("extract_bills", [])

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

        products_with_expense = ctx.env["product.product"].search(
            [("property_account_expense_id", "!=", False)]
        )
        product_expense_map = {
            p.id: p.property_account_expense_id.id
            for p in products_with_expense
            if p.property_account_expense_id
        }

        ctx.env.cr.execute(
            "SELECT qbo_tax_id, id FROM account_tax WHERE qbo_tax_id IS NOT NULL AND type_tax_use = 'purchase'"
        )
        tax_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_rate_id, id FROM account_tax WHERE qbo_tax_rate_id IS NOT NULL AND type_tax_use = 'purchase'"
        )
        tax_rate_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Build purchase order lookup for linking bills to POs
        ctx.env.cr.execute(
            "SELECT qbo_purchase_order_id, id FROM purchase_order WHERE qbo_purchase_order_id IS NOT NULL"
        )
        po_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

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

        for bill in bills:
            # Get vendor
            vendor_ref = bill.get("VendorRef", {})
            qbo_vendor_id = int(vendor_ref.get("value", 0))
            partner_id = vendor_map.get(qbo_vendor_id)

            if not partner_id:
                _logger.warning(
                    f"Vendor not found for QBO ID {qbo_vendor_id} "
                    f"in bill {bill.get('Id')}"
                )
                skipped += 1
                continue

            # Parse dates
            txn_date = bill.get("TxnDate")
            due_date = bill.get("DueDate")

            invoice_date = None
            if txn_date:
                try:
                    invoice_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                except ValueError:
                    invoice_date = datetime.now().date()

            invoice_date_due = None
            if due_date:
                try:
                    invoice_date_due = datetime.strptime(due_date, "%Y-%m-%d").date()
                except ValueError:
                    pass

            # Get currency
            currency_id = company.currency_id.id
            currency_ref = bill.get("CurrencyRef", {})
            if currency_ref:
                currency_code = currency_ref.get("value")
                if currency_code:
                    currency = ctx.env["res.currency"].search(
                        [("name", "=", currency_code)], limit=1
                    )
                    if currency:
                        currency_id = currency.id

            # Build bill lines
            line_vals = []
            for line in bill.get("Line", []):
                line_data = self._transform_bill_line(
                    line, product_map, product_expense_map, account_map, tax_map, tax_rate_map, bill, ctx
                )
                if line_data:
                    line_vals.append((0, 0, line_data))

            if not line_vals:
                _logger.warning(f"No valid lines for bill {bill.get('Id')}")
                skipped += 1
                continue

            # Compute actual total from line amounts to determine move type
            computed_total = sum(
                line_tuple[2].get("price_unit", 0) * line_tuple[2].get("quantity", 1)
                for line_tuple in line_vals
            )
            move_type = "in_refund" if computed_total < 0 else "in_invoice"

            # For refunds, make line amounts positive (Odoo expects positive amounts)
            if move_type == "in_refund":
                for line_tuple in line_vals:
                    line_data = line_tuple[2]
                    if "price_unit" in line_data:
                        line_data["price_unit"] = abs(line_data["price_unit"])

            # Check for linked PurchaseOrder
            purchase_order_id = None
            for linked in bill.get("LinkedTxn", []):
                if linked.get("TxnType") == "PurchaseOrder":
                    txn_id = str(linked.get("TxnId", ""))
                    if txn_id in po_map:
                        purchase_order_id = po_map[txn_id]
                        break

            vals = {
                "move_type": move_type,
                "journal_id": journal.id,
                "partner_id": partner_id,
                "invoice_date": invoice_date,
                "invoice_date_due": invoice_date_due,
                "currency_id": currency_id,
                "ref": bill.get("DocNumber", ""),
                "narration": bill.get("Memo", ""),
                "invoice_line_ids": line_vals,
                "qbo_bill_id": int(bill.get("Id")),
            }

            # Link to purchase order if found
            if purchase_order_id:
                vals["invoice_origin"] = (
                    ctx.env["purchase.order"].browse(purchase_order_id).name
                )

            move_vals.append(vals)
        _logger.info(f"Transformed {len(move_vals)} bills, skipped {skipped}")
        return move_vals

    def _transform_bill_line(
        self,
        line: Dict,
        product_map: Dict,
        product_expense_map: Dict,
        account_map: Dict,
        tax_map: Dict,
        tax_rate_map: Dict,
        bill: Dict,
        ctx: ETLContext,
    ) -> Optional[Dict]:
        """Transform a single bill line."""
        detail_type = line.get("DetailType", "")

        # Handle item-based expense lines
        if detail_type == "ItemBasedExpenseLineDetail":
            detail = line.get("ItemBasedExpenseLineDetail", {})
            if not detail:
                return None

            # Get product
            item_ref = detail.get("ItemRef", {})
            item_value = item_ref.get("value", "0") if item_ref else "0"
            # Handle special QBO item IDs like SHIPPING_ITEM_ID
            try:
                qbo_item_id = int(item_value)
            except ValueError:
                qbo_item_id = 0  # Special items like SHIPPING_ITEM_ID
            product_id = product_map.get(qbo_item_id)

            # Get quantity and price
            qty = float(detail.get("Qty", 1) or 1)
            unit_price = float(detail.get("UnitPrice", 0) or 0)
            amount = float(line.get("Amount", 0) or 0)

            if not unit_price and amount and qty:
                unit_price = amount / qty

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
                expense_account_id = product_expense_map.get(product_id)
                if expense_account_id:
                    line_vals["account_id"] = expense_account_id
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
    def load_bills(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load bills into Odoo."""
        move_vals = transformed.get("transform_bills", [])

        if not move_vals:
            _logger.info("No new bills to create")
            return

        created = 0
        posted = 0

        for vals in move_vals:
            with ctx.skippable(f"bill QBO#{vals.get('qbo_bill_id', '?')}"):
                move = ctx.env["account.move"].create(vals)
                created += 1
                move.action_post()
                posted += 1

        _logger.info(f"Created {created} bills ({posted} posted)")

        # Update last sync timestamp
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection.last_bill_sync = ctx.env.cr.now()
