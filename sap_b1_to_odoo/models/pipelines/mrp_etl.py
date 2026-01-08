"""Production Order ETL Pipeline

This module contains the ETL pipeline for importing production orders from SAP B1 (OWOR/WOR1).

SAP Tables:
-----------
- OWOR: Production order headers
    - docentry, docnum: identifiers
    - itemcode: finished product being produced
    - status: L=Released, C=Closed, P=Planned, R=?
    - plannedqty, cmpltqty, rjctqty: quantities
    - postdate, duedate, startdate
    - warehouse: production warehouse
    - cardcode: customer (if make-to-order)

- WOR1: Production order components (BOM lines)
    - docentry: link to OWOR
    - linenum: line sequence
    - itemcode: component item (materials, labor, freight, etc.)
    - baseqty: quantity per 1 unit of finished product
    - plannedqty: total planned quantity (baseqty * parent plannedqty)
    - issuedqty: quantity actually consumed/issued
    - warehouse: source warehouse for component
    - u_nbs_runtime: runtime in hours (for labor)
    - u_nbs_setup: setup time in hours (for labor)

- WOR2: Base document links (e.g., link to sales order) - only 2 records, likely not critical
- WOR4: Production stages - empty, not used
- WOR5: Related document references - 29 records, cross-references to other production orders

SAP Item Types in WOR1:
-----------------------
All WOR1 lines have itemtype=4 (BOM component), but the underlying items in OITM have:
- itemtype='I': Regular inventory items (materials)
- itemtype='L': Labor items (51 distinct codes like LABORPOURXX, LABORMOLD0X, etc.)
- itemtype='T': Travel/expense items (FREIGHT1DXX)
- Non-inventory items: GASCAST1DXX, NONINVEN1DX, NONINVENEQU1DX

SAP Labor Item Codes (52 total):
--------------------------------
LABORPOURXX (Pour/Rammed), LABORMOLD0X (Build/Repair/Strip Mold), LABORGRINDXX (Grind),
LABORWELDXX (Welding), LABORCLEANXX (Mold Removal/Clean Up), LABORSETUP (Set Up),
LABORSHIPXX (Packaging/Shipping), LABORJACKHXX (Jack Hammer), LABORMASTICX (Mastic),
LABORSANDXX (Sand Blast), LABORPAINTX (Paint), LABORBRICKXX (Install Brick),
LABORBOARDX (Install Board/Paper), LABORINSULATIONX (Install Insulation),
LABORANCHORXX (Anchor Removal), LABORDRILLDXX (Drilling), LABORCHOPSAW (Chop Saw),
LABORSPRAY (Spray), LABORSSPREP (SS Rod Prep), LABORSCAFFOLD (Scaffold),
Plus overtime variants (*OT*) and consulting/contract items (*_JOB, *CONTRACT*)

Odoo Mapping:
-------------
- mrp.production <- OWOR headers
    - product_id <- itemcode (lookup product by sap_item_code)
    - product_qty <- plannedqty
    - qty_produced <- cmpltqty
    - date_start <- startdate or postdate
    - date_deadline <- duedate
    - state <- status mapping (see SAP_STATUS_MAP)
    - location_src_id <- warehouse (lookup stock.location)
    - location_dest_id <- warehouse (finished goods location)
    - origin <- SAP docnum reference

- mrp.workcenter <- Single generic "Production" work center (code: PROD)
    All BOM operations use this single work center.

- mrp.routing.workcenter <- BOM operations from SAP labor items in ITT1
    Each labor line in a BOM becomes an operation on that BOM.
    - name <- labor item name from OITM
    - workcenter_id <- generic PROD work center
    - bom_id <- parent BOM
    - time_cycle_manual <- quantity (hours) * 60 (convert to minutes)

    Work orders are auto-generated from these operations when MOs are confirmed.

- stock.move <- WOR1 lines for materials (non-labor items)
    Track actual material consumption via stock moves.
    - product_id <- itemcode
    - product_uom_qty <- issuedqty
    - raw_material_production_id <- link to mrp.production

Status Counts in SAP:
--------------------
- L (Released/Launched): 2,002 orders
- C (Closed): 127 orders
- P (Planned): 3 orders
- R (?): 1 order

Total: 2,133 production orders with 17,351 component lines
"""

import logging
from typing import Dict, List, Any

from odoo import api, fields, models
from odoo.tools import mute_logger

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


# SAP status to Odoo state mapping
SAP_STATUS_MAP = {
    "P": "draft",  # Planned -> Draft
    "R": "confirmed",  # Released -> Confirmed
    "L": "progress",  # Launched/Released -> In Progress
    "C": "done",  # Closed -> Done
}


# =============================================================================
# PIPELINE 1: Production Orders (create MOs and confirm)
# =============================================================================


@ETL.pipeline(
    target_model="mrp.production",
    importer_name="mrp.production.importer",
    sap_source="owor",
    depends_on=[
        "product.product.importer",
    ],
    chunk_size=50,
)
class MrpProductionETLImporter(models.AbstractModel):
    _name = "mrp.production.importer"
    _description = "SAP Production Order ETL Importer"

    @ETL.extract("owor")
    def extract_production_orders(self, ctx: ETLContext):
        """Extract production order headers from SAP OWOR table."""
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_docnum FROM mrp_production WHERE sap_docnum IS NOT NULL"
        )
        existing_docnums = tuple(row[0] for row in ctx.env.cr.fetchall())

        sql = """
            SELECT docentry, docnum, itemcode, status,
                   plannedqty, cmpltqty, postdate, duedate, startdate, warehouse,
                   origintype, originnum, atcentry
            FROM owor
        """
        if existing_docnums:
            sql += " WHERE docnum NOT IN %s"
            ctx.cr.execute(sql, (existing_docnums,))
        else:
            ctx.cr.execute(sql)

        orders = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(orders)} production orders from SAP OWOR")
        return {"headers": orders}

    @ETL.extract("metadata")
    def extract_metadata(self, ctx: ETLContext):
        """Extract lookup data needed for transform."""
        products = ctx.env["product.product"].search(
            [("sap_item_code", "!=", False), ("active", "in", [True, False])]
        )
        # Get sale order lines by SAP docnum for linking
        sale_lines = ctx.env["sale.order.line"].search(
            [("order_id.sap_docnum", "!=", False)]
        )
        # Map SAP docnum -> first sale order line id (for MO linking)
        sale_line_map = {}
        for line in sale_lines:
            docnum = line.order_id.sap_docnum
            if docnum not in sale_line_map:
                sale_line_map[docnum] = line.id
        return {
            "products": {p.sap_item_code: p.id for p in products},
            "sale_lines": sale_line_map,
        }

    @ETL.transform()
    def transform_production_orders(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform SAP production orders to Odoo mrp.production values."""
        orders = extracted.get("extract_production_orders", {}).get("headers", [])
        metadata = extracted.get("extract_metadata", {})
        products_dict = metadata.get("products", {})
        sale_lines = metadata.get("sale_lines", {})

        production_vals = []
        skipped_products = {}  # itemcode -> count

        for order in orders:
            product_id = products_dict.get(order["itemcode"])
            if not product_id:
                skipped_products[order["itemcode"]] = (
                    skipped_products.get(order["itemcode"], 0) + 1
                )
                continue

            # Strip timezone from dates (Odoo expects naive datetimes)
            date_start = order.get("startdate") or order.get("postdate")
            date_deadline = order.get("duedate")
            if date_start and hasattr(date_start, "replace"):
                date_start = date_start.replace(tzinfo=None)
            if date_deadline and hasattr(date_deadline, "replace"):
                date_deadline = date_deadline.replace(tzinfo=None)

            vals = {
                "sap_docentry": order["docentry"],
                "sap_docnum": order["docnum"],
                "sap_atcentry": order.get("atcentry") or 0,
                "product_id": product_id,
                "product_qty": order["plannedqty"] or 0.0,
                "date_start": date_start,
                "date_deadline": date_deadline,
                "origin": f"SAP-{order['docnum']}",
                "_sap_status": order["status"],
            }

            # Link to sale order if origintype is 'S' (Sales Order)
            if order.get("origintype") == "S" and order.get("originnum"):
                sale_line_id = sale_lines.get(str(order["originnum"]))
                if sale_line_id:
                    vals["sale_line_id"] = sale_line_id

            production_vals.append(vals)

        if skipped_products:
            total_skipped = sum(skipped_products.values())
            _logger.warning(
                f"Skipped {total_skipped} MOs due to {len(skipped_products)} missing products"
            )
        _logger.info(f"Transformed {len(production_vals)} production orders")
        return production_vals

    @ETL.load()
    def load_production_orders(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create production orders and confirm non-draft ones."""
        production_vals = transformed.get("transform_production_orders", [])
        if not production_vals:
            return

        # Separate status before create
        statuses = [v.pop("_sap_status", "P") for v in production_vals]

        with mute_logger("odoo.sql_db"):
            productions = ctx.env["mrp.production"].create(production_vals)
        _logger.info(f"Created {len(productions)} production orders")

        # Confirm non-draft orders via SQL
        to_confirm = [
            p.id for p, s in zip(productions, statuses) if s in ("R", "L", "C")
        ]
        if to_confirm:
            ctx.env.cr.execute(
                "UPDATE mrp_production SET state = 'confirmed' WHERE id = ANY(%s)",
                [to_confirm],
            )
            _logger.info(f"Confirmed {len(to_confirm)} production orders")


# =============================================================================
# PIPELINE 2: Work Center (create single generic work center)
# =============================================================================


@ETL.pipeline(
    target_model="mrp.workcenter",
    importer_name="mrp.workcenter.importer",
    sap_source="oitm",
    depends_on=[],
    allow_multiprocessing=False,
)
class MrpWorkcenterETLImporter(models.AbstractModel):
    _name = "mrp.workcenter.importer"
    _description = "Create Generic Production Work Center"

    @ETL.extract("existing")
    def extract_existing_workcenter(self, ctx: ETLContext):
        """Check if generic work center already exists."""
        workcenter = ctx.env["mrp.workcenter"].search([("code", "=", "PROD")], limit=1)
        return {
            "exists": bool(workcenter),
            "workcenter_id": workcenter.id if workcenter else None,
        }

    @ETL.transform()
    def transform_workcenter(self, ctx: ETLContext, extracted: Dict) -> Dict:
        """Prepare generic work center if needed."""
        data = extracted.get("extract_existing_workcenter", {})
        if data.get("exists"):
            _logger.info("Generic work center PROD already exists")
            return {"create": False, "workcenter_id": data.get("workcenter_id")}

        return {
            "create": True,
            "vals": {
                "name": "Production",
                "code": "PROD",
                "time_efficiency": 100.0,
            },
        }

    @ETL.load()
    def load_workcenter(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create generic work center if needed."""
        data = transformed.get("transform_workcenter", {})
        if not data.get("create"):
            return

        workcenter = ctx.env["mrp.workcenter"].create(data["vals"])
        _logger.info(
            f"Created generic work center: {workcenter.name} ({workcenter.code})"
        )


# =============================================================================
# PIPELINE 3: Material Consumption (stock moves from WOR1)
# =============================================================================
# Note: Work orders are now auto-generated from BOM operations when MOs are confirmed.
# This pipeline only handles material consumption tracking.


@ETL.pipeline(
    target_model="stock.move",
    importer_name="mrp.consumption.importer",
    sap_source="wor1",
    depends_on=[
        "mrp.production.importer",
    ],
    chunk_size=200,
)
class MrpConsumptionETLImporter(models.AbstractModel):
    _name = "mrp.consumption.importer"
    _description = "SAP WOR1 Material Consumption ETL Importer"

    @ETL.extract("wor1")
    def extract_wor1_lines(self, ctx: ETLContext):
        """Extract WOR1 material lines (non-labor), excluding already imported."""
        # Get existing stock moves
        ctx.env.cr.execute(
            "SELECT sap_docentry, sap_linenum FROM stock_move WHERE sap_docentry IS NOT NULL"
        )
        existing_moves = {(r[0], r[1]) for r in ctx.env.cr.fetchall()}

        # Only get non-labor lines
        ctx.cr.execute(
            """
            SELECT docentry, linenum, itemcode, plannedqty, issuedqty, warehouse,
                   u_nbs_wrkinstr
            FROM wor1
            WHERE itemcode NOT LIKE 'LABOR%'
            """
        )
        all_lines = ctx.cr.dictfetchall()

        # Filter out already imported lines
        lines = [
            line
            for line in all_lines
            if (line["docentry"], line["linenum"]) not in existing_moves
        ]

        _logger.info(
            f"Extracted {len(lines)} new material lines (skipped {len(all_lines) - len(lines)} existing)"
        )
        return lines

    @ETL.extract("metadata")
    def extract_metadata(self, ctx: ETLContext):
        """Extract lookup data as serializable dicts."""
        # Productions by SAP docentry
        productions = ctx.env["mrp.production"].search([("sap_docentry", "!=", False)])
        prod_map = {
            p.sap_docentry: {
                "id": p.id,
                "location_src_id": p.location_src_id.id if p.location_src_id else False,
                "company_id": p.company_id.id,
            }
            for p in productions
        }

        # Products by SAP item code (include archived)
        products = ctx.env["product.product"].search(
            [("sap_item_code", "!=", False), ("active", "in", [True, False])]
        )
        product_map = {
            p.sap_item_code: {
                "id": p.id,
                "uom_id": p.uom_id.id,
            }
            for p in products
        }

        # Production location
        prod_loc = ctx.env["stock.location"].search(
            [("usage", "=", "production"), ("company_id", "=", ctx.env.company.id)],
            limit=1,
        )

        return {
            "productions": prod_map,
            "products": product_map,
            "production_location_id": prod_loc.id if prod_loc else False,
        }

    @ETL.transform()
    def transform_stock_moves(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform material lines to stock move vals."""
        lines = extracted.get("extract_wor1_lines", [])
        metadata = extracted.get("extract_metadata", {})
        prod_map = metadata.get("productions", {})
        product_map = metadata.get("products", {})
        prod_loc_id = metadata.get("production_location_id")

        move_vals = []
        for line in lines:
            production = prod_map.get(line["docentry"])
            product = product_map.get(line["itemcode"])
            issued_qty = line.get("issuedqty") or 0.0

            if not production or not product or issued_qty <= 0:
                continue

            vals = {
                "raw_material_production_id": production["id"],
                "product_id": product["id"],
                "product_uom": product["uom_id"],
                "product_uom_qty": issued_qty,
                "location_id": production["location_src_id"],
                "location_dest_id": prod_loc_id,
                "company_id": production["company_id"],
                "sap_docentry": line["docentry"],
                "sap_linenum": line["linenum"],
            }
            # Add work instructions if present
            if line.get("u_nbs_wrkinstr"):
                vals["sap_comment"] = line["u_nbs_wrkinstr"]
            move_vals.append(vals)

        _logger.info(f"Transformed {len(move_vals)} component stock moves")
        return move_vals

    @ETL.load()
    def load_stock_moves(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create component consumption stock moves."""
        move_vals = transformed.get("transform_stock_moves", [])
        if not move_vals:
            return

        with mute_logger("odoo.sql_db"):
            moves = ctx.env["stock.move"].create(move_vals)
        _logger.info(f"Created {len(moves)} component stock moves")


# =============================================================================
# PIPELINE 4: Work Order Time Updates (from WOR1 labor lines)
# =============================================================================


@ETL.pipeline(
    target_model="mrp.workorder",
    importer_name="mrp.workorder.time.updater",
    sap_source="wor1",
    depends_on=[
        "mrp.consumption.importer",
    ],
    allow_multiprocessing=False,
)
class MrpWorkorderTimeUpdater(models.AbstractModel):
    _name = "mrp.workorder.time.updater"
    _description = "Update Work Order Durations from SAP WOR1"

    @ETL.extract("wor1_labor")
    def extract_labor_lines(self, ctx: ETLContext):
        """Extract WOR1 labor lines with actual hours."""
        # Get labor item names for matching
        ctx.cr.execute(
            "SELECT itemcode, itemname FROM oitm WHERE itemcode LIKE 'LABOR%'"
        )
        labor_names = {row[0]: row[1] for row in ctx.cr.fetchall()}

        # Get labor lines with issued hours
        ctx.cr.execute(
            """
            SELECT docentry, itemcode, issuedqty
            FROM wor1
            WHERE itemcode LIKE 'LABOR%' AND issuedqty > 0
            """
        )
        labor_lines = ctx.cr.dictfetchall()

        _logger.info(f"Extracted {len(labor_lines)} labor lines with actual hours")
        return {"labor_lines": labor_lines, "labor_names": labor_names}

    @ETL.extract("workorders")
    def extract_workorders(self, ctx: ETLContext):
        """Get work orders that need duration updates."""
        # Get work orders with their production's SAP docentry and operation name
        ctx.env.cr.execute(
            """
            SELECT wo.id, wo.name, mp.sap_docentry, rw.name as op_name
            FROM mrp_workorder wo
            JOIN mrp_production mp ON wo.production_id = mp.id
            LEFT JOIN mrp_routing_workcenter rw ON wo.operation_id = rw.id
            WHERE mp.sap_docentry IS NOT NULL
              AND wo.duration = 0
            """
        )
        workorders = ctx.env.cr.fetchall()
        _logger.info(f"Found {len(workorders)} work orders needing duration updates")
        return {"workorders": workorders}

    @ETL.transform()
    def transform_updates(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Match labor lines to work orders and prepare updates."""
        labor_data = extracted.get("extract_labor_lines", {})
        labor_lines = labor_data.get("labor_lines", [])
        labor_names = labor_data.get("labor_names", {})
        workorders = extracted.get("extract_workorders", {}).get("workorders", [])

        # Build lookup: (sap_docentry, labor_name) -> issued_hours
        labor_map = {}
        for line in labor_lines:
            labor_name = labor_names.get(line["itemcode"], line["itemcode"])
            key = (line["docentry"], labor_name)
            # Sum hours if multiple lines for same operation
            labor_map[key] = labor_map.get(key, 0) + (line["issuedqty"] or 0)

        # Match work orders to labor lines
        updates = []
        for wo_id, wo_name, sap_docentry, op_name in workorders:
            if not op_name:
                continue
            key = (sap_docentry, op_name)
            issued_hours = labor_map.get(key)
            if issued_hours and issued_hours > 0:
                updates.append(
                    {
                        "id": wo_id,
                        "duration": issued_hours * 60,  # hours to minutes
                    }
                )

        _logger.info(f"Matched {len(updates)} work orders to labor time data")
        return updates

    @ETL.load()
    def load_updates(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update work order durations."""
        updates = transformed.get("transform_updates", [])
        if not updates:
            return

        # Batch update via SQL for performance
        for update in updates:
            ctx.env.cr.execute(
                "UPDATE mrp_workorder SET duration = %s WHERE id = %s",
                [update["duration"], update["id"]],
            )
        _logger.info(f"Updated duration on {len(updates)} work orders")


# =============================================================================
# PIPELINE 5: Post-process (finalize states via SQL)
# =============================================================================


@ETL.pipeline(
    target_model="mrp.production",
    importer_name="mrp.production.postprocess",
    sap_source="owor",
    depends_on=[
        "mrp.workorder.time.updater",
    ],
    chunk_size=500,
)
class MrpProductionPostprocessImporter(models.AbstractModel):
    _name = "mrp.production.postprocess"
    _description = "SAP Production Order Post-processing"

    @ETL.extract("productions")
    def extract_productions_to_finalize(self, ctx: ETLContext):
        """Get productions that need state finalization."""
        # Get Odoo productions not yet finalized
        ctx.env.cr.execute(
            """
            SELECT id, sap_docnum
            FROM mrp_production
            WHERE sap_docnum IS NOT NULL
              AND state NOT IN ('done', 'cancel')
            """
        )
        odoo_prods = {row[1]: row[0] for row in ctx.env.cr.fetchall()}  # docnum -> id

        if not odoo_prods:
            return []

        # Get SAP status for those productions
        # Convert string docnums to integers for SAP query
        docnums = tuple(int(d) for d in odoo_prods.keys())
        ctx.cr.execute(
            """
            SELECT docnum, status, cmpltqty, plannedqty, closedate
            FROM owor
            WHERE docnum IN %s
            """,
            (docnums,),
        )
        sap_data = ctx.cr.fetchall()

        # Combine: return list of (odoo_id, status, cmpltqty, plannedqty, closedate)
        return [
            (odoo_prods[str(row[0])], row[1], row[2], row[3], row[4])
            for row in sap_data
            if str(row[0]) in odoo_prods
        ]

    @ETL.transform()
    def transform_state_updates(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Prepare state update data."""
        productions = extracted.get("extract_productions_to_finalize", [])
        return [
            {
                "id": row[0],
                "status": row[1],
                "qty_produced": row[2],
                "qty_planned": row[3],
                "closedate": row[4],
            }
            for row in productions
        ]

    @ETL.load()
    def load_finalize_states(self, ctx: ETLContext, transformed: Dict) -> None:
        """Finalize production states via SQL."""
        updates = transformed.get("transform_state_updates", [])
        if not updates:
            return

        cr = ctx.env.cr
        now = fields.Datetime.now()

        # Mark as done if status is 'C' OR completed qty >= planned qty
        done_ids = [
            u["id"]
            for u in updates
            if u["status"] == "C"
            or (u["qty_produced"] or 0) >= (u["qty_planned"] or 0) > 0
        ]
        # Mark as progress if status is 'L' and not already done
        done_id_set = set(done_ids)
        progress_ids = [
            u["id"]
            for u in updates
            if u["status"] == "L" and u["id"] not in done_id_set
        ]

        if progress_ids:
            cr.execute(
                "UPDATE mrp_production SET state = 'progress' WHERE id = ANY(%s)",
                [progress_ids],
            )
            cr.execute(
                "UPDATE stock_move SET state = 'done', quantity = product_uom_qty WHERE raw_material_production_id = ANY(%s)",
                [progress_ids],
            )
            _logger.info(f"Set {len(progress_ids)} productions to 'progress'")

        if done_ids:
            cr.execute(
                "UPDATE mrp_production SET state = 'done', date_finished = %s, is_locked = true WHERE id = ANY(%s)",
                [now, done_ids],
            )
            cr.execute(
                "UPDATE stock_move SET state = 'done', quantity = product_uom_qty WHERE raw_material_production_id = ANY(%s) OR production_id = ANY(%s)",
                [done_ids, done_ids],
            )
            cr.execute(
                "UPDATE mrp_workorder SET state = 'done', date_finished = %s WHERE production_id = ANY(%s)",
                [now, done_ids],
            )
            _logger.info(f"Set {len(done_ids)} productions to 'done'")


# =============================================================================
# NOTES: Implementation Details
# =============================================================================
#
# Material Consumption:
# - Component consumption is tracked via stock.move records linked to productions
# - Moves are created with raw_material_production_id pointing to the MO
# - States are set to 'done' via SQL for closed/in-progress productions
#
# Labor Tracking:
# - Each LABOR* item code becomes an mrp.workcenter
# - Each labor line on a production order becomes an mrp.workorder
# - Time tracking via mrp.workcenter.productivity records
#
# State Management:
# - Production states are set via direct SQL UPDATE for performance
# - Stock move states are also set via SQL to avoid ORM overhead
# - Work order states are set via SQL for done productions
#
# No lot/serial tracking in SAP WOR1 - confirmed empty.
#
# =============================================================================
# TODO: BOM Importer (if needed)
# =============================================================================
#
# If BOMs don't exist in Odoo yet, we could create them from SAP data.
# SAP stores BOMs in OITT (BOM headers) and ITT1 (BOM lines).
#
# However, WOR1 contains the "exploded" BOM for each production order,
# which may differ from the master BOM due to:
# - Quantity adjustments
# - Substitutions
# - Order-specific modifications
#
# Current approach: Skip BOM creation, track consumption via stock.move
