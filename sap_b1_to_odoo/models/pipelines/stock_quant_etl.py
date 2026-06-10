import logging
from typing import Dict, List

from odoo import api, models

from odoo.addons.etl_framework import ETL, ETLContext

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
        "mrp.production.postprocess",
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

        # Filter for products and warehouses that exist in Odoo;
        # collect skipped rows (onhand > 0) so they are not silently lost.
        filtered_quants = []
        skipped_no_product = []
        skipped_no_warehouse = []
        for quant in sap_quants:
            if quant["itemcode"] not in product_map:
                skipped_no_product.append(quant)
            elif quant["whscode"] not in warehouse_location_map:
                skipped_no_warehouse.append(quant)
            else:
                filtered_quants.append(quant)

        if skipped_no_product:
            sample = [q["itemcode"] for q in skipped_no_product[:5]]
            _logger.warning(
                "stock_quant extract: skipped %d onhand>0 row(s) — item code not in "
                "product map (no matching sap_item_code in Odoo). Sample: %s",
                len(skipped_no_product),
                sample,
            )
        if skipped_no_warehouse:
            sample = [
                (q["itemcode"], q["whscode"]) for q in skipped_no_warehouse[:5]
            ]
            _logger.warning(
                "stock_quant extract: skipped %d onhand>0 row(s) — warehouse code not in "
                "warehouse_location_map (no matching sap_whs_code in Odoo). Sample: %s",
                len(skipped_no_warehouse),
                sample,
            )

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
        transform_skipped = []
        for sap_quant in sap_quants:
            product_id = product_map.get(sap_quant["itemcode"])
            location_id = warehouse_location_map.get(sap_quant["whscode"])

            if not product_id or not location_id:
                transform_skipped.append(
                    (sap_quant["itemcode"], sap_quant["whscode"])
                )
                continue

            vals = {
                "product_id": product_id,
                "location_id": location_id,
                "quantity": sap_quant["onhand"],
                "reserved_quantity": sap_quant["iscommited"] or 0,
                "company_id": company_id,
            }
            quant_vals.append(vals)

        if transform_skipped:
            _logger.warning(
                "stock_quant transform: skipped %d row(s) — product or location "
                "missing from maps (should have been caught at extract). Sample: %s",
                len(transform_skipped),
                transform_skipped[:5],
            )
        _logger.info(f"Transformed {len(quant_vals)} stock quant records.")
        return quant_vals

    @ETL.load()
    def load_stock_quants(self, ctx: ETLContext, transformed: Dict) -> None:
        """Zero out all internal quants then set SAP quantities.

        Stock moves from SO/PO/MRP imports leave behind quants that may not
        match SAP. We reset everything to zero first, then write the SAP
        on-hand values so Odoo matches SAP exactly.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        quant_vals = transformed.get("transform_stock_quants") or []
        Quant = ctx.env["stock.quant"].sudo()

        # Zero out all existing internal quants
        internal_quants = Quant.search(
            [("location_id.usage", "=", "internal")]
        )
        if internal_quants:
            _logger.info(
                f"Zeroing out {len(internal_quants)} existing internal quants."
            )
            internal_quants.write({"quantity": 0, "reserved_quantity": 0})

        if not quant_vals:
            _logger.info("No stock quants to import from SAP.")
            return

        created_count = 0
        for vals in quant_vals:
            product = ctx.env["product.product"].browse(vals["product_id"])
            location = ctx.env["stock.location"].browse(vals["location_id"])

            # Use _gather to find existing quant
            existing_quant = Quant._gather(product, location, strict=False)

            if existing_quant:
                existing_quant.write(
                    {
                        "quantity": vals["quantity"],
                        "reserved_quantity": vals["reserved_quantity"],
                    }
                )
            else:
                Quant.create(vals)
            created_count += 1

        _logger.info(f"Processed {created_count} stock quant records.")

