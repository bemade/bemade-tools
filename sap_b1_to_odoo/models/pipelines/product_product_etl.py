import logging
from typing import Dict, List

from odoo import models
from odoo.sql_db import SQL

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.sap_b1_to_odoo.tools import fix_quotes

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="product.product",
    importer_name="product.product.importer",
    sap_source="oitm",
    depends_on=["product.category.importer"],
    multiprocessing_threshold=500,
    chunk_size=500,
    max_workers=8,
)
class ProductImporter(models.AbstractModel):
    _name = "product.product.importer"
    _description = "SAP Product Importer (OITM)"

    # Class-level cache for lookup dictionaries
    _lookup_cache = {}

    @ETL.extract("oitm")
    def extract_products(self, ctx: ETLContext) -> List[Dict]:
        """Extract products from SAP OITM table.

        Also pre-computes category mapping for use in transform phase.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of product dictionaries from SAP.
        """
        # Get existing products to avoid duplicates
        existing_products = tuple(
            [
                p["sap_item_code"]
                for p in ctx.env["product.product"].search_read(
                    [
                        ("sap_item_code", "!=", False),
                        ("active", "in", [True, False]),
                    ],
                    ["sap_item_code"],
                )
            ]
        )
        _logger.info(f"Found {len(existing_products)} existing products.")

        # Query SAP — join ITM1 to get base pricelist price (listnum 3)
        sql = (
            "SELECT oitm.*, itm1.price AS base_price"
            " FROM oitm"
            " LEFT JOIN itm1 ON oitm.itemcode = itm1.itemcode"
            "   AND itm1.pricelist = 3"
        )
        if existing_products:
            sql += " WHERE oitm.itemcode NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_products))
        else:
            ctx.cr.execute(sql)

        sap_products = ctx.cr.dictfetchall()

        # Pre-compute category mapping
        categories = ctx.env["product.category"].search(
            [("sap_itms_grp_cod", "!=", False)]
        )
        categories_map = {cat.sap_itms_grp_cod: cat.id for cat in categories}

        # Get company ID
        company_id = ctx.env.company.id

        ProductImporter._lookup_cache = {
            "categories_map": categories_map,
            "company_id": company_id,
        }

        return sap_products

    @ETL.transform()
    def transform_products(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP products into Odoo product values.

        Uses pre-computed category mapping from extract phase.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of product value dictionaries ready for creation.
        """
        sap_products = extracted["extract_products"]

        # Use pre-computed lookups from class cache
        cache = ProductImporter._lookup_cache
        if not cache:
            raise RuntimeError("Cache is empty in transform! This should never happen.")

        categories_map = cache["categories_map"]
        company_id = cache["company_id"]

        product_vals = []
        for sap_product in sap_products:
            # Determine category
            categ_id = (
                categories_map.get(sap_product["itmsgrpcod"])
                if sap_product["itmsgrpcod"]
                else False
            )

            # Determine name and default_code based on what's filled
            # Standard SAP B1: frgnname = display name, itemname = internal reference
            # If only one is filled, use it as the name and leave default_code empty
            itemname = fix_quotes(sap_product["itemname"])
            frgnname = fix_quotes(sap_product["frgnname"])

            if itemname and frgnname:
                # Both filled: standard SAP B1 convention
                name = frgnname
                default_code = itemname
            elif frgnname:
                # Only frgnname filled
                name = frgnname
                default_code = False
            elif itemname:
                # Only itemname filled
                name = itemname
                default_code = False
            else:
                # Neither filled (shouldn't happen)
                name = "N/A"
                default_code = False

            # Build product values
            base_price = sap_product.get("base_price") or 0.0
            avg_price = float(sap_product.get("avgprice") or 0.0)
            vals = {
                "sap_item_code": sap_product["itemcode"],
                "sap_atcentry": sap_product["atcentry"],
                "name": name,
                "default_code": default_code,
                "list_price": float(base_price),
                "standard_price": avg_price,
                "sale_ok": sap_product["sellitem"] == "Y",
                "purchase_ok": sap_product["prchseitem"] == "Y",
                "active": sap_product["validfor"] == "Y",
                "type": "consu",
                "is_storable": True,
                "company_id": company_id,
            }

            if categ_id:
                vals["categ_id"] = categ_id

            product_vals.append(vals)

        return product_vals

    @ETL.load()
    def load_products(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load products into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        product_vals = transformed["transform_products"]

        if product_vals:
            products = ctx.env["product.product"].create(product_vals)
            _logger.info(f"Created {len(products)} products.")
        else:
            _logger.info("No new products to create.")
