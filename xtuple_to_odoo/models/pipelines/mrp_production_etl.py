"""xTuple Manufacturing Order ETL Pipeline

This module contains the ETL pipeline for importing work orders
from xTuple as manufacturing orders in Odoo.
"""

import logging
from typing import Dict, List

from odoo import api, models

from odoo.addons.etl_framework.framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# SQL for extracting work orders
SELECT_WORK_ORDERS = """
    SELECT 
        wo_id,
        wo_number,
        wo_subnumber,
        wo_status,
        wo_itemsite_id,
        wo_startdate,
        wo_duedate,
        wo_qtyord,
        wo_qtyrcv,
        wo_prodnotes,
        item_id,
        item_number
    FROM wo
    LEFT JOIN itemsite ON wo_itemsite_id = itemsite_id
    LEFT JOIN item ON itemsite_item_id = item_id
"""


@ETL.pipeline(
    target_model="mrp.production",
    importer_name="xtuple.mrp.production.importer",
    depends_on=[
        "xtuple.product.importer",
        "xtuple.mrp.bom.importer",
    ],
)
class XtupleMrpProductionImporter(models.AbstractModel):
    """ETL Pipeline for importing manufacturing orders from xTuple work orders."""

    _name = "xtuple.mrp.production.importer"
    _description = "xTuple Manufacturing Order Importer"

    @ETL.extract("wo")
    def extract_productions(self, ctx: ETLContext) -> Dict:
        """Extract work orders from xTuple."""
        # Check for existing MOs
        ctx.env.cr.execute(
            "SELECT xtuple_wo_id FROM mrp_production WHERE xtuple_wo_id IS NOT NULL"
        )
        existing_wo_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(f"Found {len(existing_wo_ids)} existing MOs in Odoo")

        # Extract work orders
        if existing_wo_ids:
            ctx.cr.execute(
                SELECT_WORK_ORDERS + " WHERE wo_id NOT IN %s",
                (tuple(existing_wo_ids),),
            )
        else:
            ctx.cr.execute(SELECT_WORK_ORDERS)

        work_orders = ctx.cr.dictfetchall()

        # Get product mapping (uom_id is on product.template in Odoo 19)
        ctx.env.cr.execute(
            """SELECT pp.xtuple_item_id, pp.id, pp.product_tmpl_id, pt.uom_id
               FROM product_product pp
               JOIN product_template pt ON pp.product_tmpl_id = pt.id
               WHERE pp.xtuple_item_id IS NOT NULL"""
        )
        product_map = {
            row[0]: {"id": row[1], "product_tmpl_id": row[2], "uom_id": row[3]}
            for row in ctx.env.cr.fetchall()
        }

        # Get BOM mapping by product template
        ctx.env.cr.execute(
            "SELECT product_tmpl_id, id FROM mrp_bom WHERE product_tmpl_id IS NOT NULL"
        )
        bom_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        _logger.info(f"Extracted {len(work_orders)} new work orders from xTuple")
        return {
            "work_orders": work_orders,
            "product_map": product_map,
            "bom_map": bom_map,
        }

    @ETL.transform()
    def transform_productions(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple work orders into Odoo manufacturing order values."""
        work_orders = extracted.get("extract_productions", {}).get("work_orders", [])
        product_map = extracted.get("extract_productions", {}).get("product_map", {})
        bom_map = extracted.get("extract_productions", {}).get("bom_map", {})

        # Map xTuple status to Odoo state
        # xTuple: O=Open, E=Exploded, R=Released, I=In-Process, C=Closed
        # Odoo: draft, confirmed, progress, to_close, done, cancel
        status_map = {
            "O": "draft",
            "E": "confirmed",
            "R": "confirmed",
            "I": "progress",
            "C": "done",
        }

        production_vals = []
        for wo in work_orders:
            item_id = wo.get("item_id")
            product_info = product_map.get(item_id)

            if not product_info:
                _logger.warning(
                    f"Product not found for WO {wo.get('wo_number')}-{wo.get('wo_subnumber')}, skipping"
                )
                continue

            product_id = product_info["id"]
            product_tmpl_id = product_info["product_tmpl_id"]
            uom_id = product_info["uom_id"]
            bom_id = bom_map.get(product_tmpl_id)

            # Skip WOs with zero/negative quantity (violates qty_positive constraint)
            qty_ord = float(wo.get("wo_qtyord", 0) or 0)
            if qty_ord <= 0:
                _logger.debug(
                    f"Skipping WO {wo.get('wo_number')}-{wo.get('wo_subnumber')} with zero/negative quantity"
                )
                continue

            state = status_map.get(wo.get("wo_status", "").strip(), "draft")

            # Build WO reference number
            wo_number = wo.get("wo_number", "")
            wo_subnumber = wo.get("wo_subnumber", "")
            name = f"WO{wo_number}-{wo_subnumber}" if wo_subnumber else f"WO{wo_number}"

            vals = {
                "name": name,
                "product_id": product_id,
                "product_uom_id": uom_id,
                "product_qty": qty_ord,
                "qty_produced": wo.get("wo_qtyrcv", 0) or 0,
                "bom_id": bom_id,
                "date_start": wo.get("wo_startdate"),
                "date_finished": wo.get("wo_duedate") if state == "done" else False,
                "state": state,
                "xtuple_wo_id": wo.get("wo_id"),
                "xtuple_wo_number": wo.get("wo_number"),
            }
            production_vals.append(vals)

        _logger.info(f"Transformed {len(production_vals)} manufacturing order records")
        return production_vals

    @ETL.load()
    def load_productions(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load manufacturing orders into Odoo."""
        production_vals = transformed.get("transform_productions", [])
        if production_vals:
            productions = (
                ctx.env["mrp.production"]
                .with_context(tracking_disable=True)
                .create(production_vals)
            )
            _logger.info(f"Created {len(productions)} manufacturing orders")


# =============================================================================
# MO Component Lines (womatl -> stock.move)
# =============================================================================

SELECT_WOMATL = """
    SELECT
        womatl_id,
        womatl_wo_id,
        womatl_itemsite_id,
        womatl_qtyreq,
        womatl_qtyiss,
        womatl_bomitem_id,
        womatl_seqnumber,
        womatl_notes,
        item_id,
        item_number
    FROM womatl
    LEFT JOIN itemsite ON womatl_itemsite_id = itemsite_id
    LEFT JOIN item ON itemsite_item_id = item_id
"""


@ETL.pipeline(
    target_model="stock.move",
    importer_name="xtuple.mrp.consumption.importer",
    sap_source="womatl",
    depends_on=[
        "xtuple.mrp.production.importer",
    ],
    chunk_size=500,
)
class XtupleMrpConsumptionImporter(models.AbstractModel):
    """ETL Pipeline for importing MO component lines from xTuple womatl."""

    _name = "xtuple.mrp.consumption.importer"
    _description = "xTuple MO Component Importer"

    @ETL.extract("womatl")
    def extract_womatl(self, ctx: ETLContext) -> List[Dict]:
        """Extract work order material lines from xTuple."""
        # Check for existing stock moves
        ctx.env.cr.execute(
            "SELECT xtuple_womatl_id FROM stock_move WHERE xtuple_womatl_id IS NOT NULL"
        )
        existing_womatl_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(
            f"Found {len(existing_womatl_ids)} existing MO component moves in Odoo"
        )

        # Extract womatl lines
        if existing_womatl_ids:
            ctx.cr.execute(
                SELECT_WOMATL + " WHERE womatl_id NOT IN %s",
                (tuple(existing_womatl_ids),),
            )
        else:
            ctx.cr.execute(SELECT_WOMATL)

        womatl_lines = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(womatl_lines)} new womatl lines from xTuple")
        return womatl_lines

    @ETL.extract("metadata")
    def extract_metadata(self, ctx: ETLContext) -> Dict:
        """Extract lookup data for transform."""
        # Get MO mapping by xTuple wo_id
        ctx.env.cr.execute(
            """SELECT xtuple_wo_id, id, location_src_id, location_dest_id, company_id
               FROM mrp_production WHERE xtuple_wo_id IS NOT NULL"""
        )
        production_map = {
            row[0]: {
                "id": row[1],
                "location_src_id": row[2],
                "location_dest_id": row[3],
                "company_id": row[4],
            }
            for row in ctx.env.cr.fetchall()
        }

        # Get product mapping
        ctx.env.cr.execute(
            """SELECT pp.xtuple_item_id, pp.id, pt.uom_id
               FROM product_product pp
               JOIN product_template pt ON pp.product_tmpl_id = pt.id
               WHERE pp.xtuple_item_id IS NOT NULL"""
        )
        product_map = {
            row[0]: {"id": row[1], "uom_id": row[2]} for row in ctx.env.cr.fetchall()
        }

        # Get production location
        prod_loc = ctx.env["stock.location"].search(
            [("usage", "=", "production"), ("company_id", "=", ctx.env.company.id)],
            limit=1,
        )

        # Get warehouse
        warehouse = ctx.env["stock.warehouse"].search(
            [("company_id", "=", ctx.env.company.id)], limit=1
        )

        # Get the manufacture pull rule for mts_else_mto procurement
        manuf_route = ctx.env["stock.route"].search(
            [("name", "ilike", "manufacture")], limit=1
        )
        manuf_pull_rule = (
            ctx.env["stock.rule"].search(
                [
                    ("route_id", "=", manuf_route.id),
                    ("action", "=", "pull"),
                    ("location_dest_id.usage", "=", "production"),
                ],
                limit=1,
            )
            if manuf_route
            else False
        )

        return {
            "productions": production_map,
            "products": product_map,
            "production_location_id": prod_loc.id if prod_loc else False,
            "manuf_pull_rule_id": manuf_pull_rule.id if manuf_pull_rule else False,
            "warehouse_id": warehouse.id if warehouse else False,
        }

    @ETL.transform()
    def transform_stock_moves(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform womatl lines to stock move vals."""
        womatl_lines = extracted.get("extract_womatl", [])
        metadata = extracted.get("extract_metadata", {})
        production_map = metadata.get("productions", {})
        product_map = metadata.get("products", {})
        prod_loc_id = metadata.get("production_location_id")
        manuf_pull_rule_id = metadata.get("manuf_pull_rule_id")
        warehouse_id = metadata.get("warehouse_id")

        move_vals = []
        skipped_no_mo = 0
        skipped_no_product = 0

        for line in womatl_lines:
            wo_id = line.get("womatl_wo_id")
            item_id = line.get("item_id")
            qty_req = line.get("womatl_qtyreq") or 0.0

            production = production_map.get(wo_id)
            product = product_map.get(item_id)

            if not production:
                skipped_no_mo += 1
                continue

            if not product:
                skipped_no_product += 1
                continue

            if qty_req <= 0:
                continue

            vals = {
                "raw_material_production_id": production["id"],
                "product_id": product["id"],
                "product_uom": product["uom_id"],
                "product_uom_qty": qty_req,
                "location_id": production["location_src_id"] or prod_loc_id,
                "location_dest_id": prod_loc_id,
                "company_id": production["company_id"],
                "warehouse_id": warehouse_id,
                "sequence": line.get("womatl_seqnumber") or 0,
                "procure_method": "mts_else_mto",
                "rule_id": manuf_pull_rule_id,
                "xtuple_womatl_id": line.get("womatl_id"),
            }
            move_vals.append(vals)

        if skipped_no_mo:
            _logger.warning(f"Skipped {skipped_no_mo} womatl lines - MO not found")
        if skipped_no_product:
            _logger.warning(
                f"Skipped {skipped_no_product} womatl lines - product not found"
            )

        _logger.info(f"Transformed {len(move_vals)} component stock moves")
        return move_vals

    @ETL.load()
    def load_stock_moves(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create component consumption stock moves."""
        move_vals = transformed.get("transform_stock_moves", [])
        if not move_vals:
            _logger.info("No component moves to create")
            return

        moves = (
            ctx.env["stock.move"].with_context(tracking_disable=True).create(move_vals)
        )
        _logger.info(f"Created {len(moves)} component stock moves")


# =============================================================================
# Component PICK generation (task 3810)
# =============================================================================
#
# The MO importer (above) writes ``state`` directly and only ``.create()``s the
# MO; the consumption importer ``.create()``s the component (raw) moves.  Neither
# confirms the MO, so std Odoo never runs the manufacture/pbm procurement that
# generates the upstream component PICK (the ``stock -> pre-production`` transfer
# a 2-step ``pbm`` warehouse needs before the components can be picked).
#
# Per the full-import sequence in ``scripts/test_import.py`` the warehouse cycle
# is configured (``manufacture_steps='pbm'``, ``pbm_loc_id``/``pbm_type_id`` and
# the pbm pull rules) by ``verajet.configurator.action_run_pre_import`` *before*
# ``xtuple.database.action_import_all`` runs.  So by the time MOs/raw moves are
# materialised the warehouse is already ``pbm`` and each imported open/in-progress
# MO already has its ``location_src_id`` at the pbm location and its raw moves
# carrying the MO's ``production_group_id`` / ``reference_ids``
# (``mrp/models/stock_move.py`` create override).  The *only* missing piece is
# the confirmation that runs procurement.
#
# This pipeline closes that gap AUTOMATICALLY as the final step of a full import:
# it calls the MO's native ``action_confirm()`` on each open/in-progress imported
# MO.  ``action_confirm`` confirms the MO's *existing* raw moves and runs the pbm
# pull rule via procurement (``_action_confirm(create_proc=True)``), generating
# exactly the component PICK std Odoo would have created on a normal confirm —
# correctly grouped onto ``mo.picking_ids`` because the procurement carries
# ``production_group_id`` (``mrp/models/stock_move._prepare_procurement_values``).
#
# Why this does NOT double the imported raw moves:
#   * ``_compute_move_raw_ids`` early-returns for any non-draft MO
#     (``mrp/models/mrp_production.py``: ``if production.state != 'draft'``), so
#     the BOM is never re-exploded for an imported confirmed/progress/to_close MO.
#   * ``action_confirm`` operates on the MO's *existing* ``move_raw_ids`` — it
#     confirms them, it does not create a second consumption set.
#   * ``stock.move._action_confirm`` skips moves whose state is not ``draft``
#     (``if move.state != 'draft': continue``), so re-running is idempotent at the
#     move level too; and we additionally skip any MO that already has a pbm PICK.
#
# This supersedes the rejected approach (hand-building pick moves + a deliberate
# "no-op at import, re-run after route config"): here PICK generation is native
# and happens automatically within ``action_import_all``, no manual re-run.


@ETL.pipeline(
    target_model="mrp.production",
    importer_name="xtuple.mrp.pick.generator",
    depends_on=[
        "xtuple.mrp.consumption.importer",
    ],
)
class XtupleMrpPickGenerator(models.AbstractModel):
    """Confirm imported open/in-progress MOs so their component PICK is
    generated natively (task 3810).

    Runs after the consumption importer (its component moves must exist first)
    and is auto-included in ``action_import_all`` via the ``xtuple_to_odoo``
    module filter.  Load-only: it has no xTuple payload to extract/transform —
    its work is a DB query over already-imported MOs, which also makes it
    idempotent and safe to re-run.
    """

    _name = "xtuple.mrp.pick.generator"
    _description = "xTuple MO Component PICK Generator"

    # xTuple-status -> Odoo-state mapping (see transform_productions) yields
    # these "open / in-progress" states for MOs that should have a live PICK.
    _OPEN_STATES = ("confirmed", "progress", "to_close")

    @ETL.load()
    def generate_component_pickings(self, ctx: ETLContext, transformed: Dict) -> None:
        """Confirm every open/in-progress imported MO that still lacks its PICK.

        Queries the DB directly (does not trust any transform payload) so the
        step is re-runnable and order-independent.
        """
        productions = ctx.env["mrp.production"].search(
            [
                ("xtuple_wo_id", "!=", False),
                ("state", "in", self._OPEN_STATES),
            ]
        )
        confirmed = 0
        skipped = 0
        for production in productions:
            if self._confirm_imported_mo(ctx, production):
                confirmed += 1
            else:
                skipped += 1
        _logger.info(
            "PICK generation: confirmed %d MO(s), skipped %d already-PICKed/empty",
            confirmed,
            skipped,
        )

    def _confirm_imported_mo(self, ctx: ETLContext, production) -> bool:
        """Confirm one imported MO so Odoo generates its component PICK natively.

        Returns True if the MO was confirmed (PICK generated), False if it was
        skipped (no pick step, already has a PICK, or has no raw moves).
        """
        warehouse = production.warehouse_id

        # Skip if the warehouse has no pick step — std Odoo has no component PICK
        # in a 1-step manufacture warehouse (components are consumed straight from
        # stock).  This is the correct no-op if the cycle was not configured to
        # ``pbm``.  With the test_import.py sequence (pre-import config) the
        # warehouse IS ``pbm`` here, so this does not short-circuit the feature.
        if (
            warehouse.manufacture_steps not in ("pbm", "pbm_sam")
            or not warehouse.pbm_type_id
        ):
            _logger.debug(
                "MO %s: warehouse %s has no pick step — skipping PICK generation",
                production.name,
                warehouse.name,
            )
            return False

        # Idempotency: skip if a pbm-type PICK already exists for this MO.
        # Search stock.move directly (cache-independent within a transaction) for
        # a pbm-type move pointing at this MO's production group with a picking.
        existing_pick_move = ctx.env["stock.move"].search_count(
            [
                ("production_group_id", "=", production.production_group_id.id),
                ("picking_type_id", "=", warehouse.pbm_type_id.id),
                ("picking_id", "!=", False),
            ]
        )
        if existing_pick_move:
            _logger.debug(
                "MO %s already has a %s PICK — skipping",
                production.name,
                warehouse.pbm_type_id.name,
            )
            return False

        # Nothing to wrap if the MO has no imported component moves.
        if not production.move_raw_ids.filtered(lambda m: m.xtuple_womatl_id):
            _logger.debug("MO %s has no imported raw moves — skipping", production.name)
            return False

        # Native confirm: confirms the MO's EXISTING raw moves and runs the pbm
        # pull rule via procurement, generating the component PICK.  Does NOT
        # re-explode the BOM (the MO is non-draft, so _compute_move_raw_ids is a
        # no-op) and does NOT touch the MO's state for non-draft MOs
        # (action_confirm only flips draft -> confirmed).
        production.action_confirm()
        _logger.info(
            "MO %s: confirmed -> generated component PICK on %s",
            production.name,
            warehouse.pbm_type_id.name,
        )
        return True


# =============================================================================
# MO finished-move generation (task 4119)
# =============================================================================
#
# The MO importer writes ``state`` directly (see
# ``XtupleMrpProductionImporter`` above) rather than going through Odoo's
# normal draft -> confirm lifecycle, so the finished-goods move that a native
# confirm/save would create for an open MO is never produced.  Odoo's stored
# compute for that field, ``_compute_move_finished_ids``
# (``mrp/models/mrp_production.py``), is guarded to only (re)build finished
# moves for ``state == 'draft'`` MOs — for any other state it just syncs the
# ``date``/``date_deadline`` on any *existing* finished moves and otherwise
# no-ops.  So an imported ``confirmed``/``progress``/``to_close`` MO is left
# with zero finished moves unless something else creates them.
#
# This pipeline closes that gap using the SAME value-builder Odoo's own
# compute would have used: it calls the MO's native
# ``_create_update_move_finished()`` directly (the helper the compute itself
# delegates to), which in turn calls ``_get_moves_finished_values()`` /
# ``_get_move_finished_values()`` to build the finished move (plus any BOM
# byproduct moves) exactly as a native confirm/save would.  Calling this
# helper directly on a non-draft MO is off Odoo's usual path — normally only
# the draft-guarded compute reaches it — but the helper itself carries no
# state guard, and assigning the resulting moves to ``move_finished_ids``
# routes through ``mrp.production.write``, whose override explicitly handles
# a ``move_finished_ids`` write on a non-draft MO by running
# ``_autoconfirm_production()``.  So the newly created draft finished move(s)
# are auto-confirmed in the same call, landing in the same confirmed state a
# normal confirm would produce.
#
# Runs after the PICK generator (``xtuple.mrp.pick.generator``, task 3810) so
# a full import's MO-related steps stay in one deterministic sequence; the two
# steps are otherwise independent (finished-move creation does not depend on
# the component PICK).  Load-only, like the PICK generator: no xTuple payload
# to extract/transform, just a DB query over already-imported MOs, which also
# makes it idempotent and safe to re-run.


@ETL.pipeline(
    target_model="mrp.production",
    importer_name="xtuple.mrp.finished.move.generator",
    depends_on=[
        "xtuple.mrp.pick.generator",
    ],
)
class XtupleMrpFinishedMoveGenerator(models.AbstractModel):
    """Generate the finished-goods move for imported open/in-progress MOs
    that still lack one (task 4119).

    Runs after the PICK generator and is auto-included in
    ``action_import_all`` via the ``xtuple_to_odoo`` module filter.
    Load-only: it has no xTuple payload to extract/transform — its work is a
    DB query over already-imported MOs, which also makes it idempotent and
    safe to re-run.
    """

    _name = "xtuple.mrp.finished.move.generator"
    _description = "xTuple MO Finished-Move Generator"

    # Same open/in-progress scope as the PICK generator (see
    # transform_productions for the xTuple-status -> Odoo-state mapping):
    # 'done'/'cancel' MOs (e.g. the ~5181 historical Done MOs) must be left
    # untouched, and 'draft' MOs are out of scope for this ETL step (their
    # native draft compute already handles finished moves).
    _OPEN_STATES = ("confirmed", "progress", "to_close")

    @ETL.load()
    def generate_finished_moves(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create the finished move for every open/in-progress imported MO
        that still lacks one.

        Queries the DB directly (does not trust any transform payload) so the
        step is re-runnable and order-independent.
        """
        productions = ctx.env["mrp.production"].search(
            [
                ("xtuple_wo_id", "!=", False),
                ("state", "in", self._OPEN_STATES),
            ]
        )
        generated = 0
        skipped = 0
        for production in productions:
            if self._generate_finished_move(ctx, production):
                generated += 1
            else:
                skipped += 1
        _logger.info(
            "Finished-move generation: generated for %d MO(s), skipped %d "
            "already-present/empty",
            generated,
            skipped,
        )

    def _generate_finished_move(self, ctx: ETLContext, production) -> bool:
        """Create the finished (+ byproduct) move(s) for one imported MO.

        Returns True if a finished move was generated, False if it was
        skipped (a finished move for the MO's product already exists, or the
        MO has no product set).
        """
        if not production.product_id:
            return False

        # Idempotency + Done-scope safety: skip if a (non-cancel) finished
        # move for the MO's own product already exists. Search stock.move
        # directly so it's cache-independent within the transaction (as the
        # PICK generator's existing-PICK guard does).
        existing_finished_move = ctx.env["stock.move"].search_count(
            [
                ("production_id", "=", production.id),
                ("product_id", "=", production.product_id.id),
                ("state", "!=", "cancel"),
            ]
        )
        if existing_finished_move:
            _logger.debug(
                "MO %s already has a finished move for %s — skipping",
                production.name,
                production.product_id.display_name,
            )
            return False

        # Native builder: creates the finished (+ byproduct) moves in draft;
        # the write override then autoconfirms them because the MO is
        # non-draft (mrp.production.write: a move_finished_ids write on a
        # non-draft MO runs _autoconfirm_production()).
        production._create_update_move_finished()
        _logger.info(
            "MO %s: generated finished move(s) for %s",
            production.name,
            production.product_id.display_name,
        )
        return True
