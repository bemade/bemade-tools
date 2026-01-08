import logging

from typing import Dict, List

from odoo import models

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="stock.valuation.layer",
    importer_name="stock.valuation.layer.importer",
    sap_source="oitw",
    depends_on=["stock.quant.importer"],
    allow_multiprocessing=False,
)
class StockValuationLayerImporter(models.AbstractModel):
    _name = "stock.valuation.layer.importer"
    _description = "SAP Stock Valuation Layer Importer (OITW)"

    @ETL.extract("oitw")
    def extract_valuation_layers(self, ctx: ETLContext) -> Dict:
        """Extract stock valuation from SAP OITW table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            Dict containing valuation data and lookup mappings.
        """
        # Get existing product mappings
        products = ctx.env["product.product"].search([("sap_item_code", "!=", False)])
        product_map = {product.sap_item_code: product.id for product in products}

        # Query SAP OITW for inventory with value
        sql = """
            SELECT w.whscode, w.itemcode, w.onhand, w.avgprice
            FROM oitw w
            INNER JOIN oitm i ON w.itemcode = i.itemcode
            WHERE w.onhand > 0 
            AND i.validfor = 'Y'
            AND w.avgprice > 0
        """
        ctx.cr.execute(sql)
        sap_valuation = ctx.cr.dictfetchall()

        # Filter for products that exist in Odoo
        filtered_valuation = [
            val for val in sap_valuation if val["itemcode"] in product_map
        ]

        _logger.info(f"Extracted {len(filtered_valuation)} valuation records from SAP.")
        return {
            "valuation": filtered_valuation,
            "product_map": product_map,
            "company_id": ctx.env.company.id,
        }

    @ETL.transform()
    def transform_valuation_layers(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform SAP valuation into Odoo stock.valuation.layer values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of valuation layer value dictionaries ready for creation.
        """
        data = extracted.get("extract_valuation_layers") or {}
        sap_valuation = data.get("valuation", [])
        product_map = data.get("product_map", {})
        company_id = data.get("company_id")

        valuation_vals = []
        for sap_val in sap_valuation:
            product_id = product_map.get(sap_val["itemcode"])

            if not product_id:
                continue

            quantity = sap_val["onhand"]
            unit_cost = sap_val["avgprice"]
            total_value = quantity * unit_cost

            vals = {
                "product_id": product_id,
                "quantity": quantity,
                "unit_cost": unit_cost,
                "value": total_value,
                "company_id": company_id,
                "description": f"SAP initial inventory import - {sap_val['itemcode']}",
            }
            valuation_vals.append(vals)

        _logger.info(f"Transformed {len(valuation_vals)} valuation layer records.")
        return valuation_vals

    @ETL.load()
    def load_valuation_layers(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load valuation layers into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        valuation_vals = transformed.get("transform_valuation_layers") or []

        if valuation_vals:
            layers = ctx.env["stock.valuation.layer"].create(valuation_vals)
            _logger.info(f"Created {len(layers)} stock valuation layers.")
        else:
            _logger.info("No valuation layers to create.")
