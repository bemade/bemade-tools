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

        # Map xTuple status to Odoo state.
        # Odoo 19 states: draft, sent, to approve, purchase, cancel.
        # Per the client (task #3814): xTuple's open/working POs live as
        # Unreleased ('U') and must import as CONFIRMED so they get real incoming
        # pickings; the released/closed history ('O'/'C') also imports confirmed
        # and is closed out as fully delivered + invoiced. So all three import as
        # 'purchase'; the open-vs-historical split is preserved on
        # xtuple_pohead_status (below) for the post-import receipt step, NOT on
        # the Odoo state.
        status_map = {
            "U": "purchase",  # Unreleased -> confirmed open PO (real pickings)
            "O": "purchase",  # Open -> historical, closed out full
            "C": "purchase",  # Closed -> historical, closed out full
        }

        order_vals = []
        for order in orders:
            vendor_id = vendor_map.get(order.get("pohead_vend_id"))
            if not vendor_id:
                _logger.warning(
                    f"Vendor not found for PO {order.get('pohead_number')}, skipping"
                )
                continue

            xtuple_status = order.get("pohead_status", "").strip()
            state = status_map.get(xtuple_status, "draft")

            vals = {
                "name": order.get("pohead_number"),
                "partner_id": vendor_id,
                "date_order": order.get("pohead_orderdate"),
                "state": state,
                "xtuple_pohead_id": order.get("pohead_id"),
                "xtuple_pohead_status": xtuple_status or False,
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
        # Make receipt_status reproducible on every migration with no manual
        # recompute (task #3814). Two distinct populations, handled differently:
        #
        #   * HISTORICAL POs (xTuple 'O'/'C') are just history; the client wants
        #     them closed out as fully delivered regardless of actual qty. They
        #     get a direct-write receipt_status='full' (no pickings — Marc
        #     reviewed and APPROVED this SQL path).
        #
        #   * OPEN POs (xTuple 'U' — the client's working orders) need a REAL
        #     incoming stock.picking so the receipt picture is reproducible AND
        #     correct stock results: native purchase_stock._compute_receipt_status
        #     then derives the status from the picking, exactly as for a PO the
        #     user confirmed by hand. For partially-received open POs we use a
        #     back-order protocol (see _generate_open_po_receipts).
        #
        # ORDERING: receipt generation writes stock moves, so it MUST run before
        # the stock-adjustment pipeline (xtuple.stock.quant.importer) that sets
        # final on-hand levels — otherwise the receipts would stack on top of the
        # adjusted quantities. That ordering is guaranteed by stock_quant_etl's
        # depends_on listing this importer (see stock_quant_etl.py).
        self._close_out_historical_pos(ctx)
        self._generate_open_po_receipts(ctx)

    def _close_out_historical_pos(self, ctx: ETLContext) -> None:
        """Mark historical (xTuple 'O'/'C') imported POs fully delivered.

        These are migration history only: per the client they are closed out as
        fully delivered + fully invoiced regardless of the real received qty, so
        we force ``receipt_status='full'`` directly. The field is a stored
        compute whose only trigger is ``picking_ids`` (none exist here), so a raw
        SQL write is safe and stable. Scoped by ``xtuple_pohead_status`` so it
        never touches the open POs (handled via real pickings) or non-imported
        orders.
        """
        ctx.env.flush_all()
        ctx.env.cr.execute(
            """
            UPDATE purchase_order
            SET receipt_status = 'full'
            WHERE xtuple_pohead_id IS NOT NULL
              AND xtuple_pohead_status IN ('O', 'C')
              AND state = 'purchase'
            """
        )
        row_count = ctx.env.cr.rowcount
        # receipt_status was written via raw SQL, bypassing the ORM cache; drop
        # any cached value so subsequent reads in the same transaction are correct.
        ctx.env["purchase.order"].invalidate_model(["receipt_status"])
        _logger.info(
            "Closed out %s historical xTuple POs as fully delivered", row_count
        )

    def _generate_open_po_receipts(self, ctx: ETLContext) -> None:
        """Generate real incoming pickings for OPEN (xTuple 'U') imported POs.

        Why pickings rather than a direct receipt_status write: open POs are the
        client's live, working orders, so their receipt picture must be real and
        editable in Odoo (so further receipts/back-orders work). The native
        ``purchase_stock._compute_receipt_status`` derives status from the
        picking, so once a correct picking exists the status is reproduced with
        no manual step.

        INVESTIGATION (the crux of the design): for storable products
        ``purchase.order.line.qty_received_method`` is forced to ``'stock_moves'``
        the moment a line has stock moves, and ``_compute_qty_received`` then
        OVERWRITES qty_received with the sum of the *done* moves' quantities
        (purchase_stock/models/purchase_order_line.py). So receiving a picking
        recomputes qty_received from the picking — a manually-imported value does
        NOT survive once moves exist. Therefore for a partially-received order we
        cannot just receive "everything"; we must receive exactly the historical
        qty and keep the rest open. We do that with a BACK-ORDER protocol:

          1. ``_create_picking`` builds the incoming picking with moves for the
             FULL ordered qty (no prior moves exist, so it uses product_qty).
          2. Set each move's done quantity to that line's historical received
             qty (captured BEFORE the picking exists, while the value is still
             the imported one).
          3. Validate with back-order creation ENABLED, so the unreceived
             remainder stays open as a back-order picking. qty_received then
             recomputes to exactly the historical received qty, and the native
             receipt_status comes out 'partial'.

        Per-PO cases handled:
          * nothing received  -> picking left ready (assigned), nothing
            validated -> receipt_status 'pending'.
          * partially received -> validate partial + back-order -> 'partial'.
          * fully received -> validate in full (no back-order) -> 'full'.
        """
        ctx.env.flush_all()
        Order = ctx.env["purchase.order"]
        open_orders = Order.search(
            [
                ("xtuple_pohead_id", "!=", False),
                ("xtuple_pohead_status", "=", "U"),
                ("state", "=", "purchase"),
            ]
        )
        if not open_orders:
            _logger.info("No open xTuple POs to generate receipts for")
            return

        generated = 0
        for order in open_orders:
            if order.picking_ids:
                # Idempotent: a re-run (or a partially completed prior run)
                # already produced the picking; do not double-generate.
                continue
            # Snapshot the imported received qty per line BEFORE any moves exist
            # (after _create_picking the line flips to stock_moves method and
            # qty_received recomputes from done moves, losing the imported value).
            received_by_line = {
                line.id: line.qty_received for line in order.order_line
            }
            order._create_picking()
            picking = order.picking_ids.filtered(
                lambda p: p.state not in ("done", "cancel")
            )[:1]
            if not picking:
                # No storable lines -> no picking created. Nothing to receive;
                # the order simply has no receipt picture (correct).
                continue
            self._receive_picking_to_history(picking, received_by_line)
            generated += 1

        # qty_received / receipt_status were driven through the ORM by the
        # validation above; flush so they are persisted before the stock-quant
        # adjustment pipeline reads stock levels.
        ctx.env.flush_all()
        _logger.info(
            "Generated incoming receipts for %s open xTuple POs", generated
        )

    @staticmethod
    def _receive_picking_to_history(picking, received_by_line: Dict) -> None:
        """Validate ``picking`` to the historical received qty, back-ordering the rest.

        ``received_by_line`` maps purchase.order.line id -> historical received
        qty. Sets each move's done quantity to its line's received qty, then:
          * total received == 0  -> leave the picking ready (pending), unvalidated.
          * 0 < received < demand -> validate with a back-order for the remainder.
          * received >= demand    -> validate in full (no back-order).
        """
        total_demand = 0.0
        total_received = 0.0
        for move in picking.move_ids:
            demand = move.product_uom_qty
            received = received_by_line.get(move.purchase_line_id.id, 0.0) or 0.0
            # Never receive more than was ordered on this move; the over-receipt
            # case (received > ordered) still closes the move as full.
            done = min(received, demand)
            total_demand += demand
            total_received += done
            # Setting move.quantity drives the inverse (_set_quantity), which
            # rewrites the reserved move lines to the historical done qty.
            move.quantity = done
            # picked=True is what _action_done uses to know a move was actually
            # received (an unpicked qty>0 move would not be validated). Only mark
            # moves with something received; the rest are left for the back-order.
            move.picked = bool(done)

        if total_received <= 0:
            # Nothing was received yet: keep the picking open and ready so the
            # native receipt_status resolves to 'pending'.
            return

        if total_received < total_demand:
            # Partial: validate and force a back-order for the remainder so the
            # unreceived qty stays open. cancel_backorder=False => back-order is
            # created; skip_backorder=True / skip_sanity_check bypass the
            # interactive validation wizards (this runs unattended).
            picking.with_context(
                skip_backorder=True, skip_sanity_check=True, cancel_backorder=False
            )._action_done()
        else:
            # Fully received: validate the whole picking, no back-order.
            picking.with_context(
                skip_backorder=True, skip_sanity_check=True, cancel_backorder=True
            )._action_done()
