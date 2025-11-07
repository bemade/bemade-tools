from odoo import models, fields, api
import logging
from typing import Dict, List

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


class MrpBom(models.Model):
    _inherit = "mrp.bom"

    sap_code = fields.Char(index="btree", copy=False)


@ETL.pipeline(
    target_model="mrp.bom",
    importer_name="mrp.bom.importer",
    sap_source="oitt",
    depends_on=["product.product.importer"],
)
class MrpBomImporter(models.AbstractModel):
    _name = "mrp.bom.importer"
    _description = "SAP BOM Importer (OITT/ITT1)"
    
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
        ctx.cr.execute("SELECT * FROM oitt")
        all_boms = ctx.cr.dictfetchall()
        
        ctx.cr.execute("SELECT * FROM itt1")
        sap_bom_lines = ctx.cr.dictfetchall()
        
        # Filter out existing BOMs
        sap_boms = [
            bom for bom in all_boms
            if bom["code"] not in existing_codes
        ]
        
        _logger.info(
            f"Extracted {len(sap_boms)} new BOMs from SAP OITT "
            f"(filtered from {len(all_boms)} total)."
        )
        _logger.info(f"Extracted {len(sap_bom_lines)} BOM lines from SAP ITT1.")
        
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
            product.sap_item_code: product.product_tmpl_id.id for product in odoo_products
        }
        company_id = ctx.env.company.id
        
        MrpBomImporter._lookup_cache = {
            "products_map": products_map,
            "product_tmpl_map": product_tmpl_map,
            "company_id": company_id,
            "bom_lines": sap_bom_lines,
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
                _logger.warning(
                    f"Skipping BOM for unknown product: code={bom['code']}"
                )
                continue
            
            bom_vals.append({
                "product_tmpl_id": product_tmpl_id,
                "product_qty": bom["qauntity"],  # Note: SAP typo in field name
                "type": "phantom" if bom["treetype"] == "A" else "normal",
                "sap_code": bom["code"],
                "company_id": company_id,
            })
        
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
        
        component_vals = []
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
            
            product_id = products_map.get(line["code"])
            if not product_id:
                _logger.warning(
                    f"Skipping BOM line for unknown product: code={line['code']}"
                )
                continue
            
            component_vals.append({
                "product_id": product_id,
                "product_qty": quantity,
                "sequence": line["childnum"],
                "bom_id": boms_by_code[line["father"]],
                "company_id": company_id,
            })
        
        if component_vals:
            ctx.env["mrp.bom.line"].create(component_vals)
            _logger.info(f"Created {len(component_vals)} BOM lines.")
        else:
            _logger.info("No BOM lines to create.")


class SapBomImporter(models.AbstractModel):
    _name = "sap.bom.importer"
    _description = "SAP BOM Importer"

    @api.model
    def import_boms(self, cr):
        _logger.info(f"Importing BOMs...")
        return self._import_boms(cr)

    def _import_boms(self, cr):
        cr.execute("SELECT * from OITT")
        sap_boms = cr.dictfetchall()
        cr.execute("SELECT * from ITT1")
        sap_bom_lines = cr.dictfetchall()
        sql = """
        SELECT code from oitt
        UNION
        SELECT code from itt1 
        """
        cr.execute(sql)
        concerned_codes = [rec[0] for rec in cr.fetchall()]
        odoo_products = self.env["product.product"].search(
            [("sap_item_code", "in", concerned_codes), ("active", "in", [False, True])]
        )
        odoo_products = {product.sap_item_code: product for product in odoo_products}
        bom_vals = []
        _logger.info(f"Importing {len(sap_boms)} BOMs...")
        for bom in sap_boms:
            bom_vals.append(
                {
                    "product_tmpl_id": odoo_products[bom["code"]].product_tmpl_id.id,
                    "product_qty": bom["qauntity"],
                    "type": "phantom" if bom["treetype"] == "A" else "normal",
                    "sap_code": bom["code"],
                    "company_id": self.env.company.id,
                }
            )
        boms = self.env["mrp.bom"].create(bom_vals)
        boms_by_code = {bom.sap_code: bom for bom in boms}
        component_vals = []
        _logger.info(f"Importing {len(sap_bom_lines)} BOM lines...")
        for line in sap_bom_lines:
            if not line["code"]:
                continue
            component_vals.append(
                {
                    "product_id": odoo_products[line["code"]].product_variant_id.id,
                    "product_qty": line["quantity"],
                    "sequence": line["childnum"],
                    "bom_id": boms_by_code[line["father"]].id,
                    "company_id": self.env.company.id,
                }
            )
        self.env["mrp.bom.line"].create(component_vals)
        return boms

    def _delete_all(self):
        self.env.cr.execute("DELETE FROM mrp_bom WHERE sap_code is not null")
