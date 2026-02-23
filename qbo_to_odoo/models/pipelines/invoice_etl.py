"""QuickBooks Online Invoice ETL Pipeline

This module handles the migration of Invoices from QBO to Odoo
using the ETL framework.
"""

import logging
from datetime import datetime
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.invoice.importer",
    sap_source="Invoice",
    depends_on=[
        "qbo.customer.importer",
        "qbo.item.importer",
        "qbo.tax.importer",
        "qbo.estimate.importer",
        "qbo.partner.account.linker",
    ],
)
class QboInvoiceImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Invoices."""

    _name = "qbo.invoice.importer"
    _description = "QBO Invoice Importer"

    @ETL.extract("Invoice")
    def extract_invoices(self, ctx: ETLContext) -> List[Dict]:
        """Extract invoices from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO invoice IDs
        ctx.env.cr.execute(
            "SELECT qbo_invoice_id FROM account_move WHERE qbo_invoice_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing invoices in Odoo")

        # Fetch all invoices from QBO
        invoices = api_client.query_all(entity="Invoice", order_by="Id")

        # Filter out already imported
        new_invoices = [
            inv for inv in invoices if str(inv.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(invoices)} invoices from QBO, {len(new_invoices)} are new"
        )
        return new_invoices

    @ETL.transform()
    def transform_invoices(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO invoices into Odoo account.move values."""
        invoices = extracted.get("extract_invoices", [])

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

        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        products_with_income = ctx.env["product.product"].search(
            [("property_account_income_id", "!=", False)]
        )
        product_income_map = {
            p.id: p.property_account_income_id.id
            for p in products_with_income
            if p.property_account_income_id
        }

        # Build sale order lookup for linking invoices to estimates
        ctx.env.cr.execute(
            "SELECT qbo_estimate_id, id FROM sale_order WHERE qbo_estimate_id IS NOT NULL"
        )
        estimate_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

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

        for inv in invoices:
            # Get customer
            customer_ref = inv.get("CustomerRef", {})
            qbo_customer_id = int(customer_ref.get("value", 0))
            partner_id = customer_map.get(qbo_customer_id)

            if not partner_id:
                _logger.warning(
                    f"Customer not found for QBO ID {qbo_customer_id} "
                    f"in invoice {inv.get('Id')}"
                )
                skipped += 1
                continue

            # Parse dates
            txn_date = inv.get("TxnDate")
            due_date = inv.get("DueDate")

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
            currency_ref = inv.get("CurrencyRef", {})
            if currency_ref:
                currency_code = currency_ref.get("value")
                if currency_code:
                    currency = ctx.env["res.currency"].search(
                        [("name", "=", currency_code)], limit=1
                    )
                    if currency:
                        currency_id = currency.id

            # Build invoice lines
            line_vals = []
            for line in inv.get("Line", []):
                line_data = self._transform_invoice_line(
                    line, product_map, product_income_map, tax_map, tax_rate_map, inv, ctx
                )
                if line_data:
                    line_vals.append((0, 0, line_data))

            if not line_vals:
                _logger.warning(f"No valid lines for invoice {inv.get('Id')}")
                skipped += 1
                continue

            # Check total amount to determine if this is a refund
            total_amt = float(inv.get("TotalAmt", 0) or 0)
            move_type = "out_refund" if total_amt < 0 else "out_invoice"

            # If refund, make line amounts positive
            if move_type == "out_refund":
                for line_tuple in line_vals:
                    line_data = line_tuple[2]
                    if "price_unit" in line_data:
                        line_data["price_unit"] = abs(line_data["price_unit"])

            # Check for linked Estimate (sale order)
            sale_order_id = None
            for linked in inv.get("LinkedTxn", []):
                if linked.get("TxnType") == "Estimate":
                    txn_id = str(linked.get("TxnId", ""))
                    if txn_id in estimate_map:
                        sale_order_id = estimate_map[txn_id]
                        break

            vals = {
                "move_type": move_type,
                "journal_id": journal.id,
                "partner_id": partner_id,
                "invoice_date": invoice_date,
                "invoice_date_due": invoice_date_due,
                "currency_id": currency_id,
                "ref": inv.get("DocNumber", ""),
                "narration": inv.get("CustomerMemo", {}).get("value", ""),
                "invoice_line_ids": line_vals,
                "qbo_invoice_id": int(inv.get("Id")),
            }

            # Link to sale order if found
            if sale_order_id:
                vals["invoice_origin"] = (
                    ctx.env["sale.order"].browse(sale_order_id).name
                )

            move_vals.append(vals)

        _logger.info(f"Transformed {len(move_vals)} invoices, skipped {skipped}")
        return move_vals

    def _transform_invoice_line(
        self,
        line: Dict,
        product_map: Dict,
        product_income_map: Dict,
        tax_map: Dict,
        tax_rate_map: Dict,
        invoice: Dict,
        ctx: ETLContext,
    ) -> Dict:
        """Transform a single invoice line."""
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

        # If no unit price but has amount and qty, calculate
        if not unit_price and amount and qty:
            unit_price = amount / qty

        # Get tax
        tax_ids = []
        tax_code_ref = detail.get("TaxCodeRef", {})
        if tax_code_ref:
            tax_code_value = tax_code_ref.get("value")
            if tax_code_value and tax_code_value not in ("NON", ""):
                # First try to get tax from tax_map using the TaxCodeRef
                tax_id = tax_map.get(tax_code_value)
                if tax_id:
                    tax_ids.append(tax_id)
                elif tax_code_value == "TAX":
                    # Fall back to looking for taxes in TxnTaxDetail
                    txn_tax = invoice.get("TxnTaxDetail", {})
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
            income_account_id = product_income_map.get(product_id)
            if income_account_id:
                line_vals["account_id"] = income_account_id

        if tax_ids:
            line_vals["tax_ids"] = [(6, 0, tax_ids)]

        return line_vals

    @ETL.load()
    def load_invoices(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load invoices into Odoo."""
        move_vals = transformed.get("transform_invoices", [])

        if not move_vals:
            _logger.info("No new invoices to create")
            return

        created = 0
        posted = 0

        for vals in move_vals:
            with ctx.skippable(f"invoice QBO#{vals.get('qbo_invoice_id', '?')}"):
                move = ctx.env["account.move"].create(vals)
                created += 1
                move.action_post()
                posted += 1

        _logger.info(f"Created {created} invoices ({posted} posted)")

        # Update last sync timestamp
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection.last_invoice_sync = ctx.env.cr.now()
