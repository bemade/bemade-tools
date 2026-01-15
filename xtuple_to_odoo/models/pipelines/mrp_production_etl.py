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
