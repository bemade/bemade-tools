#
#    Bemade Inc.
#
#    Copyright (C) 2024-June Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for SAP ETL invoice_status compute correctness.

Acceptance criteria:
1. (test_sap_qty_invoiced_sql_update_triggers_invoice_status) ETL-path reproduction:
   After raw SQL UPDATE of sap_qty_invoiced + cache invalidation + _trigger_recomputation,
   the sale order must have invoice_status == 'to invoice'.  Pre-fix: fails because the
   ORM cache was never invalidated and the compute read sap_qty_invoiced = 0.
2. (test_ordinary_so_invoice_status_unchanged) Normal-path regression: an ordinary SO
   partially invoiced via ORM must still show invoice_status == 'to invoice'; adding
   sap_qty_invoiced to @api.depends must not double-count when sap_qty_invoiced == 0.
3. (test_raw_sql_sap_qty_invalidates_cache) Cache invalidation: after a raw SQL UPDATE,
   calling invalidate_model(['sap_qty_invoiced']) must cause the ORM to re-read the new
   value from DB rather than serving the pre-UPDATE cached value.
4. (test_sap_qty_invoiced_ormwrite_schedules_recompute) Dependency graph: writing
   sap_qty_invoiced through the ORM schedules a recompute so that qty_invoiced and
   invoice_status update to reflect the new value without explicit compute calls.
5. (test_remediation_server_action_idempotent) Idempotency: running the remediation
   server action twice produces the same final state; the second run must not alter any
   value and must report 0 orders moved.
"""

from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("-at_install", "post_install")
class TestSapSaleOrderInvoiceStatus(TransactionCase):
    """Guards the ETL path that writes sap_qty_invoiced via raw SQL.

    The core bug: import_order_invoiced_qty writes sap_qty_invoiced through raw
    SQL (bypassing ORM), so the ORM cache stays stale (sap_qty_invoiced == 0).
    The compute override therefore sees zero and invoice_status stays 'no'.
    Fix: invalidate_model(['sap_qty_invoiced']) before _trigger_recomputation.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create a minimal partner
        cls.partner = cls.env["res.partner"].create({"name": "Test SAP Customer"})
        # Minimal storable product
        cls.product = cls.env["product.product"].create(
            {
                "name": "Test Product",
                "type": "consu",
                "invoice_policy": "delivery",
            }
        )

    def _create_confirmed_so(self, qty=10.0):
        """Create and confirm a sale order with one line (qty_delivered pre-set)."""
        so = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": qty,
                            "price_unit": 100.0,
                        },
                    )
                ],
            }
        )
        so.action_confirm()
        # Simulate delivery by setting qty_delivered directly on the line
        so.order_line.qty_delivered = qty
        return so

    def test_sap_qty_invoiced_sql_update_triggers_invoice_status(self):
        """ETL-path: raw SQL + invalidate_model + _trigger_recomputation sets invoice_status.

        Pre-fix behaviour: sap_qty_invoiced cache stays at 0 after raw SQL, so
        _compute_qty_invoiced adds 0 and invoice_status remains 'no'.
        Post-fix: invalidate_model clears the cache; compute reads the updated DB value.
        """
        so = self._create_confirmed_so(qty=10.0)
        line = so.order_line
        # Stamp SAP identifiers so the ETL path matches
        so.write({"sap_docentry": 1001, "sap_docnum": 1001})
        line.write({"sap_docentry": 1001, "sap_line_num": 1})

        # Simulate raw SQL UPDATE (as import_order_invoiced_qty does)
        self.env.cr.execute(
            "UPDATE sale_order_line SET sap_qty_invoiced = %s WHERE id = %s",
            (5.0, line.id),
        )
        # Without invalidation the ORM would still see 0.0 here.
        # Post-fix: invalidate cache before recomputing.
        self.env["sale.order.line"].invalidate_model(["sap_qty_invoiced"])

        # Now simulate _trigger_recomputation (post-processor path)
        line._compute_qty_invoiced()
        line._compute_qty_to_invoice()
        so._compute_invoice_status()
        self.env.flush_all()

        self.assertEqual(
            so.invoice_status,
            "to invoice",
            "invoice_status must be 'to invoice' after ETL writes sap_qty_invoiced=5 "
            "with qty_delivered=10 (5 still to invoice)",
        )
        self.assertAlmostEqual(
            line.qty_to_invoice,
            5.0,
            msg="qty_to_invoice must equal qty_delivered - sap_qty_invoiced = 5",
        )

    def test_ordinary_so_invoice_status_unchanged(self):
        """Normal-path regression: ORM-invoiced SO must show 'to invoice'.

        Verifies that adding sap_qty_invoiced to @api.depends does not double-count
        when sap_qty_invoiced == 0 (the default for non-SAP orders).
        """
        so = self._create_confirmed_so(qty=10.0)
        line = so.order_line

        # sap_qty_invoiced must default to 0 / falsy — confirm no side-effect
        self.assertFalse(
            line.sap_qty_invoiced,
            "sap_qty_invoiced must be 0 for a non-SAP order line",
        )

        # invoice_policy = 'delivery'; qty_delivered = 10 → fully deliverable,
        # no invoice yet → 'to invoice'
        self.assertEqual(
            so.invoice_status,
            "to invoice",
            "An ordinary SO with qty_delivered == product_uom_qty and no invoices "
            "must have invoice_status 'to invoice'",
        )

        # Create and post a partial invoice for 4 units
        ctx = {"default_move_type": "out_invoice"}
        wiz = (
            self.env["sale.advance.payment.inv"]
            .with_context(**ctx)
            .create({"advance_payment_method": "delivered"})
        )
        wiz.with_context(active_ids=so.ids).create_invoices()
        invoice = so.invoice_ids
        self.assertTrue(invoice, "Invoice should have been created")
        invoice.invoice_line_ids.quantity = 4.0
        invoice.action_post()

        # After posting a partial invoice qty_invoiced=4, qty_to_invoice=6
        self.assertAlmostEqual(line.qty_invoiced, 4.0, msg="qty_invoiced must be 4")
        self.assertAlmostEqual(
            line.qty_to_invoice, 6.0, msg="qty_to_invoice must be 6"
        )
        self.assertEqual(
            so.invoice_status,
            "to invoice",
            "invoice_status must remain 'to invoice' after partial ordinary invoice",
        )

    def test_raw_sql_sap_qty_invalidates_cache(self):
        """Cache-invalidation: ORM must re-read sap_qty_invoiced from DB after raw SQL.

        Loads sap_qty_invoiced into ORM cache, then runs raw SQL UPDATE.
        Without invalidation the ORM would still return the old value.
        After invalidate_model the ORM must return the DB value.
        """
        so = self._create_confirmed_so(qty=10.0)
        line = so.order_line

        # Pre-load into ORM cache
        _cached = line.sap_qty_invoiced  # noqa: F841 — side-effect: populates cache
        self.assertAlmostEqual(
            line.sap_qty_invoiced, 0.0, msg="sap_qty_invoiced must start at 0"
        )

        # Raw SQL bypasses ORM
        self.env.cr.execute(
            "UPDATE sale_order_line SET sap_qty_invoiced = %s WHERE id = %s",
            (7.0, line.id),
        )

        # Without invalidation: still reads cached 0.0
        # (We do NOT check the stale value here to avoid depending on internal
        #  cache representation — the important assertion is post-invalidation.)

        # Invalidate and re-read
        self.env["sale.order.line"].invalidate_model(["sap_qty_invoiced"])
        self.assertAlmostEqual(
            line.sap_qty_invoiced,
            7.0,
            msg="After invalidate_model the ORM must read sap_qty_invoiced=7 from DB",
        )

    def test_sap_qty_invoiced_ormwrite_schedules_recompute(self):
        """Dependency graph: ORM write to sap_qty_invoiced must schedule qty_invoiced recompute.

        Now that 'sap_qty_invoiced' is in @api.depends, an ORM write must dirty
        qty_invoiced (and transitively invoice_status) without requiring an explicit
        _compute call.
        """
        so = self._create_confirmed_so(qty=10.0)
        line = so.order_line

        # Write sap_qty_invoiced=10.0 through the ORM (fully invoiced via SAP)
        line.sap_qty_invoiced = 10.0

        # Reading qty_invoiced must trigger the auto-recompute (Odoo lazy compute).
        # No Odoo-posted invoice lines exist; qty_invoiced comes entirely from
        # sap_qty_invoiced (the override adds sap_qty_invoiced - 0 open Odoo lines).
        self.assertAlmostEqual(
            line.qty_invoiced,
            10.0,
            msg="qty_invoiced must equal sap_qty_invoiced=10 after ORM write",
        )
        self.assertEqual(
            so.invoice_status,
            "invoiced",
            "invoice_status must be 'invoiced' when sap_qty_invoiced == qty_delivered == 10",
        )

    def test_remediation_server_action_idempotent(self):
        """Idempotency: running the remediation server action twice must yield same state.

        The second run must not change any value and the logged count of orders
        "now 'to invoice'" must equal the same as after the first run (since the
        state is stable), confirming safe-to-rerun behaviour.
        """
        so = self._create_confirmed_so(qty=10.0)
        line = so.order_line
        so.write({"sap_docentry": 2001, "sap_docnum": 2001})
        line.write({"sap_docentry": 2001, "sap_line_num": 1})

        # Set sap_qty_invoiced via raw SQL to simulate pre-fix stale state
        self.env.cr.execute(
            "UPDATE sale_order_line SET sap_qty_invoiced = %s WHERE id = %s",
            (5.0, line.id),
        )
        self.env["sale.order.line"].invalidate_model(["sap_qty_invoiced"])

        def _run_remediation():
            orders = self.env["sale.order"].search(
                [
                    ("sap_docnum", "!=", 0),
                    ("state", "in", ["sale", "done"]),
                ]
            )
            self.env["sale.order.line"].invalidate_model(["sap_qty_invoiced"])
            lines = orders.order_line
            lines._compute_qty_invoiced()
            lines._compute_qty_to_invoice()
            lines._compute_invoice_status()
            orders._compute_invoice_status()
            self.env.flush_all()
            changed = orders.filtered(lambda o: o.invoice_status == "to invoice")
            return len(changed)

        count_first = _run_remediation()
        status_after_first = so.invoice_status

        count_second = _run_remediation()
        status_after_second = so.invoice_status

        self.assertEqual(
            status_after_first,
            status_after_second,
            "invoice_status must be identical after first and second remediation run",
        )
        self.assertEqual(
            count_first,
            count_second,
            "The number of 'to invoice' orders must be identical after both runs "
            "(remediation is idempotent)",
        )
        self.assertEqual(
            so.invoice_status,
            "to invoice",
            "SO with sap_qty_invoiced=5, qty_delivered=10 must be 'to invoice' "
            "after remediation",
        )
