"""xTuple Stock Quant ETL Pipeline

This module contains the ETL pipeline for importing inventory levels
from xTuple itemsite as stock quants in Odoo.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework.framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

SELECT_ITEMSITE_STOCK = """
    SELECT
        itemsite_id,
        itemsite_item_id,
        itemsite_qtyonhand,
        itemsite_location,
        item_number
    FROM itemsite
    LEFT JOIN item ON itemsite_item_id = item_id
    WHERE itemsite_qtyonhand > 0
"""


@ETL.pipeline(
    target_model="stock.quant",
    importer_name="xtuple.stock.quant.importer",
    depends_on=[
        "xtuple.product.importer",
    ],
)
class XtupleStockQuantImporter(models.AbstractModel):
    """ETL Pipeline for importing stock levels from xTuple itemsite."""

    _name = "xtuple.stock.quant.importer"
    _description = "xTuple Stock Quant Importer"

    @ETL.extract("itemsite")
    def extract_stock_levels(self, ctx: ETLContext) -> Dict:
        """Extract stock levels from xTuple itemsite table."""
        ctx.cr.execute(SELECT_ITEMSITE_STOCK)
        itemsite_records = ctx.cr.dictfetchall()
        _logger.info(
            f"Extracted {len(itemsite_records)} itemsite records with stock from xTuple"
        )

        # Get product mapping (only storable products that can have quants)
        ctx.env.cr.execute(
            """SELECT pp.xtuple_item_id, pp.id
               FROM product_product pp
               JOIN product_template pt ON pp.product_tmpl_id = pt.id
               WHERE pp.xtuple_item_id IS NOT NULL
               AND pt.is_storable = true"""
        )
        product_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Get default stock location (VJ/Stock or WH/Stock)
        warehouse = ctx.env["stock.warehouse"].search([], limit=1)
        stock_location_id = warehouse.lot_stock_id.id if warehouse else False

        return {
            "itemsite_records": itemsite_records,
            "product_map": product_map,
            "stock_location_id": stock_location_id,
        }

    @ETL.transform()
    def transform_quants(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple itemsite records into Odoo stock quant values."""
        data = extracted.get("extract_stock_levels", {})
        itemsite_records = data.get("itemsite_records", [])
        product_map = data.get("product_map", {})
        stock_location_id = data.get("stock_location_id")

        if not stock_location_id:
            _logger.error("No stock location found, cannot import stock levels")
            return []

        quant_vals = []
        skipped_no_product = 0

        for record in itemsite_records:
            item_id = record.get("itemsite_item_id")
            product_id = product_map.get(item_id)

            if not product_id:
                skipped_no_product += 1
                continue

            qty = float(record.get("itemsite_qtyonhand", 0) or 0)
            if qty <= 0:
                continue

            vals = {
                "product_id": product_id,
                "location_id": stock_location_id,
                "quantity": qty,
                "xtuple_itemsite_id": record.get("itemsite_id"),
            }
            quant_vals.append(vals)

        if skipped_no_product:
            _logger.warning(
                f"Skipped {skipped_no_product} itemsite records - product not found"
            )

        _logger.info(f"Transformed {len(quant_vals)} stock quant records")
        return quant_vals

    @ETL.load()
    def load_quants(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load stock quants into Odoo.

        Uses _update_available_quantity to properly create/update quants
        with inventory adjustment semantics.
        """
        quant_vals = transformed.get("transform_quants", [])
        if not quant_vals:
            _logger.info("No stock quants to create")
            return

        Quant = ctx.env["stock.quant"].with_context(inventory_mode=True)
        created_count = 0

        for vals in quant_vals:
            product = ctx.env["product.product"].browse(vals["product_id"])
            location = ctx.env["stock.location"].browse(vals["location_id"])

            # Check if quant already exists for this itemsite
            existing = Quant.search(
                [("xtuple_itemsite_id", "=", vals["xtuple_itemsite_id"])], limit=1
            )
            if existing:
                continue

            # Use _update_available_quantity for proper quant handling
            Quant._update_available_quantity(
                product,
                location,
                vals["quantity"],
            )

            # Find the created/updated quant and set the xtuple_itemsite_id
            quant = Quant.search(
                [
                    ("product_id", "=", product.id),
                    ("location_id", "=", location.id),
                ],
                limit=1,
            )
            if quant:
                quant.xtuple_itemsite_id = vals["xtuple_itemsite_id"]

            created_count += 1

        _logger.info(f"Created/updated {created_count} stock quants")
