"""Tests for xTuple PO receipt_status reproducibility (task #3814).

The xTuple import writes ``state='purchase'`` directly without
``button_confirm``, so no incoming pickings are generated and the native
``purchase_stock._compute_receipt_status`` (derived from picking_ids.state)
leaves ``receipt_status`` blank on every confirmed imported PO. The fix bakes a
line-based fill into the automatic ``load_lines`` step
(``XtuplePurchaseOrderLineImporter._recompute_receipt_status``) so the status is
reproduced on every migration with no manual recompute.

These tests exercise that helper directly against real PO/line records built via
the ORM (no live xTuple connection needed), asserting the full/partial/pending
mapping matches the native compute's semantics and that draft / non-imported POs
are left untouched.

Test plan:
1. Fully received PO  -> receipt_status = 'full'
2. Partially received PO -> 'partial'
3. Nothing received PO -> 'pending'
4. Over-received PO (qty_received > product_qty) -> 'full'
5. Draft (state != 'purchase') PO -> left blank (NULL)
6. Non-imported PO (no xtuple_pohead_id) -> never touched
7. Multi-line: any short line -> 'partial' (mixed received), all met -> 'full'
"""

import logging

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext

_logger = logging.getLogger(__name__)


@tagged("-at_install", "post_install")
class TestReceiptStatusReproducible(TransactionCase):
    """Unit tests for ``_recompute_receipt_status`` line-based fill."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["xtuple.purchase.order.line.importer"]
        cls.ctx = ETLContext(cr=None, env=cls.env)
        cls.vendor = cls.env["res.partner"].create(
            {"name": "xTuple Test Vendor", "supplier_rank": 1}
        )
        cls.product = cls.env["product.product"].create(
            {"name": "xTuple Test Widget", "type": "consu", "purchase_ok": True}
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_po(self, lines, *, state="purchase", xtuple_id=1000, imported=True):
        """Create a confirmed imported PO with the given (ordered, received) lines.

        ``lines`` is a list of (product_qty, qty_received) tuples. Returns the PO.
        ``qty_received`` is written via the manual inverse so no pickings exist
        (exactly the imported-PO situation).
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
            }
        )
        # Set state directly like the importer does (no button_confirm).
        po.state = state
        # Apply received quantities via the manual inverse (no pickings).
        for line, (_ordered, received) in zip(po.order_line, lines):
            if received:
                line.write(
                    {"qty_received": received, "qty_received_method": "manual"}
                )
        # Native compute leaves it blank because there are no pickings.
        po.invalidate_recordset(["receipt_status"])
        self.assertFalse(
            po.receipt_status,
            "precondition: native compute must leave receipt_status blank "
            "(no pickings exist for an imported PO)",
        )
        return po

    def _status(self, po):
        po.invalidate_recordset(["receipt_status"])
        return po.receipt_status

    # ------------------------------------------------------------------
    # Test plan #1-4 — single-line full / partial / pending / over-received
    # ------------------------------------------------------------------

    def test_fully_received_is_full(self):
        po = self._make_po([(10, 10)], xtuple_id=1001)
        self.importer._recompute_receipt_status(self.ctx)
        self.assertEqual(self._status(po), "full")

    def test_partially_received_is_partial(self):
        po = self._make_po([(10, 4)], xtuple_id=1002)
        self.importer._recompute_receipt_status(self.ctx)
        self.assertEqual(self._status(po), "partial")

    def test_nothing_received_is_pending(self):
        po = self._make_po([(10, 0)], xtuple_id=1003)
        self.importer._recompute_receipt_status(self.ctx)
        self.assertEqual(self._status(po), "pending")

    def test_over_received_is_full(self):
        """qty_received > product_qty must still resolve to 'full', not partial."""
        po = self._make_po([(10, 12)], xtuple_id=1004)
        self.importer._recompute_receipt_status(self.ctx)
        self.assertEqual(self._status(po), "full")

    # ------------------------------------------------------------------
    # Test plan #5 — draft PO left blank
    # ------------------------------------------------------------------

    def test_draft_po_left_blank(self):
        """A draft (unconfirmed) imported PO keeps a blank receipt_status."""
        po = self._make_po([(10, 0)], state="draft", xtuple_id=1005)
        self.importer._recompute_receipt_status(self.ctx)
        self.assertFalse(self._status(po))

    # ------------------------------------------------------------------
    # Test plan #6 — non-imported PO never touched
    # ------------------------------------------------------------------

    def test_non_imported_po_untouched(self):
        """A PO with no xtuple_pohead_id must be ignored by the fill."""
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
        po.write({"order_line": [(1, po.order_line.id, {"qty_received": 5})]})
        po.order_line.qty_received_method = "manual"
        po.order_line.qty_received = 5
        po.invalidate_recordset(["receipt_status"])
        before = po.receipt_status
        self.importer._recompute_receipt_status(self.ctx)
        self.assertEqual(
            self._status(po),
            before,
            "non-imported PO must not be modified by the xTuple fill",
        )

    # ------------------------------------------------------------------
    # Test plan #7 — multi-line orders
    # ------------------------------------------------------------------

    def test_multi_line_all_met_is_full(self):
        po = self._make_po([(10, 10), (5, 5)], xtuple_id=1007)
        self.importer._recompute_receipt_status(self.ctx)
        self.assertEqual(self._status(po), "full")

    def test_multi_line_one_short_is_partial(self):
        """One fully-received line + one short line -> partial."""
        po = self._make_po([(10, 10), (5, 2)], xtuple_id=1008)
        self.importer._recompute_receipt_status(self.ctx)
        self.assertEqual(self._status(po), "partial")

    def test_multi_line_none_received_is_pending(self):
        po = self._make_po([(10, 0), (5, 0)], xtuple_id=1009)
        self.importer._recompute_receipt_status(self.ctx)
        self.assertEqual(self._status(po), "pending")
