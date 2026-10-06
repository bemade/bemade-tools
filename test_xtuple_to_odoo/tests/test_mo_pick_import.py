"""Tests for automatic component-PICK generation on imported MOs (task 3810).

Acceptance criteria (Marc, 2026-06-18):
1. A FULL import yields open/in-progress MOs WITH their component PICK
   AUTOMATICALLY — no manual re-run.  Realised by ``xtuple.mrp.pick.generator``
   calling the MO's native ``action_confirm()``, which runs the pbm pull rule.
2. The already-imported consumption (raw) moves are NOT regenerated or doubled.
3. PICK generation uses Odoo's native confirm/procurement path (no hand-built
   pick moves, no deliberate import-time no-op).
4. Closed/done/cancel/draft MOs are unaffected.

These tests reproduce the REAL full-import sequence of ``scripts/test_import.py``:
the warehouse cycle is configured to ``pbm`` (pre-import config) BEFORE the MO
and its raw moves are materialised.  So each imported open/in-progress MO already
sits at the pbm source location and its raw moves carry the production group; the
only missing piece — the confirmation that runs procurement — is supplied by the
generator step.

No live xTuple DB — pure Odoo records, mirroring ``test_partner_etl_addresses``.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


@tagged("post_install", "-at_install", "xtuple")
class TestXtupleMrpPickGenerator(TransactionCase):
    """Automatic component-PICK generation for open/in-progress imported MOs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.generator = cls.env["xtuple.mrp.pick.generator"]
        cls.company = cls.env.company

        cls.wh = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        # Configure the warehouse cycle to pbm (2-step manufacturing) BEFORE
        # any MO/raw move is created — exactly as verajet.configurator's
        # pre-import config does ahead of the xTuple import in test_import.py.
        # This auto-provisions pbm_loc_id / pbm_type_id / pbm_route_id + rules.
        cls.wh.write({"manufacture_steps": "pbm"})
        assert cls.wh.pbm_type_id, "pbm warehouse must provision pbm_type_id"
        assert cls.wh.pbm_loc_id, "pbm warehouse must provision pbm_loc_id"

        cls.prod_loc = cls.env["stock.location"].search(
            [("usage", "=", "production"), ("company_id", "=", cls.company.id)],
            limit=1,
        )

        cls.uom_unit = cls.env.ref("uom.product_uom_unit")

        # Finished product + two storable components, all manufacturable.
        cls.finished = cls.env["product.product"].create(
            {"name": "PICK-Finished", "is_storable": True}
        )
        cls.comp_a = cls.env["product.product"].create(
            {"name": "PICK-Comp-A", "is_storable": True}
        )
        cls.comp_b = cls.env["product.product"].create(
            {"name": "PICK-Comp-B", "is_storable": True}
        )

        cls.bom = cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.finished.product_tmpl_id.id,
                "product_qty": 1.0,
                "type": "normal",
                "bom_line_ids": [
                    (0, 0, {"product_id": cls.comp_a.id, "product_qty": 2.0}),
                    (0, 0, {"product_id": cls.comp_b.id, "product_qty": 3.0}),
                ],
            }
        )

    # ---- fixture helpers -------------------------------------------------

    _next_wo = 1000

    def _make_imported_mo(self, state="progress"):
        """Create an imported MO mirroring the importer: ``state`` written
        directly, no explicit ``picking_type_id`` (let the computes resolve it
        against the live pbm warehouse — as the real import does post-config)."""
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
                    "bom_id": self.bom.id,
                    "state": state,
                    "xtuple_wo_id": wo_id,
                    "xtuple_wo_number": wo_id,
                }
            )
        )
        return mo

    def _make_imported_raw_moves(self, mo):
        """Create imported raw moves mirroring the consumption importer:
        ``raw_material_production_id`` + ``location_id = mo.location_src_id``,
        ``xtuple_womatl_id`` set.  (Odoo 19's stock.move.create override forces
        ``location_dest_id`` to the production location and back-propagates the
        MO's production_group_id/reference_ids — same as the real import.)"""
        type(self)._next_wo += 1
        moves = self.env["stock.move"]
        for i, (prod, qty) in enumerate(
            [(self.comp_a, 2.0), (self.comp_b, 3.0)], start=1
        ):
            moves |= (
                self.env["stock.move"]
                .with_context(tracking_disable=True)
                .create(
                    {
                        "raw_material_production_id": mo.id,
                        "product_id": prod.id,
                        "product_uom": self.uom_unit.id,
                        "product_uom_qty": qty,
                        "location_id": mo.location_src_id.id,
                        "location_dest_id": self.prod_loc.id,
                        "company_id": self.company.id,
                        "warehouse_id": self.wh.id,
                        # The real importer sets 'mts_else_mto' (a value the
                        # mrp_mts_else_mto module adds and which is not installed
                        # in this minimal test DB).  The move's own procure_method
                        # is not load-bearing for PICK generation: action_confirm
                        # runs _adjust_procure_method which re-derives it (and the
                        # rule_id) from the live pbm route chain.
                        "procure_method": "make_to_stock",
                        "xtuple_womatl_id": type(self)._next_wo * 10 + i,
                    }
                )
            )
        return moves

    def _run(self):
        self.generator.generate_component_pickings(_make_ctx(self.env), {})

    def _pbm_pickings(self, mo):
        return mo.picking_ids.filtered(
            lambda p: p.picking_type_id == self.wh.pbm_type_id
        )

    # ---- tests -----------------------------------------------------------

    def test_ac1_pick_generated_on_full_import(self):
        """AC1: an open/in-progress imported MO gets exactly one pbm-type PICK
        automatically when the generator step runs (no manual re-run)."""
        mo = self._make_imported_mo(state="progress")
        raws = self._make_imported_raw_moves(mo)

        self.assertFalse(self._pbm_pickings(mo), "no PICK should exist pre-run")
        self._run()

        picks = self._pbm_pickings(mo)
        self.assertEqual(
            len(picks), 1, "exactly one pbm-type PICK must be generated for the MO"
        )
        # The PICK feeds the pbm location (stock -> pre-production) and covers the
        # MO's components.
        pick = picks
        self.assertEqual(pick.location_dest_id, self.wh.pbm_loc_id)
        self.assertEqual(
            set(pick.move_ids.mapped("product_id")),
            set(raws.mapped("product_id")),
            "PICK moves must cover the MO's imported components",
        )

    def test_ac2_raw_moves_not_doubled(self):
        """AC2: the imported raw moves are not regenerated or doubled — the
        same recordset (ids, count, qtys) survives the confirm."""
        mo = self._make_imported_mo(state="progress")
        raws = self._make_imported_raw_moves(mo)
        before_ids = set(raws.ids)
        before_qtys = {m.id: m.product_uom_qty for m in raws}

        self._run()

        after = mo.move_raw_ids
        self.assertEqual(
            set(after.ids), before_ids, "raw-move recordset must be unchanged (no doubling)"
        )
        self.assertEqual(len(after), 2)
        for m in after:
            self.assertEqual(m.product_uom_qty, before_qtys[m.id])
        # No second consumption set created against this MO.
        self.assertEqual(
            self.env["stock.move"].search_count(
                [("raw_material_production_id", "=", mo.id)]
            ),
            2,
        )

    def test_pick_chained_to_raw(self):
        """The generated PICK chains into the MO's raw moves (stock -> pbm ->
        production): each raw move's move_orig_ids includes a pbm pick move."""
        mo = self._make_imported_mo(state="progress")
        raws = self._make_imported_raw_moves(mo)
        self._run()

        for raw in raws:
            origins = raw.move_orig_ids
            self.assertTrue(
                origins, f"raw move {raw.product_id.name} should be fed by a pick move"
            )
            self.assertTrue(
                any(o.picking_type_id == self.wh.pbm_type_id for o in origins),
                "the feeding move must be a pbm-type pick move",
            )

    def test_ac3_native_confirm_state_transition(self):
        """AC3: generation uses the native confirm path.  A 'progress' MO keeps
        its state (action_confirm only flips draft -> confirmed)."""
        mo = self._make_imported_mo(state="progress")
        self._make_imported_raw_moves(mo)
        self._run()
        self.assertEqual(mo.state, "progress")

    def test_confirmed_mo_keeps_state(self):
        """A 'confirmed' imported MO keeps its state and still gets its PICK."""
        mo = self._make_imported_mo(state="confirmed")
        self._make_imported_raw_moves(mo)
        self._run()
        self.assertEqual(mo.state, "confirmed")
        self.assertEqual(len(self._pbm_pickings(mo)), 1)

    def test_ac4_done_cancel_draft_untouched(self):
        """AC4: done / cancel / draft MOs get no PICK from the generator."""
        for state in ("done", "cancel", "draft"):
            mo = self._make_imported_mo(state=state)
            # draft can carry imported raw moves too; done/cancel are terminal.
            if state == "draft":
                self._make_imported_raw_moves(mo)
            self._run()
            self.assertFalse(
                self._pbm_pickings(mo),
                f"{state} MO must not get a component PICK",
            )

    def test_one_step_warehouse_no_pick(self):
        """E2: if the warehouse has no pick step the generator is a no-op."""
        self.wh.write({"manufacture_steps": "mrp_one_step"})
        try:
            mo = self._make_imported_mo(state="progress")
            self._make_imported_raw_moves(mo)
            self._run()
            self.assertFalse(
                mo.picking_ids,
                "1-step manufacture warehouse must yield no component PICK",
            )
        finally:
            self.wh.write({"manufacture_steps": "pbm"})

    def test_idempotent_rerun(self):
        """E3: running the generator twice yields exactly one PICK (no double
        confirm, no duplicate PICK)."""
        mo = self._make_imported_mo(state="progress")
        self._make_imported_raw_moves(mo)
        self._run()
        first = self._pbm_pickings(mo)
        self.assertEqual(len(first), 1)
        # Re-run — must not create a second PICK nor a second pick-move set.
        self._run()
        second = self._pbm_pickings(mo)
        self.assertEqual(set(second.ids), set(first.ids), "no second PICK on re-run")
        self.assertEqual(
            len(second.move_ids), len(first.move_ids), "no duplicate pick moves"
        )

    def test_no_imported_raw_moves_skip(self):
        """E1: an open MO with zero imported raw moves -> no PICK, no error."""
        mo = self._make_imported_mo(state="progress")
        self._run()
        self.assertFalse(self._pbm_pickings(mo))

    def test_non_imported_mo_ignored(self):
        """A native (non-imported) MO without xtuple_wo_id is never touched by
        the generator (its scope is strictly imported MOs)."""
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
        self._run()
        # draft + no xtuple_wo_id: untouched, still draft, no PICK.
        self.assertEqual(native.state, "draft")
        self.assertFalse(
            native.picking_ids.filtered(
                lambda p: p.picking_type_id == self.wh.pbm_type_id
            )
        )
