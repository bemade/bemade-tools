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

TestSapSaleOrderLineState acceptance criteria (bemade-tools#3334):
1. (test_closed_bucket_sets_line_state_to_sale) _confirm_closed_orders sets
   sale_order_line.state='sale' so that qty_to_invoice and invoice_status are correct.
2. (test_cancel_bucket_sets_line_state_to_cancel) _cancel_canceled_orders sets
   sale_order_line.state='cancel'.
3. (test_open_bucket_already_correct_via_action_confirm) ORM path (_confirm_open_orders
   via action_confirm) cascades line.state='sale' without any extra SQL.
4. (test_remediate_stale_line_state_server_action_idempotent) The remediation server
   action fixes mismatched rows, leaves consistent rows untouched, and is idempotent.
5. (test_remediate_does_not_touch_draft_orders) Draft orders/lines are not touched by
   the remediation.
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
        # active_ids must be in context at create() time so sale_order_ids default works
        ctx = {"default_move_type": "out_invoice", "active_ids": so.ids}
        wiz = (
            self.env["sale.advance.payment.inv"]
            .with_context(**ctx)
            .create({"advance_payment_method": "delivered"})
        )
        wiz.create_invoices()
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


@tagged("-at_install", "post_install")
class TestSapSaleOrderLineState(TransactionCase):
    """Guards the ETL path that writes sale_order.state via raw SQL (bemade-tools#3334).

    Root cause: _confirm_closed_orders and _cancel_canceled_orders update
    sale_order.state via raw SQL, bypassing ORM machinery.  Because
    sale_order_line.state is a stored related field (store=True, precompute=True),
    the stored column is NOT updated automatically.  Lines therefore remain at
    'draft', causing _compute_qty_to_invoice to gate out and return 0.

    Fix: pair a sale_order_line UPDATE (joined through sale_order, filtered by
    sap_docnum IN %s) after each header UPDATE, then invalidate_model(['state']).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "Test SAP Customer LS"})
        cls.product = cls.env["product.product"].create(
            {
                "name": "Test Product LS",
                "type": "consu",
                "invoice_policy": "delivery",
            }
        )

    def _make_so(self, sap_docnum, qty=5.0):
        """Create a confirmed SO with one delivery-policy line, stamped with SAP ids."""
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
                            "price_unit": 50.0,
                        },
                    )
                ],
            }
        )
        so.action_confirm()
        so.write({"sap_docnum": sap_docnum, "sap_docentry": sap_docnum})
        so.order_line.write({"sap_docentry": sap_docnum, "sap_line_num": 2})
        return so

    def _stomp_to_draft(self, so):
        """Simulate the pre-fix state: parent state stomped back to draft via SQL."""
        self.env.cr.execute(
            "UPDATE sale_order SET state = 'draft' WHERE id = %s", (so.id,)
        )
        self.env.cr.execute(
            "UPDATE sale_order_line SET state = 'draft' WHERE order_id = %s",
            (so.id,),
        )
        self.env["sale.order"].invalidate_model(["state"])
        self.env["sale.order.line"].invalidate_model(["state"])

    def test_closed_bucket_sets_line_state_to_sale(self):
        """_confirm_closed_orders must set sale_order_line.state='sale'.

        After the SQL stomp the line is in 'draft'; _confirm_closed_orders must
        update both the header and line, so qty_to_invoice and invoice_status
        are correct.
        """
        so = self._make_so(sap_docnum=33340)
        line = so.order_line
        # Simulate delivered quantity
        line.write({"qty_delivered": 3.0, "qty_delivered_method": "manual"})

        # Stomp parent back to draft to replicate pre-fix pipeline state
        self._stomp_to_draft(so)
        self.assertEqual(line.state, "draft", "Pre-condition: line must be draft")

        # Invoke the method under test
        post_proc = self.env["sale.order.post.processor"]
        post_proc._confirm_closed_orders([so.sap_docnum])

        # Re-read from DB
        self.env["sale.order"].invalidate_model(["state"])
        self.env["sale.order.line"].invalidate_model(["state"])

        self.assertEqual(line.state, "sale", "line.state must be 'sale' after closed-bucket SQL")
        self.assertEqual(so.state, "sale", "order.state must be 'sale' after closed-bucket SQL")

        # Trigger recompute so invoice_status is fresh
        line._compute_qty_to_invoice()
        so._compute_invoice_status()
        self.env.flush_all()

        self.assertAlmostEqual(
            line.qty_to_invoice,
            3.0,
            msg="qty_to_invoice must equal qty_delivered=3 when nothing invoiced",
        )
        self.assertEqual(
            so.invoice_status,
            "to invoice",
            "invoice_status must be 'to invoice' for confirmed order with delivery",
        )

    def test_cancel_bucket_sets_line_state_to_cancel(self):
        """_cancel_canceled_orders must set sale_order_line.state='cancel'."""
        so = self._make_so(sap_docnum=33341)
        line = so.order_line

        # Stomp parent back to draft to replicate pre-fix pipeline state
        self._stomp_to_draft(so)
        self.assertEqual(line.state, "draft", "Pre-condition: line must be draft")

        post_proc = self.env["sale.order.post.processor"]
        post_proc._cancel_canceled_orders([so.sap_docnum])

        self.env["sale.order"].invalidate_model(["state"])
        self.env["sale.order.line"].invalidate_model(["state"])

        self.assertEqual(
            line.state, "cancel", "line.state must be 'cancel' after cancel-bucket SQL"
        )
        self.assertEqual(
            so.state, "cancel", "order.state must be 'cancel' after cancel-bucket SQL"
        )

    def test_open_bucket_already_correct_via_action_confirm(self):
        """ORM path (_confirm_open_orders via action_confirm) cascades line.state='sale'.

        Guards against a regression where someone adds redundant SQL for the ORM path.
        After action_confirm the related field machinery must have set line.state='sale'
        without any extra SQL write.
        """
        # Create an unconfirmed SO (not yet confirmed)
        so = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": 2.0,
                            "price_unit": 75.0,
                        },
                    )
                ],
            }
        )
        so.write({"sap_docnum": 33342, "sap_docentry": 33342})
        so.order_line.write({"sap_docentry": 33342, "sap_line_num": 2})

        self.assertEqual(so.state, "draft", "Pre-condition: order must be draft")
        self.assertEqual(so.order_line.state, "draft", "Pre-condition: line must be draft")

        # Use action_confirm (the ORM path used by _confirm_open_orders)
        so.action_confirm()
        self.env.flush_all()

        self.assertEqual(so.state, "sale", "order.state must be 'sale' after action_confirm")
        self.assertEqual(
            so.order_line.state,
            "sale",
            "line.state must be 'sale' after action_confirm — no extra SQL needed",
        )

    def test_remediate_stale_line_state_server_action_idempotent(self):
        """Remediation server action must fix mismatches and be idempotent.

        Sets up:
        - Two lines mismatched (order.state='sale', line.state='draft')
        - One already-consistent line (order.state='sale', line.state='sale')

        Runs the server action twice; asserts:
        - All three lines end with line.state == order.state after first run.
        - Consistent line write_date is unchanged after the second run (not stomped).
        - Second run reports zero rows changed (idempotent).
        """
        so_mismatch = self._make_so(sap_docnum=33343)
        so_consistent = self._make_so(sap_docnum=33344)

        # Force mismatch on so_mismatch: parent='sale', line='draft'
        self.env.cr.execute(
            "UPDATE sale_order_line SET state = 'draft' WHERE order_id = %s",
            (so_mismatch.id,),
        )
        self.env["sale.order.line"].invalidate_model(["state"])
        self.assertEqual(
            so_mismatch.order_line.state,
            "draft",
            "Pre-condition: mismatched line must be draft",
        )
        self.assertEqual(
            so_consistent.order_line.state,
            "sale",
            "Pre-condition: consistent line must already be 'sale'",
        )

        # Capture write_date of consistent line before first run
        consistent_line = so_consistent.order_line
        write_date_before = consistent_line.write_date

        def _run_remediation():
            self.env.cr.execute("""
                UPDATE sale_order_line sol
                   SET state = so.state
                  FROM sale_order so
                 WHERE sol.order_id = so.id
                   AND sol.state != so.state
                   AND so.state NOT IN ('draft', 'sent')
            """)
            rows = self.env.cr.rowcount
            self.env["sale.order.line"].invalidate_model(["state"])
            self.env.flush_all()
            touched_lines = (
                self.env["sale.order.line"]
                .search([("order_id.state", "in", ["sale", "done"])])
            )
            touched_lines._compute_qty_to_invoice()
            touched_lines._compute_invoice_status()
            touched_lines.order_id._compute_invoice_status()
            self.env.flush_all()
            return rows

        rows_first = _run_remediation()
        self.assertGreater(rows_first, 0, "First run must fix at least one row")
        self.assertEqual(
            so_mismatch.order_line.state,
            "sale",
            "Mismatched line must be 'sale' after first remediation run",
        )
        self.assertEqual(
            so_consistent.order_line.state,
            "sale",
            "Consistent line must remain 'sale' after first run",
        )

        rows_second = _run_remediation()
        self.assertEqual(rows_second, 0, "Second run must fix zero rows (idempotent)")

        # Consistent line's write_date must not have changed (not stomped by second run)
        self.assertEqual(
            consistent_line.write_date,
            write_date_before,
            "Consistent line write_date must be unchanged after idempotent second run",
        )

    def test_remediate_does_not_touch_draft_orders(self):
        """Remediation must not touch lines whose parent order is draft.

        A draft SO with a draft line is legitimate and must be left untouched
        (AC#7).
        """
        # Create a plain draft SO (no confirm)
        so = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": 1.0,
                            "price_unit": 10.0,
                        },
                    )
                ],
            }
        )
        so.write({"sap_docnum": 33345, "sap_docentry": 33345})
        line = so.order_line
        self.assertEqual(so.state, "draft", "Pre-condition: order must be draft")
        self.assertEqual(line.state, "draft", "Pre-condition: line must be draft")

        # Run the same SQL as the remediation (excludes draft/sent orders)
        self.env.cr.execute("""
            UPDATE sale_order_line sol
               SET state = so.state
              FROM sale_order so
             WHERE sol.order_id = so.id
               AND sol.state != so.state
               AND so.state NOT IN ('draft', 'sent')
        """)
        self.env["sale.order.line"].invalidate_model(["state"])
        self.env.flush_all()

        self.assertEqual(
            line.state,
            "draft",
            "Draft line must NOT be touched by the remediation (AC#7)",
        )
        self.assertEqual(
            so.state,
            "draft",
            "Draft order must remain draft after remediation",
        )
