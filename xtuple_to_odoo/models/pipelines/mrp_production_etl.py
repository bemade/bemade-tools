"""xTuple Manufacturing Order ETL Pipeline

This module contains the ETL pipeline for importing work orders
from xTuple as manufacturing orders in Odoo.
"""

import logging
from typing import Dict, List

from odoo import api, models
from odoo.fields import Command

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
# Component PICK generator (task 3810)
# =============================================================================
#
# xTuple-imported MOs are created without their component PICK because the MO
# importer writes ``state`` directly and never calls ``action_confirm()`` /
# ``button_plan()`` (which would regenerate/double the separately-imported raw
# moves). For a multi-step manufacture warehouse (``manufacture_steps`` in
# ``('pbm', 'pbm_sam')``) the component PICK is a *separate* set of stock moves
# running ``lot_stock -> pbm_loc`` (picking type ``pbm_type_id``) whose
# ``move_dest_ids`` chain forward into the MO's raw moves (``pbm_loc ->
# production``). This pipeline generates that missing PICK for open/in-progress
# imported MOs, wrapping the already-imported raw moves without confirming the
# MO.
#
# It is its own pipeline (``depends_on=['xtuple.mrp.consumption.importer']``)
# so the executor runs it AFTER the consumption moves are loaded and committed,
# and it is idempotent + re-runnable: the real route config happens AFTER the
# migration, so the first ``action_import_all`` run is a correct no-op (every
# warehouse is still 1-step) and the PICKs are created by RE-RUNNING the step
# after the warehouse is flipped to ``pbm``.


@ETL.pipeline(
    target_model="stock.picking",
    importer_name="xtuple.mrp.pick.generator",
    depends_on=[
        "xtuple.mrp.consumption.importer",
    ],
)
class XtupleMrpPickGenerator(models.AbstractModel):
    """Generate the component PICK for open/in-progress imported MOs.

    Wraps the already-imported raw (consumption) moves in an upstream
    ``pbm`` PICK without confirming the MO, so imported MOs become actionable
    after route config without manual re-confirmation. Idempotent and
    re-runnable.
    """

    _name = "xtuple.mrp.pick.generator"
    _description = "xTuple MO Component PICK Generator"

    # In-scope MO states (open / in-progress). Excludes draft, done, cancel.
    _IN_SCOPE_STATES = ("confirmed", "progress", "to_close")

    @ETL.load()
    def generate_component_pickings(self, ctx: ETLContext, transformed: Dict) -> None:
        """Generate the component PICK for every in-scope imported MO.

        Queries ``mrp.production`` from the DB (does not trust any transform
        payload) so it is independently invocable and re-runnable.
        """
        prods = ctx.env["mrp.production"].search(
            [
                ("xtuple_wo_id", "!=", False),
                ("state", "in", list(self._IN_SCOPE_STATES)),
            ]
        )
        _logger.info(
            "Pick generator: %d in-scope imported MO(s) to evaluate", len(prods)
        )

        pick_count = 0
        for production in prods:
            if self._generate_pick_for_production(ctx, production):
                pick_count += 1

        _logger.info("Pick generator: generated PICKs for %d MO(s)", pick_count)

    def _generate_pick_for_production(self, ctx: ETLContext, production) -> bool:
        """Generate (if needed) the component PICK for a single MO.

        Returns True if a PICK was generated, False if the MO was skipped
        (no pick step, already has a PICK, or no wrappable raw moves).
        """
        # 1. Resolve the LIVE warehouse + pbm params at run time. Never trust
        #    the (frozen) raw-move location_id.
        wh = production.warehouse_id

        # 2. Skip if no pick step (1-step warehouse, or pbm params unset).
        #    This is also the correct pre-route-config no-op.
        if (
            not wh
            or wh.manufacture_steps == "mrp_one_step"
            or not wh.pbm_type_id
            or not wh.pbm_loc_id
        ):
            return False

        pbm_type = wh.pbm_type_id
        pbm_loc = wh.pbm_loc_id
        lot_stock = wh.lot_stock_id
        pick_loc_src = pbm_type.default_location_src_id or lot_stock

        # 3. Idempotency — skip if a pbm-type PICK already exists for this MO.
        #    Search directly on the moves (not the cached picking_ids compute)
        #    so a re-run within the same transaction is correctly detected.
        existing_pick_move = ctx.env["stock.move"].search(
            [
                ("production_group_id", "=", production.production_group_id.id),
                ("picking_type_id", "=", pbm_type.id),
                ("picking_id", "!=", False),
            ],
            limit=1,
        )
        if existing_pick_move:
            return False

        # 4. Select the imported raw moves and classify their source location
        #    against the live warehouse.
        raw = production.move_raw_ids.filtered(lambda m: m.xtuple_womatl_id)
        if not raw:
            return False  # E1 — nothing to wrap

        to_realign = raw.filtered(lambda m: m.location_id == lot_stock)
        already_ok = raw.filtered(lambda m: m.location_id == pbm_loc)
        anomalous = raw - to_realign - already_ok

        if anomalous:
            locs = anomalous.mapped("location_id").mapped("complete_name")
            _logger.warning(
                "MO %s: %d raw move(s) with unexpected location_id %s — "
                "skipped, needs human review",
                production.name,
                len(anomalous),
                locs,
            )
            ctx.report.warning(
                message=(
                    "Unexpected raw-move source location(s) %s — skipped, "
                    "not realigned, excluded from the component PICK."
                    % (locs,)
                ),
                source_ref=production.name,
            )

        wrap = to_realign | already_ok
        if not wrap:
            return False  # every raw move was anomalous — nothing safe to wrap

        # Realign the frozen lot_stock raw moves so the pbm -> production leg
        # is geographically correct. Touches ONLY location_id, ONLY for moves
        # currently at lot_stock_id.
        if to_realign:
            to_realign.write({"location_id": pbm_loc.id})

        # Back-propagate the MO's group + refs onto the raw moves so the PICK
        # surfaces on mo.picking_ids and groups via _assign_picking.
        wrap.write(
            {
                "production_group_id": production.production_group_id.id,
                "reference_ids": [Command.set(production.reference_ids.ids)],
            }
        )

        # 5. Build one upstream pick move per wrapped raw move (state draft),
        #    chained to the raw move via move_dest_ids.
        pick_move_vals = []
        for rm in wrap:
            pick_move_vals.append(
                {
                    "product_id": rm.product_id.id,
                    "product_uom": rm.product_uom.id,
                    "product_uom_qty": rm.product_uom_qty,
                    "location_id": pick_loc_src.id,
                    "location_dest_id": pbm_loc.id,
                    "picking_type_id": pbm_type.id,
                    "company_id": production.company_id.id,
                    "warehouse_id": wh.id,
                    "production_group_id": production.production_group_id.id,
                    "reference_ids": [
                        Command.set(production.reference_ids.ids)
                    ],
                    "origin": production.name,
                    "move_dest_ids": [Command.link(rm.id)],
                    "state": "draft",
                }
            )
        pick_moves = (
            ctx.env["stock.move"]
            .with_context(tracking_disable=True)
            .create(pick_move_vals)
        )

        # 6. Attach to a PICK via Odoo's own grouping (works on draft moves;
        #    all share the key tuple so they group into one PICK per MO).
        #    We do NOT confirm — pick moves ship draft.
        pick_moves._assign_picking()

        return True
