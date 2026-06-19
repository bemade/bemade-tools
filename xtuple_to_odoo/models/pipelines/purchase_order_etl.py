"""xTuple Purchase Order ETL Pipelines

This module contains ETL pipelines for importing purchase orders
and purchase order lines from xTuple.
"""

import logging
from typing import Dict, List

from odoo import api, models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData

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
    def extract_lines(self, ctx: ETLContext) -> ChunkableData:
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
        return ChunkableData(
            records=lines,
            context={"po_map": po_map, "product_map": product_map},
        )

    @ETL.transform()
    def transform_lines(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple PO lines into Odoo purchase order line values."""
        data = extracted.get("extract_lines")
        lines = data.records if data else []
        po_map = data.context.get("po_map", {}) if data else {}
        product_map = data.context.get("product_map", {}) if data else {}

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
        # Imported POs are written with state='purchase' directly (no
        # button_confirm), so no incoming pickings exist and the native
        # purchase_stock._compute_receipt_status (derived from picking_ids.state)
        # leaves receipt_status blank on every confirmed PO. Bake the line-based
        # fill into this automatic load step so receipt_status is reproducible on
        # every migration with no manual recompute (task #3814).
        self._recompute_receipt_status(ctx)

    def _recompute_receipt_status(self, ctx: ETLContext) -> None:
        """Set receipt_status from line ordered-vs-received qty for imported POs.

        Mirrors purchase_stock._compute_receipt_status' full/partial/pending
        semantics from line quantities instead of pickings, because the import
        writes ``state='purchase'`` without ``button_confirm`` so no pickings are
        ever generated. Scoped to confirmed (``state='purchase'``) xTuple POs:
        draft POs correctly keep a blank receipt_status (nothing to receive yet),
        matching the native behaviour for POs with no pickings.

        Run as raw SQL (the field is a stored compute whose only trigger is
        picking_ids, which never fire here) and keyed on ``xtuple_pohead_id`` so
        it only touches imported orders.
        """
        ctx.env.flush_all()
        ctx.env.cr.execute(
            """
            UPDATE purchase_order po
            SET receipt_status = CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM purchase_order_line pol
                    WHERE pol.order_id = po.id
                )
                THEN NULL
                WHEN NOT EXISTS (
                    SELECT 1 FROM purchase_order_line pol
                    WHERE pol.order_id = po.id
                      AND pol.qty_received < pol.product_qty
                )
                THEN 'full'
                WHEN EXISTS (
                    SELECT 1 FROM purchase_order_line pol
                    WHERE pol.order_id = po.id
                      AND pol.qty_received > 0
                )
                THEN 'partial'
                ELSE 'pending'
            END
            WHERE po.xtuple_pohead_id IS NOT NULL
              AND po.state = 'purchase'
            """
        )
        row_count = ctx.env.cr.rowcount
        # receipt_status was written via raw SQL, bypassing the ORM cache; drop
        # any cached value so subsequent reads in the same transaction are correct.
        ctx.env["purchase.order"].invalidate_model(["receipt_status"])
        _logger.info(
            "Recomputed receipt_status on %s confirmed xTuple POs from line qty",
            row_count,
        )
