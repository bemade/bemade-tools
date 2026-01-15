"""xTuple Purchase Order ETL Pipelines

This module contains ETL pipelines for importing purchase orders
and purchase order lines from xTuple.
"""

import logging
from typing import Dict, List

from odoo import api, models

from odoo.addons.etl_framework.framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# SQL for extracting purchase order headers
SELECT_PO_HEADERS = """
    SELECT 
        pohead_id,
        pohead_number,
        pohead_status,
        pohead_orderdate,
        pohead_vend_id,
        pohead_comments,
        pohead_freight,
        pohead_curr_id,
        pohead_shipvia,
        pohead_fob
    FROM pohead
"""

# SQL for extracting purchase order lines
SELECT_PO_LINES = """
    SELECT 
        poitem_id,
        poitem_pohead_id,
        poitem_linenumber,
        poitem_status,
        poitem_duedate,
        poitem_itemsite_id,
        poitem_qty_ordered,
        poitem_qty_received,
        poitem_unitprice,
        poitem_vend_item_number,
        poitem_vend_item_descrip,
        poitem_comments,
        item_id,
        item_number
    FROM poitem
    LEFT JOIN itemsite ON poitem_itemsite_id = itemsite_id
    LEFT JOIN item ON itemsite_item_id = item_id
"""


@ETL.pipeline(
    target_model="purchase.order",
    importer_name="xtuple.purchase.order.importer",
    depends_on=[
        "xtuple.partner.vendor.importer",
        "xtuple.product.importer",
    ],
)
class XtuplePurchaseOrderImporter(models.AbstractModel):
    """ETL Pipeline for importing purchase orders from xTuple."""

    _name = "xtuple.purchase.order.importer"
    _description = "xTuple Purchase Order Importer"

    @ETL.extract("pohead")
    def extract_orders(self, ctx: ETLContext) -> Dict:
        """Extract purchase orders from xTuple."""
        # Check for existing POs
        ctx.env.cr.execute(
            "SELECT xtuple_pohead_id FROM purchase_order WHERE xtuple_pohead_id IS NOT NULL"
        )
        existing_pohead_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(f"Found {len(existing_pohead_ids)} existing POs in Odoo")

        # Extract PO headers
        if existing_pohead_ids:
            ctx.cr.execute(
                SELECT_PO_HEADERS + " WHERE pohead_id NOT IN %s",
                (tuple(existing_pohead_ids),),
            )
        else:
            ctx.cr.execute(SELECT_PO_HEADERS)

        orders = ctx.cr.dictfetchall()

        # Get vendor mapping
        ctx.env.cr.execute(
            "SELECT xtuple_vend_id, id FROM res_partner WHERE xtuple_vend_id IS NOT NULL"
        )
        vendor_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        _logger.info(f"Extracted {len(orders)} new POs from xTuple")
        return {"orders": orders, "vendor_map": vendor_map}

    @ETL.transform()
    def transform_orders(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple POs into Odoo purchase order values."""
        orders = extracted.get("extract_orders", {}).get("orders", [])
        vendor_map = extracted.get("extract_orders", {}).get("vendor_map", {})

        # Map xTuple status to Odoo state
        # Odoo 19 states: draft, sent, to approve, purchase, cancel
        status_map = {
            "U": "draft",  # Unreleased
            "O": "purchase",  # Open
            "C": "purchase",  # Closed -> purchase (no 'done' state in Odoo 19)
        }

        order_vals = []
        for order in orders:
            vendor_id = vendor_map.get(order.get("pohead_vend_id"))
            if not vendor_id:
                _logger.warning(
                    f"Vendor not found for PO {order.get('pohead_number')}, skipping"
                )
                continue

            state = status_map.get(order.get("pohead_status", "").strip(), "draft")

            vals = {
                "name": order.get("pohead_number"),
                "partner_id": vendor_id,
                "date_order": order.get("pohead_orderdate"),
                "state": state,
                "xtuple_pohead_id": order.get("pohead_id"),
            }
            # Add comments if present (note is Html field in Odoo 19)
            if order.get("pohead_comments"):
                vals["note"] = order.get("pohead_comments")
            order_vals.append(vals)

        _logger.info(f"Transformed {len(order_vals)} PO records")
        return order_vals

    @ETL.load()
    def load_orders(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load purchase orders into Odoo."""
        order_vals = transformed.get("transform_orders", [])
        if order_vals:
            # Create POs - they'll be in draft state initially
            orders = (
                ctx.env["purchase.order"]
                .with_context(tracking_disable=True)
                .create(order_vals)
            )
            _logger.info(f"Created {len(orders)} purchase orders")


@ETL.pipeline(
    target_model="purchase.order.line",
    importer_name="xtuple.purchase.order.line.importer",
    depends_on=["xtuple.purchase.order.importer"],
)
class XtuplePurchaseOrderLineImporter(models.AbstractModel):
    """ETL Pipeline for importing purchase order lines from xTuple."""

    _name = "xtuple.purchase.order.line.importer"
    _description = "xTuple Purchase Order Line Importer"

    @ETL.extract("poitem")
    def extract_lines(self, ctx: ETLContext) -> Dict:
        """Extract purchase order lines from xTuple."""
        # Check for existing PO lines
        ctx.env.cr.execute(
            "SELECT xtuple_poitem_id FROM purchase_order_line WHERE xtuple_poitem_id IS NOT NULL"
        )
        existing_poitem_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(f"Found {len(existing_poitem_ids)} existing PO lines in Odoo")

        # Extract PO lines
        if existing_poitem_ids:
            ctx.cr.execute(
                SELECT_PO_LINES + " WHERE poitem_id NOT IN %s",
                (tuple(existing_poitem_ids),),
            )
        else:
            ctx.cr.execute(SELECT_PO_LINES)

        lines = ctx.cr.dictfetchall()

        # Get PO mapping
        ctx.env.cr.execute(
            "SELECT xtuple_pohead_id, id FROM purchase_order WHERE xtuple_pohead_id IS NOT NULL"
        )
        po_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Get product mapping
        ctx.env.cr.execute(
            "SELECT xtuple_item_id, id FROM product_product WHERE xtuple_item_id IS NOT NULL"
        )
        product_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        _logger.info(f"Extracted {len(lines)} new PO lines from xTuple")
        return {"lines": lines, "po_map": po_map, "product_map": product_map}

    @ETL.transform()
    def transform_lines(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple PO lines into Odoo purchase order line values."""
        lines = extracted.get("extract_lines", {}).get("lines", [])
        po_map = extracted.get("extract_lines", {}).get("po_map", {})
        product_map = extracted.get("extract_lines", {}).get("product_map", {})

        line_vals = []
        for line in lines:
            po_id = po_map.get(line.get("poitem_pohead_id"))
            if not po_id:
                continue

            product_id = product_map.get(line.get("item_id"))

            vals = {
                "order_id": po_id,
                "sequence": line.get("poitem_linenumber", 10),
                "product_id": product_id,
                "name": line.get("poitem_vend_item_descrip")
                or line.get("item_number")
                or "Unknown Product",
                "product_qty": line.get("poitem_qty_ordered", 0),
                "qty_received": line.get("poitem_qty_received", 0),
                "price_unit": line.get("poitem_unitprice", 0),
                "date_planned": line.get("poitem_duedate"),
                "xtuple_poitem_id": line.get("poitem_id"),
            }
            line_vals.append(vals)

        _logger.info(f"Transformed {len(line_vals)} PO line records")
        return line_vals

    @ETL.load()
    def load_lines(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load purchase order lines into Odoo."""
        line_vals = transformed.get("transform_lines", [])
        if line_vals:
            lines = (
                ctx.env["purchase.order.line"]
                .with_context(tracking_disable=True)
                .create(line_vals)
            )
            _logger.info(f"Created {len(lines)} purchase order lines")
