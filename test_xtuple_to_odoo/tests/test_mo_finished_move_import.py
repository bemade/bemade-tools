"""Tests for automatic finished-move generation on imported MOs (task 4119).

Acceptance criteria:
1. An open/in-progress imported MO gets its finished move created, matching
   what ``mrp.production._create_update_move_finished()`` would create for a
   native draft MO (product/qty/uom, source = the product's production
   location, destination = the MO's location_dest_id, linked to the MO), and
   the move is confirmed (not left in draft).
2. (covered under AC1) The generated move appears in ``mo.move_finished_ids``.
3. Re-running the generator is idempotent — no duplicate finished move.
4. Done/cancelled MOs are unaffected (the ~5181 historical Done MOs must not
   gain output), and non-imported (no ``xtuple_wo_id``) MOs are out of scope.

No live xTuple DB — pure Odoo records, mirroring ``test_mo_pick_import.py``.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


@tagged("post_install", "-at_install", "xtuple")
class TestXtupleMrpFinishedMoveGenerator(TransactionCase):
    """Automatic finished-move generation for open/in-progress imported MOs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.generator = cls.env["xtuple.mrp.finished.move.generator"]
        cls.company = cls.env.company

        cls.uom_unit = cls.env.ref("uom.product_uom_unit")

        # Finished product + one storable component, manufacturable.
        cls.finished = cls.env["product.product"].create(
            {"name": "FIN-Finished", "is_storable": True}
        )
        cls.comp = cls.env["product.product"].create(
            {"name": "FIN-Comp", "is_storable": True}
        )
        cls.byproduct = cls.env["product.product"].create(
            {"name": "FIN-Byproduct", "is_storable": True}
        )

        cls.bom = cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.finished.product_tmpl_id.id,
                "product_qty": 1.0,
                "type": "normal",
                "bom_line_ids": [
                    (0, 0, {"product_id": cls.comp.id, "product_qty": 2.0}),
                ],
            }
        )

        cls.bom_with_byproduct = cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.finished.product_tmpl_id.id,
                "product_qty": 1.0,
                "type": "normal",
                "bom_line_ids": [
                    (0, 0, {"product_id": cls.comp.id, "product_qty": 2.0}),
                ],
                "byproduct_ids": [
                    (0, 0, {"product_id": cls.byproduct.id, "product_qty": 1.0}),
                ],
            }
        )

    # ---- fixture helpers -------------------------------------------------

    _next_wo = 2000

    def _make_imported_mo(self, state="progress", bom=None):
        """Create an imported MO mirroring the importer: ``state`` written
        directly, no explicit finished move (let the generator create it, as
        the real import leaves it)."""
        type(self)._next_wo += 1
        wo_id = type(self)._next_wo
        mo = (
            self.env["mrp.production"]
            .with_context(tracking_disable=True)
            .create(
                {
                    "name": f"WO{wo_id}",
                    "product_id": self.finished.id,
                    "product_uom_id": self.uom_unit.id,
                    "product_qty": 1.0,
                    "bom_id": (bom or self.bom).id,
                    "state": state,
                    "xtuple_wo_id": wo_id,
                    "xtuple_wo_number": wo_id,
                }
            )
        )
        return mo

    def _run(self):
        self.generator.generate_finished_moves(_make_ctx(self.env), {})

    def _finished_moves(self, mo, product=None):
        product = product or mo.product_id
        return self.env["stock.move"].search(
            [
                ("production_id", "=", mo.id),
                ("product_id", "=", product.id),
                ("state", "!=", "cancel"),
            ]
        )

    # ---- tests -----------------------------------------------------------

    def test_ac1_finished_move_created(self):
        """AC1: an open imported MO gets exactly one finished move matching
        what the native builder would produce, and it is confirmed."""
        mo = self._make_imported_mo(state="progress")
        self.assertFalse(mo.move_finished_ids, "no finished move should exist pre-run")

        self._run()

        moves = self._finished_moves(mo)
        self.assertEqual(len(moves), 1, "exactly one finished move must be generated")
        move = moves
        self.assertEqual(move.product_id, self.finished)
        self.assertEqual(move.product_uom_qty, mo.product_qty)
        self.assertEqual(move.product_uom, mo.product_uom_id)
        self.assertEqual(
            move.location_id,
            self.finished.with_company(mo.company_id).property_stock_production,
        )
        self.assertEqual(move.location_dest_id, mo.location_dest_id)
        self.assertEqual(move.production_id, mo)
        self.assertNotIn(
            move.state, ("draft", "cancel"), "move must be confirmed, not left draft"
        )
        self.assertTrue(move.picking_type_id, "picking_type_id must resolve")
        self.assertIn(move, mo.move_finished_ids)

    def test_ac3_idempotent_rerun(self):
        """AC3: running the generator twice yields exactly one finished move
        for the MO's product (same id, no duplicate)."""
        mo = self._make_imported_mo(state="progress")
        self._run()
        first = self._finished_moves(mo)
        self.assertEqual(len(first), 1)

        self._run()
        second = self._finished_moves(mo)
        self.assertEqual(
            set(second.ids), set(first.ids), "no second finished move on re-run"
        )
        self.assertEqual(len(second), 1)

    def test_ac4_done_cancel_untouched(self):
        """AC4: done/cancel imported MOs get no finished move from the
        generator (the historical Done MOs must not gain output)."""
        for state in ("done", "cancel"):
            mo = self._make_imported_mo(state=state)
            self._run()
            self.assertFalse(
                self._finished_moves(mo),
                f"{state} MO must not get a finished move generated",
            )

    def test_non_imported_mo_ignored(self):
        """A native (non-imported) MO without xtuple_wo_id is never touched
        by the generator (its scope is strictly imported MOs); its native
        draft finished move is untouched too."""
        native = (
            self.env["mrp.production"]
            .with_context(tracking_disable=True)
            .create(
                {
                    "product_id": self.finished.id,
                    "product_uom_id": self.uom_unit.id,
                    "product_qty": 1.0,
                    "bom_id": self.bom.id,
                }
            )
        )
        self.assertEqual(native.state, "draft")
        native_finished_before = native.move_finished_ids
        self._run()
        self.assertEqual(native.state, "draft")
        self.assertEqual(native.move_finished_ids, native_finished_before)

    def test_state_preserved(self):
        """State preserved: a 'confirmed' and a 'progress' imported MO each
        keep their state after the generator runs."""
        for state in ("confirmed", "progress"):
            mo = self._make_imported_mo(state=state)
            self._run()
            self.assertEqual(mo.state, state)

    def test_byproduct_coverage(self):
        """Byproduct coverage (smoke): a BOM with one byproduct line yields a
        finished move for the main product AND a distinct byproduct finished
        move, not double-counted by the idempotency guard."""
        mo = self._make_imported_mo(state="progress", bom=self.bom_with_byproduct)
        self._run()

        main_moves = self._finished_moves(mo, product=self.finished)
        byproduct_moves = self._finished_moves(mo, product=self.byproduct)
        self.assertEqual(len(main_moves), 1)
        self.assertEqual(len(byproduct_moves), 1)
        self.assertNotEqual(main_moves.id, byproduct_moves.id)

        # Idempotent re-run: byproduct guard only checks the MO's own
        # product, so re-run twice and confirm no duplication of either.
        self._run()
        self.assertEqual(len(self._finished_moves(mo, product=self.finished)), 1)
        self.assertEqual(len(self._finished_moves(mo, product=self.byproduct)), 1)
