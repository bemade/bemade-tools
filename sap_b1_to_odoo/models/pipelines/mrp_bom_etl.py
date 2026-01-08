import logging
from typing import Dict, List

from odoo import api, models

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="mrp.bom",
    importer_name="mrp.bom.importer",
    sap_source="oitt",
    depends_on=["product.product.importer", "mrp.workcenter.importer"],
)
class MrpBomImporter(models.AbstractModel):
    _name = "mrp.bom.importer"
    _description = "SAP BOM Importer (OITT/ITT1) with Operations"

    _lookup_cache = {}

    @ETL.extract("oitt")
    def extract_boms(self, ctx: ETLContext) -> List[Dict]:
        """Extract BOMs from SAP OITT and ITT1 tables.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of BOM dictionaries from SAP.
        """
        # Get existing BOMs to avoid duplicates
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_code FROM mrp_bom WHERE sap_code IS NOT NULL"
        )
        existing_codes = set(row[0] for row in ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_codes)} existing BOMs.")

        # Extract BOMs and BOM lines
        ctx.cr.execute("SELECT *, atcentry FROM oitt")
        all_boms = ctx.cr.dictfetchall()

        ctx.cr.execute("SELECT * FROM itt1")
        sap_bom_lines = ctx.cr.dictfetchall()

        # Get labor item names for operations
        ctx.cr.execute(
            "SELECT itemcode, itemname FROM oitm WHERE itemcode LIKE 'LABOR%'"
        )
        labor_items = {row[0]: row[1] for row in ctx.cr.fetchall()}

        # Filter out existing BOMs
        sap_boms = [bom for bom in all_boms if bom["code"] not in existing_codes]

        _logger.info(
            f"Extracted {len(sap_boms)} new BOMs from SAP OITT "
            f"(filtered from {len(all_boms)} total)."
        )
        _logger.info(f"Extracted {len(sap_bom_lines)} BOM lines from SAP ITT1.")
        _logger.info(f"Extracted {len(labor_items)} labor item definitions.")

        # Pre-compute product lookup
        _logger.info("Pre-computing product lookup for transform phase...")
        sql = """
        SELECT code FROM oitt
        UNION
        SELECT code FROM itt1 
        """
        ctx.cr.execute(sql)
        concerned_codes = [rec[0] for rec in ctx.cr.fetchall()]

        odoo_products = ctx.env["product.product"].search(
            [("sap_item_code", "in", concerned_codes), ("active", "in", [False, True])]
        )
        products_map = {product.sap_item_code: product.id for product in odoo_products}
        product_tmpl_map = {
            product.sap_item_code: product.product_tmpl_id.id
            for product in odoo_products
        }
        company_id = ctx.env.company.id

        # Get generic work center for operations
        workcenter = ctx.env["mrp.workcenter"].search([("code", "=", "PROD")], limit=1)
        workcenter_id = workcenter.id if workcenter else None

        MrpBomImporter._lookup_cache = {
            "products_map": products_map,
            "product_tmpl_map": product_tmpl_map,
            "company_id": company_id,
            "bom_lines": sap_bom_lines,
            "labor_items": labor_items,
            "workcenter_id": workcenter_id,
        }
        _logger.info(f"Product lookup ready with {len(products_map)} products.")

        return sap_boms

    @ETL.transform()
    def transform_boms(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP BOMs into Odoo BOM values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of BOM value dictionaries ready for creation.
        """
        sap_boms = extracted["extract_boms"]
        cache = MrpBomImporter._lookup_cache

        if not cache:
            raise RuntimeError("Cache is empty in transform! This should never happen.")

        product_tmpl_map = cache["product_tmpl_map"]
        company_id = cache["company_id"]

        bom_vals = []
        for bom in sap_boms:
            product_tmpl_id = product_tmpl_map.get(bom["code"])
            if not product_tmpl_id:
                _logger.warning(f"Skipping BOM for unknown product: code={bom['code']}")
                continue

            bom_vals.append(
                {
                    "product_tmpl_id": product_tmpl_id,
                    "product_qty": bom["qauntity"],  # Note: SAP typo in field name
                    "type": "phantom" if bom["treetype"] == "A" else "normal",
                    "sap_code": bom["code"],
                    "sap_atcentry": bom.get("atcentry") or 0,
                    "company_id": company_id,
                }
            )

        _logger.info(f"Transformed {len(bom_vals)} BOM records.")
        return bom_vals

    @ETL.load()
    def load_boms(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load BOMs and BOM lines into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        bom_vals = transformed["transform_boms"]

        if not bom_vals:
            _logger.info("No new BOMs to create.")
            return

        # Create BOMs
        boms = ctx.env["mrp.bom"].create(bom_vals)
        _logger.info(f"Created {len(boms)} BOMs.")

        # Create BOM lines
        cache = MrpBomImporter._lookup_cache
        products_map = cache["products_map"]
        company_id = cache["company_id"]
        sap_bom_lines = cache["bom_lines"]

        boms_by_code = {bom.sap_code: bom.id for bom in boms}

        labor_items = cache.get("labor_items", {})
        workcenter_id = cache.get("workcenter_id")

        component_vals = []
        operation_vals = []

        for line in sap_bom_lines:
            if not line["code"] or not line["father"]:
                continue

            # Only create lines for BOMs we just created
            if line["father"] not in boms_by_code:
                continue

            # Skip lines with invalid quantities
            quantity = line["quantity"]
            if not quantity or quantity <= 0:
                _logger.warning(
                    f"Skipping BOM line with invalid quantity {quantity}: "
                    f"father={line['father']}, code={line['code']}"
                )
                continue

            # Labor items become operations, not BOM lines
            if line["code"].startswith("LABOR") and workcenter_id:
                labor_name = labor_items.get(line["code"], line["code"])
                operation_vals.append(
                    {
                        "name": labor_name,
                        "workcenter_id": workcenter_id,
                        "bom_id": boms_by_code[line["father"]],
                        "time_cycle_manual": quantity * 60,  # hours to minutes
                        "sequence": line["childnum"],
                        "company_id": company_id,
                    }
                )
                continue

            product_id = products_map.get(line["code"])
            if not product_id:
                _logger.warning(
                    f"Skipping BOM line for unknown product: code={line['code']}"
                )
                continue

            vals = {
                "product_id": product_id,
                "product_qty": quantity,
                "sequence": line["childnum"],
                "bom_id": boms_by_code[line["father"]],
                "company_id": company_id,
            }
            # Add SAP comment if present
            if line.get("comment"):
                vals["sap_comment"] = line["comment"]
            component_vals.append(vals)

        if component_vals:
            ctx.env["mrp.bom.line"].create(component_vals)
            _logger.info(f"Created {len(component_vals)} BOM lines.")
        else:
            _logger.info("No BOM lines to create.")

        if operation_vals:
            ctx.env["mrp.routing.workcenter"].create(operation_vals)
            _logger.info(
                f"Created {len(operation_vals)} BOM operations from labor items."
            )
        else:
            _logger.info("No BOM operations to create.")
