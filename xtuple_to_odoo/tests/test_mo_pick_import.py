"""Tests for the xTuple MO component PICK generator (task 3810).

Acceptance criteria:
1. For open/in-progress imported MOs, the pipeline generates the component
   PICK for the MO's existing imported consumption moves.
2. The already-imported consumption moves are NOT regenerated or doubled —
   the PICK wraps them.
3. action_confirm() is NOT used as the move/PICK generator for these MOs
   (MO state is unchanged).
4. Closed/done/cancel/draft MOs are unaffected.

The fixture reproduces the real frozen-location scenario (design §1e): the MO
and its imported raw moves are created while the warehouse is ``mrp_one_step``
(so ``location_src_id`` freezes to ``lot_stock_id``), THEN the warehouse is
flipped to ``pbm`` (provisioning ``pbm_type_id``/``pbm_loc_id``), THEN the
generator runs.

No live xTuple DB — pure Odoo records, mirroring test_partner_etl_addresses.py.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


@tagged("post_install", "-at_install", "xtuple")
class TestXtupleMrpPickGenerator(TransactionCase):
    """Component PICK generation for open/in-progress imported MOs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.generator = cls.env["xtuple.mrp.pick.generator"]
        cls.company = cls.env.company

        # The warehouse is left at mrp_one_step for MO/raw creation so that
        # location_src_id freezes to lot_stock_id (reproducing design §1e).
        cls.wh = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        cls.wh.write({"manufacture_steps": "mrp_one_step"})

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
        """Create an imported MO mirroring the importer (state written
        directly, no picking_type_id) while the warehouse is 1-step."""
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
        # Reproduce §1e: source location frozen to lot_stock at 1-step.
        self.assertEqual(
            mo.location_src_id,
            self.wh.lot_stock_id,
            "fixture precondition: MO location_src_id must freeze to "
            "lot_stock_id at 1-step (the real frozen-location case)",
        )
        return mo

    def _make_imported_raw_moves(self, mo, products_qty=None):
        """Create imported raw moves mirroring the consumption importer:
        location_id = mo.location_src_id (lot_stock).

        NB: Odoo 19 stock.move.create auto-propagates the MO's
        production_group_id/reference_ids onto a raw move when
        raw_material_production_id is set (mrp/models/stock_move.py:255-260),
        so we do NOT assert the moves lack a group here — the pipeline's
        back-propagation write is defensive/idempotent. What matters is the
        location freezing to lot_stock_id (the §1e scenario), asserted below."""
        if products_qty is None:
            products_qty = [(self.comp_a, 2.0), (self.comp_b, 3.0)]
        womatl_seq = mo.xtuple_wo_id * 10
        moves = self.env["stock.move"]
        for i, (product, qty) in enumerate(products_qty):
            moves |= (
                self.env["stock.move"]
                .with_context(tracking_disable=True)
                .create(
                    {
                        "raw_material_production_id": mo.id,
                        "product_id": product.id,
                        "product_uom": self.uom_unit.id,
                        "product_uom_qty": qty,
                        "location_id": mo.location_src_id.id,
                        "location_dest_id": self.prod_loc.id,
                        "company_id": mo.company_id.id,
                        "warehouse_id": self.wh.id,
                        "xtuple_womatl_id": womatl_seq + i,
                    }
                )
            )
        # §1e precondition: imported raw moves sit at the frozen lot_stock_id.
        for m in moves:
            self.assertEqual(m.location_id, self.wh.lot_stock_id)
        return moves

    def _enable_pbm(self):
        """Flip the warehouse to pbm AFTER the MO/raw moves exist."""
        self.wh.write({"manufacture_steps": "pbm"})
        self.assertTrue(self.wh.pbm_type_id)
        self.assertTrue(self.wh.pbm_loc_id)

    def _run(self):
        self.generator.generate_component_pickings(_make_ctx(self.env), {})

    def _pbm_pickings(self, mo):
        # picking_ids is a non-stored compute keyed on production_group_id;
        # invalidate so we read post-run state, not a cached value.
        mo.invalidate_recordset(["picking_ids"])
        return mo.picking_ids.filtered(
            lambda p: p.picking_type_id == self.wh.pbm_type_id
        )

    # ---- tests -----------------------------------------------------------

    def test_ac1_pick_generated_after_route_config(self):
        """AC1: open MO at a now-pbm warehouse gets exactly one pbm PICK with
        N moves running lot_stock -> pbm_loc."""
        mo = self._make_imported_mo(state="progress")
        raw = self._make_imported_raw_moves(mo)
        self._enable_pbm()

        self._run()

        picks = self._pbm_pickings(mo)
        self.assertEqual(len(picks), 1, "exactly one pbm PICK expected")
        pick_moves = picks.move_ids
        self.assertEqual(len(pick_moves), len(raw))
        for m in pick_moves:
            self.assertEqual(m.location_id, self.wh.lot_stock_id)
            self.assertEqual(m.location_dest_id, self.wh.pbm_loc_id)
            self.assertEqual(m.picking_type_id, self.wh.pbm_type_id)
        self.assertEqual(
            sorted(pick_moves.mapped("product_uom_qty")),
            sorted(raw.mapped("product_uom_qty")),
        )

    def test_raw_location_realigned_to_pbm(self):
        """C1 fix: each imported raw move's location_id is realigned from
        lot_stock_id to pbm_loc_id."""
        mo = self._make_imported_mo(state="progress")
        raw = self._make_imported_raw_moves(mo)
        # Pre-run: every raw move sits at lot_stock (guards masked-bug fixture).
        for m in raw:
            self.assertEqual(m.location_id, self.wh.lot_stock_id)
        self._enable_pbm()

        self._run()

        for m in raw:
            self.assertEqual(m.location_id, self.wh.pbm_loc_id)

    def test_pick_chained_to_raw(self):
        """Each pick move's move_dest_ids == the matching raw move; each raw
        move's move_orig_ids includes its pick move."""
        mo = self._make_imported_mo(state="progress")
        raw = self._make_imported_raw_moves(mo)
        self._enable_pbm()

        self._run()

        pick_moves = self._pbm_pickings(mo).move_ids
        # Every pick move chains to exactly one raw move, all raw moves covered.
        self.assertEqual(pick_moves.move_dest_ids, raw)
        for m in pick_moves:
            self.assertEqual(len(m.move_dest_ids), 1)
            rm = m.move_dest_ids
            self.assertIn(m, rm.move_orig_ids)

    def test_ac2_raw_moves_not_doubled(self):
        """AC2: the raw-move recordset is identical after the run (same ids,
        count, qtys) — no new raw moves created."""
        mo = self._make_imported_mo(state="progress")
        raw = self._make_imported_raw_moves(mo)
        before_ids = set(mo.move_raw_ids.ids)
        before_qtys = sorted(mo.move_raw_ids.mapped("product_uom_qty"))
        self._enable_pbm()

        self._run()

        mo.invalidate_recordset()
        self.assertEqual(set(mo.move_raw_ids.ids), before_ids)
        self.assertEqual(
            sorted(mo.move_raw_ids.mapped("product_uom_qty")), before_qtys
        )
        new_raw = self.env["stock.move"].search(
            [
                ("raw_material_production_id", "=", mo.id),
                ("id", "not in", list(before_ids)),
            ]
        )
        self.assertFalse(new_raw, "no new raw moves should be created")

    def test_ac3_no_action_confirm(self):
        """AC3: MO state is unchanged (no confirm transition)."""
        mo = self._make_imported_mo(state="progress")
        self._make_imported_raw_moves(mo)
        self._enable_pbm()

        self._run()

        mo.invalidate_recordset()
        self.assertEqual(mo.state, "progress")

    def test_ac4_done_cancel_draft_untouched(self):
        """AC4: done/cancel/draft MOs get no PICK and raw moves unchanged."""
        for state in ("done", "cancel", "draft"):
            mo = self._make_imported_mo(state=state)
            raw = self._make_imported_raw_moves(mo)
            before_locs = raw.mapped("location_id")
            self._enable_pbm()

            self._run()

            self.assertFalse(
                self._pbm_pickings(mo), f"{state} MO must get no PICK"
            )
            self.assertEqual(
                raw.mapped("location_id"),
                before_locs,
                f"{state} MO raw locations must be unchanged",
            )
            # reset for next iteration
            self.wh.write({"manufacture_steps": "mrp_one_step"})

    def test_one_step_warehouse_no_pick(self):
        """E2: warehouse left 1-step -> no PICK, no realignment."""
        mo = self._make_imported_mo(state="progress")
        raw = self._make_imported_raw_moves(mo)
        # NB: no _enable_pbm()

        self._run()

        self.assertFalse(self._pbm_pickings(mo))
        for m in raw:
            self.assertEqual(m.location_id, self.wh.lot_stock_id)

    def test_idempotent_rerun(self):
        """E3/§3.0: running twice yields exactly one PICK + N pick moves."""
        mo = self._make_imported_mo(state="progress")
        raw = self._make_imported_raw_moves(mo)
        self._enable_pbm()

        self._run()
        self._run()

        picks = self._pbm_pickings(mo)
        self.assertEqual(len(picks), 1)
        self.assertEqual(len(picks.move_ids), len(raw))
        # raw moves still a single set, all at pbm_loc
        self.assertEqual(len(mo.move_raw_ids), len(raw))
        for m in mo.move_raw_ids:
            self.assertEqual(m.location_id, self.wh.pbm_loc_id)

    def test_anomalous_raw_location_skipped_and_reported(self):
        """E6: a raw move at a third internal location is not realigned, not
        wrapped, and is reported; the well-formed raw moves still get a PICK."""
        mo = self._make_imported_mo(state="progress")
        raw = self._make_imported_raw_moves(mo)
        # Pick one raw move and move it to a third internal location.
        other_loc = self.env["stock.location"].create(
            {
                "name": "PICK-Other-Loc",
                "usage": "internal",
                "location_id": self.wh.view_location_id.id,
                "company_id": self.company.id,
            }
        )
        anomalous = raw[0]
        well_formed = raw[1:]
        anomalous.location_id = other_loc.id
        self._enable_pbm()

        # Attach a detached PipelineReport via a lightweight reporter stub so
        # ctx.report captures the anomaly without ETLReporter.start_run()'s
        # DB commit (forbidden inside a test).
        from odoo.addons.etl_framework.reporter import PipelineReport

        class _ReporterStub:
            def __init__(self):
                self.current = PipelineReport(pipeline_name="test")

        reporter = _ReporterStub()
        ctx = _make_ctx(self.env)
        ctx._reporter = reporter
        self.generator._generate_pick_for_production(ctx, mo)

        # Anomalous move untouched (still at other_loc), not in the PICK.
        self.assertEqual(anomalous.location_id, other_loc)
        picks = self._pbm_pickings(mo)
        self.assertEqual(len(picks), 1)
        self.assertEqual(len(picks.move_ids), len(well_formed))
        self.assertNotIn(anomalous, picks.move_ids.move_dest_ids)
        # Well-formed move realigned + wrapped.
        for m in well_formed:
            self.assertEqual(m.location_id, self.wh.pbm_loc_id)
        # Reported.
        self.assertGreaterEqual(reporter.current.warning_count, 1)

    def test_no_imported_raw_moves_skip(self):
        """E1: open MO with zero imported raw moves -> no PICK, no error."""
        mo = self._make_imported_mo(state="progress")
        # No imported raw moves created.
        self._enable_pbm()

        self._run()

        self.assertFalse(self._pbm_pickings(mo))

    def test_production_group_backpropagated(self):
        """R5: imported raw moves carry the MO's production_group_id and
        reference_ids after the run (the picking_ids linkage)."""
        mo = self._make_imported_mo(state="progress")
        raw = self._make_imported_raw_moves(mo)
        self.assertTrue(mo.production_group_id)
        self._enable_pbm()

        self._run()

        for m in raw:
            self.assertEqual(m.production_group_id, mo.production_group_id)
            self.assertEqual(m.reference_ids, mo.reference_ids)
        # And the pick moves too.
        for m in self._pbm_pickings(mo).move_ids:
            self.assertEqual(m.production_group_id, mo.production_group_id)
