"""Tests for xTuple PO receipt reproducibility (task #3814).

Two populations of imported confirmed (``state='purchase'``) POs, distinguished
by the persisted raw xTuple status ``xtuple_pohead_status``:

* HISTORICAL (``'O'``/``'C'``) — migration history; the client wants them closed
  out as fully delivered regardless of actual qty.
  ``XtuplePurchaseOrderLineImporter._close_out_historical_pos`` direct-writes
  ``receipt_status='full'`` (no pickings — this is the SQL path Marc approved).

* OPEN (``'U'`` — the client's live/working orders) — must get a REAL incoming
  ``stock.picking`` so the native ``purchase_stock._compute_receipt_status``
  (picking-derived) reproduces the status with no manual step, AND so the stock
  picture is correct/editable. ``_generate_open_po_receipts`` creates the
  picking for the full ordered qty and receives exactly the historical received
  qty, back-ordering the remainder (because receiving recomputes qty_received
  from the done moves — a manually-imported value does NOT survive once moves
  exist, so we must receive the right amount, not "everything").

These tests build PO/line records via the ORM (no live xTuple connection) to
mirror the post-import state, then call the importer helpers directly.

Test plan:
1. Open, nothing received    -> picking generated (ready), receipt_status 'pending'
2. Open, fully received      -> picking validated in full, receipt_status 'full'
3. Open, partially received  -> picking + back-order; qty_received preserved at
                                the historical value (NOT recomputed away);
                                receipt_status 'partial'
4. Open, multi-line mixed     -> partial; received lines preserved, rest back-ordered
5. Open, over-received        -> capped at ordered, 'full', no spurious back-order
6/7. Historical 'O'/'C'      -> closed out 'full' via direct write, NO picking
8. Historical partial-on-paper-> still 'full' (closed out regardless of qty)
9. Non-imported PO           -> never touched
10. Draft (state != purchase) -> never touched, stays blank
11. Idempotency              -> re-running generates no second picking
12. ORDERING                 -> stock.quant importer depends_on the PO-line
                                importer, so receipts run before the adjustment
"""

import logging

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@tagged("-at_install", "post_install")
class TestOpenPoReceipts(TransactionCase):
    """Open POs get real pickings; historical POs are closed out by SQL."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["xtuple.purchase.order.line.importer"]
        cls.ctx = ETLContext(cr=None, env=cls.env)
        cls.vendor = cls.env["res.partner"].create(
            {"name": "xTuple Test Vendor", "supplier_rank": 1}
        )
        # Storable product -> purchase_stock generates a picking on confirm and
        # forces qty_received_method='stock_moves' (the recompute crux).
        cls.product = cls.env["product.product"].create(
            {
                "name": "xTuple Test Widget",
                "type": "consu",
                "is_storable": True,
                "purchase_ok": True,
            }
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_po(self, lines, *, status="U", xtuple_id=2000, imported=True):
        """Create a confirmed imported PO mirroring the post-import state.

        ``lines`` is a list of (ordered, received) tuples. ``received`` is
        written via the manual inverse (no pickings yet), exactly as the line
        importer does. ``status`` is the raw xTuple pohead_status.
        """
        order_lines = []
        for idx, (ordered, _received) in enumerate(lines):
            order_lines.append(
                (
                    0,
                    0,
                    {
                        "product_id": self.product.id,
                        "name": "line %s" % idx,
                        "product_qty": ordered,
                        "price_unit": 1.0,
                        "xtuple_poitem_id": xtuple_id * 100 + idx,
                    },
                )
            )
        po = self.env["purchase.order"].create(
            {
                "partner_id": self.vendor.id,
                "order_line": order_lines,
                "xtuple_pohead_id": xtuple_id if imported else False,
                "xtuple_pohead_status": status if imported else False,
            }
        )
        # Importer sets state directly (no button_confirm) and writes
        # qty_received via the manual inverse (no pickings).
        po.state = "purchase"
        for line, (_ordered, received) in zip(po.order_line, lines):
            if received:
                line.write({"qty_received": received})
        po.invalidate_recordset(["receipt_status"])
        # Precondition: with no pickings, native compute leaves status blank.
        self.assertFalse(
            po.receipt_status,
            "precondition: an imported confirmed PO with no pickings must have "
            "a blank receipt_status before the fill runs",
        )
        return po

    def _status(self, po):
        po.invalidate_recordset(["receipt_status"])
        return po.receipt_status

    # ==================================================================
    # OPEN POs (xTuple 'U') -> real pickings
    # ==================================================================

    def test_open_nothing_received_generates_ready_picking_pending(self):
        po = self._make_po([(10, 0)], xtuple_id=2001)
        self.importer._generate_open_po_receipts(self.ctx)
        self.assertTrue(po.picking_ids, "an incoming picking must be generated")
        self.assertTrue(
            all(p.state not in ("done", "cancel") for p in po.picking_ids),
            "nothing received -> picking stays ready, not validated",
        )
        self.assertEqual(self._status(po), "pending")

    def test_open_fully_received_validates_full(self):
        po = self._make_po([(10, 10)], xtuple_id=2002)
        self.importer._generate_open_po_receipts(self.ctx)
        self.assertTrue(po.picking_ids)
        self.assertTrue(
            any(p.state == "done" for p in po.picking_ids),
            "fully received -> picking validated done",
        )
        # No back-order should remain for a fully-received order.
        self.assertFalse(
            po.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")),
            "fully received -> no open back-order picking",
        )
        self.assertEqual(self._status(po), "full")
        self.assertEqual(po.order_line.qty_received, 10)

    def test_open_partial_backorders_and_preserves_received_qty(self):
        """The crux: receive 4 of 10 -> qty_received stays 4 (recomputed from the
        done move, not lost), a back-order holds the remaining 6, status partial.
        """
        po = self._make_po([(10, 4)], xtuple_id=2003)
        self.importer._generate_open_po_receipts(self.ctx)
        done = po.picking_ids.filtered(lambda p: p.state == "done")
        backorder = po.picking_ids.filtered(
            lambda p: p.state not in ("done", "cancel")
        )
        self.assertTrue(done, "a validated (done) receipt must exist")
        self.assertTrue(backorder, "a back-order must hold the remaining qty")
        self.assertEqual(self._status(po), "partial")
        # qty_received recomputes from the done move == the historical value.
        self.assertEqual(
            po.order_line.qty_received,
            4,
            "received qty must be preserved at the historical value",
        )
        # The back-order demand is the remaining 6.
        self.assertEqual(sum(backorder.move_ids.mapped("product_uom_qty")), 6)

    def test_open_multi_line_mixed_partial(self):
        """Line A fully received, line B partially, line C untouched -> partial,
        each line's received qty preserved, remainder back-ordered."""
        po = self._make_po([(10, 10), (5, 2), (8, 0)], xtuple_id=2004)
        self.importer._generate_open_po_receipts(self.ctx)
        self.assertEqual(self._status(po), "partial")
        by_ordered = {l.product_qty: l.qty_received for l in po.order_line}
        self.assertEqual(by_ordered[10], 10)
        self.assertEqual(by_ordered[5], 2)
        self.assertEqual(by_ordered[8], 0)
        backorder = po.picking_ids.filtered(
            lambda p: p.state not in ("done", "cancel")
        )
        self.assertTrue(backorder)
        # Back-order demand = unreceived remainder: (5-2) + (8-0) = 11.
        self.assertEqual(sum(backorder.move_ids.mapped("product_uom_qty")), 11)

    def test_open_over_received_caps_at_ordered_full(self):
        """received > ordered must close the move full with no back-order."""
        po = self._make_po([(10, 12)], xtuple_id=2005)
        self.importer._generate_open_po_receipts(self.ctx)
        self.assertEqual(self._status(po), "full")
        self.assertFalse(
            po.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")),
            "over-receipt must not spawn a back-order",
        )
        self.assertEqual(po.order_line.qty_received, 10)

    def test_open_idempotent_no_second_picking(self):
        po = self._make_po([(10, 4)], xtuple_id=2006)
        self.importer._generate_open_po_receipts(self.ctx)
        count = len(po.picking_ids)
        self.importer._generate_open_po_receipts(self.ctx)
        self.assertEqual(
            len(po.picking_ids), count, "re-run must not generate a second picking"
        )

    # ==================================================================
    # HISTORICAL POs (xTuple 'O'/'C') -> closed out full, no picking
    # ==================================================================

    def test_historical_open_status_closed_out_full(self):
        po = self._make_po([(10, 3)], status="O", xtuple_id=2007)
        self.importer._close_out_historical_pos(self.ctx)
        self.assertEqual(self._status(po), "full")
        self.assertFalse(po.picking_ids, "historical POs get no pickings")

    def test_historical_closed_status_closed_out_full(self):
        po = self._make_po([(10, 0)], status="C", xtuple_id=2008)
        self.importer._close_out_historical_pos(self.ctx)
        self.assertEqual(self._status(po), "full")
        self.assertFalse(po.picking_ids)

    def test_historical_full_regardless_of_qty(self):
        """Even a partially-received-on-paper historical PO is closed out full."""
        po = self._make_po([(10, 1), (20, 0)], status="C", xtuple_id=2009)
        self.importer._close_out_historical_pos(self.ctx)
        self.assertEqual(self._status(po), "full")

    def test_close_out_does_not_touch_open_pos(self):
        """The historical close-out must leave 'U' (open) POs blank for the
        picking path to handle."""
        po = self._make_po([(10, 5)], status="U", xtuple_id=2010)
        self.importer._close_out_historical_pos(self.ctx)
        self.assertFalse(
            self._status(po), "open POs must not be touched by the close-out"
        )

    # ==================================================================
    # Guards: non-imported / draft
    # ==================================================================

    def test_non_imported_po_untouched(self):
        po = self.env["purchase.order"].create(
            {
                "partner_id": self.vendor.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "name": "native line",
                            "product_qty": 5,
                            "price_unit": 1.0,
                        },
                    )
                ],
            }
        )
        po.state = "purchase"
        po.invalidate_recordset(["receipt_status"])
        before_pickings = po.picking_ids
        self.importer._close_out_historical_pos(self.ctx)
        self.importer._generate_open_po_receipts(self.ctx)
        self.assertEqual(
            po.picking_ids,
            before_pickings,
            "a non-imported PO must not get migration pickings",
        )

    def test_draft_imported_po_untouched(self):
        """A draft (unconfirmed) imported PO is touched by neither path."""
        po = self._make_po([(10, 0)], status="U", xtuple_id=2011)
        po.state = "draft"
        po.invalidate_recordset(["receipt_status"])
        self.importer._close_out_historical_pos(self.ctx)
        self.importer._generate_open_po_receipts(self.ctx)
        self.assertFalse(po.picking_ids, "draft PO must not get a migration picking")
        self.assertFalse(self._status(po))

    # ==================================================================
    # ORDERING (requirement #3)
    # ==================================================================

    def test_receipts_run_before_stock_adjustment(self):
        """The stock-quant adjustment importer must depend on the PO-line
        importer, guaranteeing receipts (which write stock moves) run BEFORE the
        on-hand adjustment that sets final levels."""
        quant_pipeline = ETL.get_pipeline("xtuple.stock.quant.importer")
        self.assertIsNotNone(quant_pipeline)
        self.assertIn(
            "xtuple.purchase.order.line.importer",
            quant_pipeline.depends_on,
            "stock.quant importer must run after PO-line receipts so receipts "
            "don't stack on top of the adjusted on-hand quantities",
        )
