import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData
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

    @ETL.extract("oitm")
    def extract_products(self, ctx: ETLContext) -> ChunkableData:
        """Extract products from SAP OITM table.

        Also pre-computes category mapping for use in transform phase.
        All OITM rows are fetched; load_products performs an upsert by
        sap_item_code so that re-imports update existing records.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of product dictionaries from SAP.
        """
        # Query SAP — join ITM1 to get base pricelist price (listnum 3)
        # No NOT-IN filter: all rows are fetched for upsert behaviour.
        sql = (
            "SELECT oitm.*, itm1.price AS base_price"
            " FROM oitm"
            " LEFT JOIN itm1 ON oitm.itemcode = itm1.itemcode"
            "   AND itm1.pricelist = 3"
        )
        ctx.cr.execute(sql)

        sap_products = ctx.cr.dictfetchall()
        _logger.info(f"Fetched {len(sap_products)} products from SAP.")

        # Pre-compute category mapping
        categories = ctx.env["product.category"].search(
            [("sap_itms_grp_cod", "!=", False)]
        )
        categories_map = {cat.sap_itms_grp_cod: cat.id for cat in categories}

        # Get company ID
        company_id = ctx.env.company.id

        return ChunkableData(
            records=sap_products,
            context={
                "categories_map": categories_map,
                "company_id": company_id,
            },
        )

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
        data = extracted["extract_products"]
        sap_products = data.records
        cache = data.context

        categories_map = cache["categories_map"]
        company_id = cache["company_id"]

        product_vals = []
        category_miss_codes = []
        for sap_product in sap_products:
            # Determine category
            itmsgrpcod = sap_product["itmsgrpcod"]
            categ_id = categories_map.get(itmsgrpcod) if itmsgrpcod else False
            if itmsgrpcod and not categ_id:
                category_miss_codes.append((sap_product["itemcode"], itmsgrpcod))

            # Name: itemname, else "N/A"
            itemname = fix_quotes(sap_product["itemname"])
            frgnname = fix_quotes(sap_product["frgnname"])
            name = itemname or "N/A"

            # default_code ← suppcatnum (supplier catalog number from SAP)
            suppcatnum = fix_quotes(sap_product.get("suppcatnum"))
            default_code = suppcatnum or False

            # description_sale ← frgnname (Sales tab "Sales Description" field)
            description_sale = frgnname or False

            # Build product values
            base_price = sap_product.get("base_price") or 0.0
            avg_price = float(sap_product.get("avgprice") or 0.0)
            description_ecommerce = (
                fix_quotes(sap_product.get("u_salesextdescription")) or False
            )
            vals = {
                "sap_item_code": sap_product["itemcode"],
                "sap_atcentry": sap_product["atcentry"],
                "name": name,
                "default_code": default_code,
                "description_sale": description_sale,
                "list_price": float(base_price),
                "standard_price": avg_price,
                "description_ecommerce": description_ecommerce,
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

        if category_miss_codes:
            _logger.warning(
                "product_product transform: %d product(s) had itmsgrpcod not found in "
                "categories_map — categ_id left False (product still created). "
                "Sample (itemcode, itmsgrpcod): %s",
                len(category_miss_codes),
                category_miss_codes[:5],
            )
        return product_vals

    @ETL.load()
    def load_products(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load products into Odoo using upsert by sap_item_code.

        Existing records are updated; new sap_item_codes are created.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        product_vals = transformed["transform_products"]

        if not product_vals:
            _logger.info("No products to load.")
            return

        # Build a lookup of existing products keyed by sap_item_code
        existing = {
            p["sap_item_code"]: p["id"]
            for p in ctx.env["product.product"].with_context(active_test=False).search_read(
                [("sap_item_code", "!=", False)],
                ["sap_item_code"],
            )
        }

        to_create = []
        to_write = []  # list of (id, vals)
        for vals in product_vals:
            sap_item_code = vals["sap_item_code"]
            if sap_item_code in existing:
                to_write.append((existing[sap_item_code], vals))
            else:
                to_create.append(vals)

        if to_create:
            products = ctx.env["product.product"].create(to_create)
            _logger.info(f"Created {len(products)} products.")

        if to_write:
            for product_id, vals in to_write:
                ctx.env["product.product"].browse(product_id).write(vals)
            _logger.info(f"Updated {len(to_write)} products.")
