import logging
from typing import Dict, List

from odoo import api, models

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="stock.quant",
    importer_name="stock.quant.importer",
    sap_source="oitw",
    depends_on=[
        "product.product.importer",
        "stock.warehouse.importer",
        # Run after all transactional imports that create stock moves
        "sale.order.post.processor",
        "purchase.order.post.processor",
    ],
    allow_multiprocessing=False,
)
class StockQuantImporter(models.AbstractModel):
    _name = "stock.quant.importer"
    _description = "SAP Stock Quant Importer (OITW)"

    @ETL.extract("oitw")
    def extract_stock_quants(self, ctx: ETLContext) -> Dict:
        """Extract stock quantities from SAP OITW table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            Dict containing stock quants and lookup mappings.
        """
        # Get existing product mappings
        products = ctx.env["product.product"].search([("sap_item_code", "!=", False)])
        product_map = {product.sap_item_code: product.id for product in products}

        # Get warehouses and their stock locations - map by SAP warehouse code
        warehouses = ctx.env["stock.warehouse"].search([("active", "=", True)])
        warehouse_location_map = {
            wh.sap_whs_code: wh.lot_stock_id.id for wh in warehouses if wh.sap_whs_code
        }

        # Query SAP OITW (warehouse item stock)
        sql = """
            SELECT w.whscode, w.itemcode, w.onhand, w.iscommited, w.avgprice
            FROM oitw w
            INNER JOIN oitm i ON w.itemcode = i.itemcode
            WHERE w.onhand > 0 
            AND i.validfor = 'Y'
        """
        ctx.cr.execute(sql)
        sap_quants = ctx.cr.dictfetchall()

        # Filter for products and warehouses that exist in Odoo
        filtered_quants = [
            quant
            for quant in sap_quants
            if quant["itemcode"] in product_map
            and quant["whscode"] in warehouse_location_map
        ]

        _logger.info(f"Extracted {len(filtered_quants)} stock quants from SAP OITW.")
        return {
            "quants": filtered_quants,
            "product_map": product_map,
            "warehouse_location_map": warehouse_location_map,
            "company_id": ctx.env.company.id,
        }

    @ETL.transform()
    def transform_stock_quants(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP stock quants into Odoo stock.quant values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of stock quant value dictionaries ready for creation.
        """
        data = extracted.get("extract_stock_quants") or {}
        sap_quants = data.get("quants", [])
        product_map = data.get("product_map", {})
        warehouse_location_map = data.get("warehouse_location_map", {})
        company_id = data.get("company_id")

        quant_vals = []
        for sap_quant in sap_quants:
            product_id = product_map.get(sap_quant["itemcode"])
            location_id = warehouse_location_map.get(sap_quant["whscode"])

            if not product_id or not location_id:
                continue

            vals = {
                "product_id": product_id,
                "location_id": location_id,
                "quantity": sap_quant["onhand"],
                "reserved_quantity": sap_quant["iscommited"] or 0,
                "company_id": company_id,
            }
            quant_vals.append(vals)

        _logger.info(f"Transformed {len(quant_vals)} stock quant records.")
        return quant_vals

    @ETL.load()
    def load_stock_quants(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load stock quants into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        quant_vals = transformed.get("transform_stock_quants") or []

        if not quant_vals:
            _logger.info("No stock quants to create.")
            return

        created_count = 0
        for vals in quant_vals:
            product = ctx.env["product.product"].browse(vals["product_id"])
            location = ctx.env["stock.location"].browse(vals["location_id"])

            # Use _gather to find existing quant
            existing_quant = ctx.env["stock.quant"]._gather(
                product, location, strict=False
            )

            if existing_quant:
                existing_quant.sudo().write(
                    {
                        "quantity": vals["quantity"],
                        "reserved_quantity": vals["reserved_quantity"],
                    }
                )
            else:
                ctx.env["stock.quant"].sudo().create(vals)
            created_count += 1

        _logger.info(f"Processed {created_count} stock quant records.")


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
