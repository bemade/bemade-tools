"""QuickBooks Online PurchaseOrder ETL Pipeline

This module handles the migration of PurchaseOrders from QBO to Odoo purchase.order
using the ETL framework.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="purchase.order",
    importer_name="qbo.purchase.order.importer",
    sap_source="PurchaseOrder",
    depends_on=["qbo.vendor.importer", "qbo.item.importer", "qbo.tax.importer"],
)
class QboPurchaseOrderImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO PurchaseOrders as purchase.order."""

    _name = "qbo.purchase.order.importer"
    _description = "QBO Purchase Order Importer"

    @ETL.extract("PurchaseOrder")
    def extract_purchase_orders(self, ctx: ETLContext) -> List[Dict]:
        """Extract purchase orders from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO purchase order IDs
        ctx.env.cr.execute(
            "SELECT qbo_purchase_order_id FROM purchase_order WHERE qbo_purchase_order_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing purchase orders in Odoo")

        # Fetch all purchase orders from QBO
        purchase_orders = api_client.query_all(entity="PurchaseOrder", order_by="Id")

        # Filter out already imported
        new_purchase_orders = [
            po for po in purchase_orders if str(po.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(purchase_orders)} purchase orders from QBO, "
            f"{len(new_purchase_orders)} are new"
        )
        return new_purchase_orders

    @ETL.transform()
    def transform_purchase_orders(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO purchase orders into Odoo purchase.order values."""
        purchase_orders = extracted.get("extract_purchase_orders", [])

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
            "SELECT qbo_tax_id, id FROM account_tax WHERE qbo_tax_id IS NOT NULL AND type_tax_use = 'purchase'"
        )
        tax_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_rate_id, id FROM account_tax WHERE qbo_tax_rate_id IS NOT NULL AND type_tax_use = 'purchase'"
        )
        tax_rate_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        order_vals = []
        skipped = 0

        for po in purchase_orders:
            # Get vendor
            vendor_ref = po.get("VendorRef", {})
            qbo_vendor_id = int(vendor_ref.get("value", 0))
            partner_id = vendor_map.get(qbo_vendor_id)

            if not partner_id:
                _logger.warning(
                    f"Vendor not found for QBO ID {qbo_vendor_id} "
                    f"in purchase order {po.get('Id')}"
                )
                skipped += 1
                continue

            # Parse date
            txn_date = po.get("TxnDate")
            order_date = None
            if txn_date:
                try:
                    order_date = datetime.strptime(txn_date, "%Y-%m-%d")
                except ValueError:
                    order_date = datetime.now()

            # Get currency
            currency_id = company.currency_id.id
            currency_ref = po.get("CurrencyRef", {})
            if currency_ref:
                currency_code = currency_ref.get("value")
                if currency_code:
                    currency = ctx.env["res.currency"].search(
                        [("name", "=", currency_code)], limit=1
                    )
                    if currency:
                        currency_id = currency.id

            # Build order lines
            line_vals = []
            for line in po.get("Line", []):
                line_data = self._transform_purchase_order_line(
                    line, product_map, tax_map, tax_rate_map, po, ctx
                )
                if line_data:
                    line_vals.append((0, 0, line_data))

            if not line_vals:
                _logger.warning(f"No valid lines for purchase order {po.get('Id')}")
                skipped += 1
                continue

            vals = {
                "partner_id": partner_id,
                "date_order": order_date,
                "currency_id": currency_id,
                "partner_ref": po.get("DocNumber", ""),
                "note": po.get("Memo", ""),
                "order_line": line_vals,
                "qbo_purchase_order_id": int(po.get("Id", 0)),
                "company_id": company.id,
            }

            order_vals.append(vals)

        _logger.info(
            f"Transformed {len(order_vals)} purchase orders, skipped {skipped}"
        )
        return order_vals

    def _transform_purchase_order_line(
        self,
        line: Dict,
        product_map: Dict,
        tax_map: Dict,
        tax_rate_map: Dict,
        purchase_order: Dict,
        ctx: ETLContext,
    ) -> Optional[Dict]:
        """Transform a single purchase order line."""
        detail_type = line.get("DetailType", "")

        # Handle item-based lines
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
                "product_qty": qty,
                "price_unit": unit_price,
            }

            if product_id:
                line_vals["product_id"] = product_id

            if tax_ids:
                line_vals["taxes_id"] = [(6, 0, tax_ids)]

            return line_vals

        return None

    @ETL.load()
    def load_purchase_orders(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load purchase orders into Odoo."""
        order_vals = transformed.get("transform_purchase_orders", [])

        if not order_vals:
            _logger.info("No new purchase orders to create")
            return

        created = 0
        errors = 0

        # Batch create purchase orders
        orders = ctx.env["purchase.order"].create(order_vals)
        _logger.info(f"Created {len(orders)} purchase orders")
