#
#    Bemade Inc.
#
#    Copyright (C) 2026-September Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for closed-order detection in the sale/purchase order post-processors.

SAP can close a document (DocStatus=C) without closing its inventory status
(InvntSttus=O). The post-processors must treat DocStatus as the sole source
of truth for "closed", independent of InvntSttus and of any date cutoff, so
that a document closed in SAP always lands correctly on import regardless of
when it was created.

Acceptance criteria:

1. (test_po_closed_bucket_uses_docstatus_only,
   test_so_closed_bucket_uses_docstatus_only) `extract_sap_order_data`
   assigns a source row to the closed bucket by `docstatus='C' AND
   canceled='N' AND confirmed='Y'` alone — never by `invntsttus`, never by a
   date cutoff. The closed, open and canceled buckets stay pairwise
   disjoint, and an unconfirmed closed document (`confirmed='N'`) is never
   treated as closed or open.
2. (test_po_post_process_closed_mismatch_lands_full_and_invoiced) A
   `docstatus='C'/invntsttus='O'` purchase order gets no receipt picking,
   full received quantity, `receipt_status='full'` and
   `invoice_status='invoiced'` after post-processing — matching the
   already-`docstatus='C'/invntsttus='C'` case, and distinct from an open
   order (which keeps a live receipt) and a canceled order (which stays
   `state='cancel'`).
3. (test_so_post_process_closed_mismatch_lands_full) Same shape on the
   sale-order side: `delivery_status='full'`, no delivery picking, and the
   canceled/open controls behave as before.
4. (test_po_invoice_status_pin_survives_orm_recompute) The `invoice_status`
   pin on a closed, confirmed purchase order survives a later ORM write that
   re-triggers the `_get_invoiced` compute (not just the one-time SQL
   forcing done by the post-processor).
"""

import contextlib
from unittest.mock import MagicMock

from odoo.fields import Command
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def _make_ctx(env):
    ctx = MagicMock()
    ctx.env = env
    ctx.cr = env.cr
    ctx.skippable = lambda *a, **k: contextlib.nullcontext()
    return ctx


@tagged("-at_install", "post_install", "order_post_processor")
class TestClosedOrderBucketPO(TransactionCase):
    """Bucket-assignment predicate on `purchase.order.post.processor`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["purchase.order.post.processor"]

    def setUp(self):
        super().setUp()
        self.patch(self.env.cr, "commit", lambda: None)
        self.env.cr.execute(
            """
            CREATE TEMP TABLE opor (
                docnum integer,
                docentry integer,
                docstatus char(1),
                invntsttus char(1),
                canceled char(1),
                confirmed char(1),
                docdate date,
                createdate date
            ) ON COMMIT DROP
            """
        )
        self.env.cr.execute(
            """
            CREATE TEMP TABLE por1 (
                docentry integer,
                itemcode varchar,
                linenum integer,
                quantity numeric,
                openqty numeric
            ) ON COMMIT DROP
            """
        )

    def _insert(self, docnum, docstatus, invntsttus, canceled, confirmed):
        self.env.cr.execute(
            """
            INSERT INTO opor
                (docnum, docentry, docstatus, invntsttus, canceled, confirmed,
                 docdate, createdate)
            VALUES (%s, %s, %s, %s, %s, %s, '2026-01-01', '2026-01-01')
            """,
            (docnum, docnum, docstatus, invntsttus, canceled, confirmed),
        )

    def test_po_closed_bucket_uses_docstatus_only(self):
        rows = {
            "cc_ny": (1, "C", "C", "N", "Y"),
            "co_ny": (2, "C", "O", "N", "Y"),
            "co_yy": (3, "C", "O", "Y", "Y"),
            "cc_yy": (4, "C", "C", "Y", "Y"),
            "oo_ny": (5, "O", "O", "N", "Y"),
            "co_nn": (6, "C", "O", "N", "N"),
        }
        for docnum, docstatus, invntsttus, canceled, confirmed in rows.values():
            self._insert(docnum, docstatus, invntsttus, canceled, confirmed)

        extracted = self.importer.extract_sap_order_data(_make_ctx(self.env))
        closed = set(extracted["closed_orders"])
        open_ = set(extracted["open_orders"])
        canceled = set(extracted["canceled_orders"])

        self.assertEqual(
            closed,
            {rows["cc_ny"][0], rows["co_ny"][0]},
            "closed bucket must be docstatus='C' AND canceled='N' AND "
            "confirmed='Y', regardless of invntsttus",
        )
        self.assertEqual(
            open_,
            {rows["oo_ny"][0]},
            "open bucket must require docstatus='O' AND invntsttus='O'; "
            "docstatus='C' rows must never appear here",
        )
        self.assertIn(rows["co_yy"][0], canceled)
        self.assertIn(rows["cc_yy"][0], canceled)
        self.assertNotIn(rows["co_yy"][0], closed)
        self.assertNotIn(rows["cc_yy"][0], closed)

        self.assertEqual(closed & open_, set(), "closed/open must be disjoint")
        self.assertEqual(closed & canceled, set(), "closed/canceled must be disjoint")
        self.assertEqual(open_ & canceled, set(), "open/canceled must be disjoint")

        unconfirmed = rows["co_nn"][0]
        self.assertIn(
            unconfirmed,
            canceled,
            "an unconfirmed closed document must fall into the canceled "
            "bucket, not closed or open",
        )
        self.assertNotIn(unconfirmed, closed)
        self.assertNotIn(unconfirmed, open_)


@tagged("-at_install", "post_install", "order_post_processor")
class TestClosedOrderBucketSO(TransactionCase):
    """Bucket-assignment predicate on `sale.order.post.processor`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["sale.order.post.processor"]

    def setUp(self):
        super().setUp()
        self.patch(self.env.cr, "commit", lambda: None)
        self.env.cr.execute(
            """
            CREATE TEMP TABLE ordr (
                docnum integer,
                docentry integer,
                docstatus char(1),
                invntsttus char(1),
                canceled char(1),
                confirmed char(1),
                docdate date,
                createdate date
            ) ON COMMIT DROP
            """
        )
        self.env.cr.execute(
            """
            CREATE TEMP TABLE rdr1 (
                docentry integer,
                itemcode varchar,
                linenum integer,
                quantity numeric,
                openqty numeric
            ) ON COMMIT DROP
            """
        )

    def _insert(self, docnum, docstatus, invntsttus, canceled, confirmed):
        self.env.cr.execute(
            """
            INSERT INTO ordr
                (docnum, docentry, docstatus, invntsttus, canceled, confirmed,
                 docdate, createdate)
            VALUES (%s, %s, %s, %s, %s, %s, '2026-01-01', '2026-01-01')
            """,
            (docnum, docnum, docstatus, invntsttus, canceled, confirmed),
        )

    def test_so_closed_bucket_uses_docstatus_only(self):
        rows = {
            "cc_ny": (11, "C", "C", "N", "Y"),
            "co_ny": (12, "C", "O", "N", "Y"),
            "co_yy": (13, "C", "O", "Y", "Y"),
            "cc_yy": (14, "C", "C", "Y", "Y"),
            "oo_ny": (15, "O", "O", "N", "Y"),
            "co_nn": (16, "C", "O", "N", "N"),
        }
        for docnum, docstatus, invntsttus, canceled, confirmed in rows.values():
            self._insert(docnum, docstatus, invntsttus, canceled, confirmed)

        extracted = self.importer.extract_sap_order_data(_make_ctx(self.env))
        closed = set(extracted["closed_orders"])
        open_ = set(extracted["open_orders"])
        canceled = set(extracted["canceled_orders"])

        self.assertEqual(closed, {rows["cc_ny"][0], rows["co_ny"][0]})
        self.assertEqual(open_, {rows["oo_ny"][0]})
        self.assertEqual(closed & open_, set())
        self.assertEqual(closed & canceled, set())
        self.assertEqual(open_ & canceled, set())

        unconfirmed = rows["co_nn"][0]
        self.assertIn(unconfirmed, canceled)
        self.assertNotIn(unconfirmed, closed)
        self.assertNotIn(unconfirmed, open_)


@tagged("-at_install", "post_install", "order_post_processor")
class TestPurchaseOrderClosedMismatchPostProcess(TransactionCase):
    """End state of `post_process_orders` for a docstatus='C'/invntsttus='O' PO."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["purchase.order.post.processor"]
        cls.vendor = cls.env["res.partner"].create({"name": "Closed-Mismatch Vendor"})
        cls.product = cls.env["product.product"].create(
            {
                "name": "Closed-Mismatch Product",
                "type": "consu",
                "is_storable": True,
            }
        )

    def setUp(self):
        super().setUp()
        self.patch(self.env.cr, "commit", lambda: None)

    def _make_po(self, docnum, sap_docstatus, qty=5.0):
        return self.env["purchase.order"].create(
            {
                "partner_id": self.vendor.id,
                "sap_docnum": docnum,
                "sap_docentry": docnum,
                "sap_docstatus": sap_docstatus,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product.id,
                            "name": self.product.name,
                            "product_qty": qty,
                            "price_unit": 1.0,
                            "sap_line_num": 2,
                        }
                    )
                ],
            }
        )

    def test_po_post_process_closed_mismatch_lands_full_and_invoiced(self):
        co_po = self._make_po(70001, "C")  # docstatus='C', invntsttus='O' case
        cc_po = self._make_po(70002, "C")  # docstatus='C', invntsttus='C' case
        canceled_po = self._make_po(70003, "C")
        oo_po = self._make_po(70004, "O")

        closed_docnums = [70001, 70002]
        canceled_docnums = [70003]
        open_docnums = [70004]

        ctx = _make_ctx(self.env)

        self.importer._confirm_closed_orders(closed_docnums)
        self.importer._set_delivered_qty_for_closed_orders(closed_docnums)
        self.importer._pin_closed_order_invoice_status(closed_docnums)
        self.importer._confirm_open_orders(ctx, open_docnums)
        self.importer._cancel_canceled_orders(canceled_docnums)
        self.importer._recompute_receipt_status()

        for po in (co_po, cc_po, canceled_po, oo_po):
            po.invalidate_recordset()

        for po in (co_po, cc_po):
            self.assertEqual(po.state, "purchase")
            self.assertFalse(
                po.picking_ids, "a closed order must get no receipt picking"
            )
            self.assertEqual(po.order_line.qty_received, po.order_line.product_qty)
            self.assertEqual(po.receipt_status, "full")
            self.assertEqual(po.invoice_status, "invoiced")

        self.assertEqual(canceled_po.state, "cancel")
        self.assertNotEqual(canceled_po.receipt_status, "full")
        self.assertEqual(canceled_po.invoice_status, "no")

        self.assertEqual(oo_po.state, "purchase")
        self.assertTrue(oo_po.picking_ids, "an open order must get a receipt picking")
        self.assertNotEqual(oo_po.receipt_status, "full")
        self.assertNotEqual(oo_po.invoice_status, "invoiced")


@tagged("-at_install", "post_install", "order_post_processor")
class TestSaleOrderClosedMismatchPostProcess(TransactionCase):
    """End state of `post_process_orders` for a docstatus='C'/invntsttus='O' SO."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["sale.order.post.processor"]
        cls.customer = cls.env["res.partner"].create({"name": "Closed-Mismatch Cust"})
        cls.product = cls.env["product.product"].create(
            {
                "name": "Closed-Mismatch SO Product",
                "type": "consu",
                "is_storable": True,
            }
        )

    def setUp(self):
        super().setUp()
        self.patch(self.env.cr, "commit", lambda: None)

    def _make_so(self, docnum, sap_docstatus, qty=5.0):
        return self.env["sale.order"].create(
            {
                "partner_id": self.customer.id,
                "sap_docnum": docnum,
                "sap_docentry": docnum,
                "sap_docstatus": sap_docstatus,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": qty,
                            "sap_line_num": 2,
                        }
                    )
                ],
            }
        )

    def test_so_post_process_closed_mismatch_lands_full(self):
        co_so = self._make_so(80001, "C")  # docstatus='C', invntsttus='O' case
        canceled_so = self._make_so(80002, "C")
        oo_so = self._make_so(80003, "O")

        closed_docnums = [80001]
        canceled_docnums = [80002]
        open_docnums = [80003]

        ctx = _make_ctx(self.env)

        self.importer._confirm_closed_orders(closed_docnums)
        self.importer._set_delivered_qty_for_closed_orders(closed_docnums)
        self.importer._confirm_open_orders(ctx, open_docnums)
        self.importer._cancel_canceled_orders(canceled_docnums)
        self.importer._recompute_delivery_status()

        for so in (co_so, canceled_so, oo_so):
            so.invalidate_recordset()

        self.assertEqual(co_so.state, "sale")
        self.assertFalse(co_so.picking_ids, "a closed order must get no delivery picking")
        self.assertEqual(co_so.order_line.qty_delivered, co_so.order_line.product_uom_qty)
        self.assertEqual(co_so.delivery_status, "full")
        self.assertEqual(co_so.invoice_status, "invoiced")

        self.assertEqual(canceled_so.state, "cancel")
        self.assertNotEqual(canceled_so.delivery_status, "full")

        self.assertTrue(oo_so.picking_ids, "an open order must get a delivery picking")


@tagged("-at_install", "post_install", "order_post_processor")
class TestPurchaseOrderInvoiceStatusPin(TransactionCase):
    """The invoice_status pin must survive a later ORM-triggered recompute."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["purchase.order.post.processor"]
        cls.vendor = cls.env["res.partner"].create({"name": "Pin Vendor"})
        cls.product = cls.env["product.product"].create(
            {
                "name": "Pin Product",
                "type": "consu",
                "is_storable": True,
                "purchase_method": "receive",
            }
        )

    def setUp(self):
        super().setUp()
        self.patch(self.env.cr, "commit", lambda: None)

    def _make_confirmed_po(self, docnum, sap_docstatus, qty=5.0):
        po = self.env["purchase.order"].create(
            {
                "partner_id": self.vendor.id,
                "sap_docnum": docnum,
                "sap_docentry": docnum,
                "sap_docstatus": sap_docstatus,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product.id,
                            "name": self.product.name,
                            "product_qty": qty,
                            "price_unit": 1.0,
                            "sap_line_num": 2,
                        }
                    )
                ],
            }
        )
        po.button_confirm()
        return po

    def test_po_invoice_status_pin_survives_orm_recompute(self):
        po = self._make_confirmed_po(90001, "C")
        po.order_line.write(
            {"qty_received": po.order_line.product_qty, "qty_received_method": "manual"}
        )
        self.assertEqual(po.invoice_status, "invoiced")

        # A later ORM write that re-triggers _get_invoiced must not revert the pin.
        po.order_line.write({"qty_received": po.order_line.product_qty - 1.0})
        po.order_line.write(
            {"qty_received": po.order_line.product_qty, "qty_received_method": "manual"}
        )
        self.assertEqual(
            po.invoice_status,
            "invoiced",
            "the pin must be reproduced by a later compute, not just set once",
        )

        open_po = self._make_confirmed_po(90002, "O")
        open_po.order_line.write(
            {
                "qty_received": open_po.order_line.product_qty,
                "qty_received_method": "manual",
            }
        )
        self.assertNotEqual(
            open_po.invoice_status,
            "invoiced",
            "sap_docstatus='O' must not be pinned",
        )
        self.assertEqual(open_po.invoice_status, "to invoice")

        canceled_po = self._make_confirmed_po(90003, "C")
        canceled_po.button_cancel()
        self.assertEqual(
            canceled_po.invoice_status,
            "no",
            "a canceled order must not be pinned to invoiced even with sap_docstatus='C'",
        )
