"""xTuple MRP BOM ETL Pipelines

This module handles the migration of bill of materials data from xTuple to Odoo
using the ETL framework.

Pipeline execution order:
1. xtuple.mrp.bom.importer - Import BOM headers
2. xtuple.mrp.bom.line.importer - Import BOM components/lines
3. xtuple.mrp.bom.postprocessor - Set routes on products
"""

import logging
from typing import Any, Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# Common SQL query parts
BOM_HEAD_SELECT = """
    bomhead_id,
    bomhead_item_id,
    bomhead_revision,
    bomhead_revisiondate,
    bomhead_batchsize,
    bomhead_requiredqtyper,
    item_number as parent_item_number,
    item_descrip1 as parent_item_descrip1
"""

BOM_ITEM_SELECT = """
    bomitem_id,
    bomitem_parent_item_id,
    bomitem_seqnumber,
    bomitem_item_id,
    bomitem_qtyper,
    bomitem_scrap,
    bomitem_effective,
    bomitem_expires,
    bomitem_createwo,
    bomitem_issuemethod,
    bomitem_uom_id,
    bomitem_notes,
    bomitem_ref,
    bomitem_qtyfxd,
    component_item.item_number as component_item_number,
    component_item.item_descrip1 as component_item_descrip1,
    uom_name
"""


# =============================================================================
# BOM Header Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="mrp.bom",
    importer_name="xtuple.mrp.bom.importer",
    sap_source="bomhead",
    depends_on=["xtuple.product.importer"],
)
class XtupleMrpBomImporter(models.AbstractModel):
    _name = "xtuple.mrp.bom.importer"
    _description = "xTuple BOM Header Importer"

    @ETL.extract("bomhead")
    def extract_boms(self, ctx: ETLContext) -> Dict[str, Any]:
        """Extract BOMs from xTuple bomhead table."""
        ctx.env.cr.execute(
            "SELECT xtuple_bomhead_id FROM mrp_bom WHERE xtuple_bomhead_id IS NOT NULL"
        )
        existing_bomhead_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(f"Found {len(existing_bomhead_ids)} existing BOMs in Odoo")

        select_clause = f"""
        SELECT
            {BOM_HEAD_SELECT}
        FROM bomhead
        JOIN item ON (bomhead_item_id = item_id)
        WHERE item_type IN ('M', 'F')
        """

        if existing_bomhead_ids:
            where_clause = "AND bomhead_id NOT IN %s"
            ctx.cr.execute(select_clause + where_clause, (tuple(existing_bomhead_ids),))
        else:
            ctx.cr.execute(select_clause)

        boms = ctx.cr.dictfetchall()

        # Get product mapping (uom_id is on product.template in Odoo 19)
        ctx.env.cr.execute(
            """SELECT pp.xtuple_item_id, pp.id, pp.product_tmpl_id, pt.uom_id, pp.xtuple_item_type 
               FROM product_product pp
               JOIN product_template pt ON pp.product_tmpl_id = pt.id
               WHERE pp.xtuple_item_id IS NOT NULL"""
        )
        product_map = {
            row[0]: {
                "id": row[1],
                "product_tmpl_id": row[2],
                "uom_id": row[3],
                "item_type": row[4],
            }
            for row in ctx.env.cr.fetchall()
        }

        _logger.info(f"Extracted {len(boms)} new BOMs from xTuple")
        return {"boms": boms, "product_map": product_map}

    @ETL.transform()
    def transform_boms(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple BOMs into Odoo BOM values."""
        data = extracted.get("extract_boms", {})
        boms = data.get("boms", [])
        product_map = data.get("product_map", {})

        # Get manufacturing operation type
        picking_type = ctx.env["stock.picking.type"].search(
            [("code", "=", "mrp_operation")], limit=1
        )

        bom_vals = []
        for bom in boms:
            product_data = product_map.get(bom.get("bomhead_item_id"))
            if not product_data:
                _logger.warning(
                    f"Product with xTuple ID {bom.get('bomhead_item_id')} not found"
                )
                continue

            # Determine BOM type based on xTuple item type
            bom_type = "normal" if product_data.get("item_type") != "F" else "phantom"

            vals = {
                "product_tmpl_id": product_data["product_tmpl_id"],
                "product_qty": bom.get("bomhead_requiredqtyper", 1.0) or 1.0,
                "product_uom_id": product_data["uom_id"],
                "type": bom_type,
                "code": bom.get("parent_item_number", ""),
                "xtuple_bomhead_id": bom.get("bomhead_id"),
                "xtuple_bomhead_item_id": bom.get("bomhead_item_id"),
                "xtuple_revision": bom.get("bomhead_revision", ""),
                "xtuple_revision_date": bom.get("bomhead_revisiondate"),
                "xtuple_batch_size": bom.get("bomhead_batchsize", 1.0),
            }

            if picking_type:
                vals["picking_type_id"] = picking_type.id

            bom_vals.append(vals)

        _logger.info(f"Transformed {len(bom_vals)} BOM records")
        return bom_vals

    @ETL.load()
    def load_boms(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load BOMs into Odoo."""
        bom_vals = transformed.get("transform_boms", [])
        if bom_vals:
            boms = ctx.env["mrp.bom"].create(bom_vals)
            _logger.info(f"Created {len(boms)} BOMs")
        else:
            _logger.info("No new BOMs to create")


# =============================================================================
# BOM Line Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="mrp.bom.line",
    importer_name="xtuple.mrp.bom.line.importer",
    sap_source="bomitem",
    depends_on=["xtuple.mrp.bom.importer"],
)
class XtupleMrpBomLineImporter(models.AbstractModel):
    _name = "xtuple.mrp.bom.line.importer"
    _description = "xTuple BOM Line Importer"

    @ETL.extract("bomitem")
    def extract_bom_lines(self, ctx: ETLContext) -> Dict[str, Any]:
        """Extract BOM components from xTuple bomitem table."""
        ctx.env.cr.execute(
            "SELECT xtuple_bomitem_id FROM mrp_bom_line WHERE xtuple_bomitem_id IS NOT NULL"
        )
        existing_bomitem_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(
            f"Found {len(existing_bomitem_ids)} existing BOM components in Odoo"
        )

        select_clause = f"""
        SELECT
            {BOM_ITEM_SELECT}
        FROM bomitem
        JOIN item parent_item ON (bomitem_parent_item_id = parent_item.item_id)
        JOIN item component_item ON (bomitem_item_id = component_item.item_id)
        LEFT JOIN uom ON (bomitem_uom_id = uom.uom_id)
        WHERE parent_item.item_type IN ('M', 'F')
          AND (bomitem_expires IS NULL OR bomitem_expires > CURRENT_DATE)
          AND (bomitem_effective IS NULL OR bomitem_effective <= CURRENT_DATE)
        """

        if existing_bomitem_ids:
            where_clause = "AND bomitem_id NOT IN %s"
            ctx.cr.execute(select_clause + where_clause, (tuple(existing_bomitem_ids),))
        else:
            ctx.cr.execute(select_clause)

        components = ctx.cr.dictfetchall()

        # Get BOM mapping by parent item ID
        ctx.env.cr.execute(
            "SELECT xtuple_bomhead_item_id, id FROM mrp_bom WHERE xtuple_bomhead_item_id IS NOT NULL"
        )
        bom_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Get product mapping (uom_id is on product.template in Odoo 19)
        ctx.env.cr.execute(
            """SELECT pp.xtuple_item_id, pp.id, pt.uom_id 
               FROM product_product pp
               JOIN product_template pt ON pp.product_tmpl_id = pt.id
               WHERE pp.xtuple_item_id IS NOT NULL"""
        )
        product_map = {
            row[0]: {"id": row[1], "uom_id": row[2]} for row in ctx.env.cr.fetchall()
        }

        _logger.info(f"Extracted {len(components)} new BOM components from xTuple")
        return {
            "components": components,
            "bom_map": bom_map,
            "product_map": product_map,
        }

    @ETL.transform()
    def transform_bom_lines(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple BOM components into Odoo BOM line values."""
        data = extracted.get("extract_bom_lines", {})
        components = data.get("components", [])
        bom_map = data.get("bom_map", {})
        product_map = data.get("product_map", {})

        bom_line_vals = []
        for component in components:
            parent_item_id = component.get("bomitem_parent_item_id")
            component_item_id = component.get("bomitem_item_id")

            bom_id = bom_map.get(parent_item_id)
            product_data = product_map.get(component_item_id)

            if not bom_id:
                _logger.warning(
                    f"BOM for parent item {parent_item_id} not found in Odoo"
                )
                continue

            if not product_data:
                _logger.warning(
                    f"Component product {component_item_id} not found in Odoo"
                )
                continue

            # Get UoM - try to match by name first, fall back to product UoM
            uom_id = product_data["uom_id"]
            if component.get("uom_name"):
                uom = ctx.env["uom.uom"].search(
                    [("name", "=", component.get("uom_name"))], limit=1
                )
                if uom:
                    uom_id = uom.id

            # Calculate quantity
            quantity = component.get("bomitem_qtyper", 0.0)
            if component.get("bomitem_qtyfxd", 0.0) > 0:
                quantity = component.get("bomitem_qtyfxd", 0.0)

            # Add scrap percentage
            scrap = component.get("bomitem_scrap", 0.0)
            if scrap > 0:
                quantity = quantity * (1 + scrap / 100.0)

            bom_line_vals.append(
                {
                    "bom_id": bom_id,
                    "product_id": product_data["id"],
                    "product_qty": quantity,
                    "product_uom_id": uom_id,
                    "sequence": component.get("bomitem_seqnumber", 0),
                    "xtuple_bomitem_id": component.get("bomitem_id"),
                }
            )

        _logger.info(f"Transformed {len(bom_line_vals)} BOM line records")
        return bom_line_vals

    @ETL.load()
    def load_bom_lines(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load BOM lines into Odoo."""
        bom_line_vals = transformed.get("transform_bom_lines", [])
        if bom_line_vals:
            bom_lines = ctx.env["mrp.bom.line"].create(bom_line_vals)
            _logger.info(f"Created {len(bom_lines)} BOM lines")
        else:
            _logger.info("No new BOM lines to create")


# =============================================================================
# BOM Postprocessor Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="mrp.bom",
    importer_name="xtuple.mrp.bom.postprocessor",
    sap_source="",
    depends_on=["xtuple.mrp.bom.line.importer"],
)
class XtupleMrpBomPostprocessor(models.AbstractModel):
    _name = "xtuple.mrp.bom.postprocessor"
    _description = "xTuple BOM Postprocessor"

    @ETL.extract("")
    def extract_nothing(self, ctx: ETLContext) -> Dict:
        """No extraction needed for postprocessing."""
        return {}

    @ETL.transform()
    def transform_nothing(self, ctx: ETLContext, extracted: Dict) -> Dict:
        """No transformation needed for postprocessing."""
        return {}

    @ETL.load()
    def postprocess_boms(self, ctx: ETLContext, transformed: Dict) -> None:
        """Set manufacture route on BOM products."""
        _logger.info("Running BOM postprocessing...")

        # Get manufacture route
        route = ctx.env["stock.route"].search([("name", "=", "Manufacture")], limit=1)
        if not route:
            _logger.warning("Manufacture route not found, skipping route assignment")
            return

        # Get all BOMs with xTuple IDs
        boms = ctx.env["mrp.bom"].search([("xtuple_bomhead_id", "!=", False)])

        if boms:
            # Set manufacture route on product templates
            boms.product_tmpl_id.write({"route_ids": [(4, route.id)]})
            _logger.info(f"Set Manufacture route on {len(boms)} product templates")

        ctx.env["mrp.bom"].flush_model()
        ctx.env["mrp.bom.line"].flush_model()
        _logger.info("BOM postprocessing complete")
