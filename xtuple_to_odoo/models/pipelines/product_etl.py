"""xTuple Product ETL Pipelines

This module handles the migration of product data (categories, products, and
supplier info) from xTuple to Odoo using the ETL framework.

Pipeline execution order:
1. xtuple.product.category.importer - Import product categories
2. xtuple.product.importer - Import products
3. xtuple.product.supplierinfo.importer - Import supplier info
"""

import logging
from typing import Any, Dict, List

from odoo import api, models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# Common SQL query parts
PRODUCT_SELECT = """
    item_id,
    item_number,
    item_descrip1,
    item_descrip2,
    item_active,
    item_type,
    item_upccode,
    item_prodcat_id,
    item_sold,
    item_fractional,
    item_inv_uom_id,
    item_price_uom_id,
    item_maxcost,
    item_listprice,
    item_listcost,
    item_classcode_id,
    invuom.uom_name as inv_uom_name,
    priceuom.uom_name as price_uom_name,
    prodcat_code,
    prodcat_descrip,
    classcode_code
"""

PRODUCT_CATEGORY_SELECT = """
    prodcat_id,
    prodcat_code,
    prodcat_descrip
"""

PRODUCT_SUPPLIER_SELECT = """
    itemsrc_id,
    itemsrc_item_id,
    itemsrc_vend_id,
    itemsrc_vend_item_number,
    itemsrc_vend_item_descrip,
    itemsrc_vend_uom,
    itemsrc_minordqty,
    itemsrc_leadtime,
    itemsrc_active,
    itemsrc_default,
    vend_number,
    vend_name
"""


# =============================================================================
# Product Category Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="product.category",
    importer_name="xtuple.product.category.importer",
    sap_source="prodcat",
    depends_on=["xtuple.uom.precision.importer"],
)
class XtupleProductCategoryImporter(models.AbstractModel):
    _name = "xtuple.product.category.importer"
    _description = "xTuple Product Category Importer"

    @ETL.extract("prodcat")
    def extract_categories(self, ctx: ETLContext) -> List[Dict]:
        """Extract product categories from xTuple prodcat table."""
        ctx.env.cr.execute(
            "SELECT xtuple_prodcat_id FROM product_category WHERE xtuple_prodcat_id IS NOT NULL"
        )
        existing_prodcat_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(
            f"Found {len(existing_prodcat_ids)} existing product categories in Odoo"
        )

        select_clause = f"""
        SELECT
            {PRODUCT_CATEGORY_SELECT}
        FROM prodcat
        """

        if existing_prodcat_ids:
            where_clause = "WHERE prodcat_id NOT IN %s"
            ctx.cr.execute(select_clause + where_clause, (tuple(existing_prodcat_ids),))
        else:
            ctx.cr.execute(select_clause)

        categories = ctx.cr.dictfetchall()

        _logger.info(f"Extracted {len(categories)} new product categories from xTuple")
        return categories

    @ETL.transform()
    def transform_categories(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple categories into Odoo category values."""
        categories = extracted.get("extract_categories", [])

        parent_category = ctx.env.ref(
            "product.product_category_all", raise_if_not_found=False
        )

        category_vals = []
        for category in categories:
            name = category.get("prodcat_descrip", "")
            if not name:
                name = category.get("prodcat_code", "")

            category_vals.append(
                {
                    "name": name,
                    "parent_id": parent_category and parent_category.id or False,
                    "xtuple_prodcat_id": category.get("prodcat_id"),
                    "xtuple_prodcat_code": category.get("prodcat_code"),
                }
            )

        _logger.info(f"Transformed {len(category_vals)} category records")
        return category_vals

    @ETL.load()
    def load_categories(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load categories into Odoo."""
        category_vals = transformed.get("transform_categories", [])
        if category_vals:
            categories = ctx.env["product.category"].create(category_vals)
            _logger.info(f"Created {len(categories)} product categories")
        else:
            _logger.info("No new categories to create")


# =============================================================================
# Product Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="product.product",
    importer_name="xtuple.product.importer",
    sap_source="item",
    depends_on=["xtuple.product.category.importer"],
    allow_multiprocessing=True,
    multiprocessing_threshold=500,
)
class XtupleProductImporter(models.AbstractModel):
    _name = "xtuple.product.importer"
    _description = "xTuple Product Importer"

    @ETL.extract("item")
    def extract_products(self, ctx: ETLContext) -> List[Dict]:
        """Extract products from xTuple item table."""
        ctx.env.cr.execute(
            "SELECT xtuple_item_id FROM product_product WHERE xtuple_item_id IS NOT NULL"
        )
        existing_item_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(f"Found {len(existing_item_ids)} existing products in Odoo")

        # Get existing default_codes for deduplication (cross-system matching)
        ctx.env.cr.execute(
            "SELECT default_code FROM product_product WHERE default_code IS NOT NULL AND default_code != ''"
        )
        existing_default_codes = {row[0] for row in ctx.env.cr.fetchall()}
        _logger.info(
            f"Found {len(existing_default_codes)} existing products by default_code for deduplication"
        )

        select_clause = f"""
        SELECT
            {PRODUCT_SELECT}
        FROM item
        LEFT JOIN uom invuom ON (item_inv_uom_id = invuom.uom_id)
        LEFT JOIN uom priceuom ON (item_price_uom_id = priceuom.uom_id)
        LEFT JOIN prodcat ON (item_prodcat_id = prodcat_id)
        LEFT JOIN classcode ON (item_classcode_id = classcode_id)
        """

        if existing_item_ids:
            where_clause = "WHERE item_id NOT IN %s"
            ctx.cr.execute(select_clause + where_clause, (tuple(existing_item_ids),))
        else:
            ctx.cr.execute(select_clause)

        products = ctx.cr.dictfetchall()

        # Filter out products that match existing by item_number (deduplication)
        deduped_products = [
            p
            for p in products
            if not p.get("item_number")
            or p.get("item_number") not in existing_default_codes
        ]

        _logger.info(
            f"Extracted {len(products)} products from xTuple, "
            f"{len(deduped_products)} after deduplication by item_number"
        )
        return deduped_products

    @ETL.transform()
    def transform_products(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple products into Odoo product values."""
        products = extracted.get("extract_products", [])

        # Build category lookup dict
        ctx.env.cr.execute(
            "SELECT xtuple_prodcat_id, id FROM product_category WHERE xtuple_prodcat_id IS NOT NULL"
        )
        category_dict = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Get default category
        all_category = ctx.env.ref(
            "product.product_category_all", raise_if_not_found=False
        )

        product_vals = []
        for product in products:
            # Determine product type based on xTuple item_type
            item_type = product.get("item_type", "")
            product_type = "consu"
            is_storable = False
            tracking = "none"

            if item_type == "P":  # Purchased
                product_type = "consu"
                is_storable = True
            elif item_type == "M":  # Manufactured
                product_type = "consu"
                is_storable = True
            elif item_type == "F":  # Phantom
                product_type = "consu"
                is_storable = False
            elif item_type in ("R", "S", "O"):  # Reference, Service, Outside Processing
                product_type = "service"
                is_storable = False
            elif item_type in ("K", "C", "Y"):  # Kit, Co-Product, By-Product
                product_type = "consu"
                is_storable = True
            else:
                product_type = "consu"
                is_storable = False

            # Determine if product is sold (based on category)
            is_sold = product.get("item_prodcat_id") in [28, 31, 32, 33, 34]

            # Get product name
            name = product.get("item_descrip1", "")
            if not name:
                name = product.get("item_number", "")

            description = product.get("item_descrip2", "")

            # Get category
            category_id = category_dict.get(product.get("item_prodcat_id"))
            if not category_id:
                category_id = all_category and all_category.id or False

            # Get UoM
            uom = self._map_xtuple_uom_to_odoo(ctx, product.get("item_inv_uom_id"))
            uom_id = uom and uom.id or False

            # Get price and cost
            list_price = product.get("item_listprice", 0.0)
            standard_price = product.get("item_listcost", 0.0)
            if not standard_price and product.get("item_maxcost"):
                standard_price = product.get("item_maxcost", 0.0)

            product_vals.append(
                {
                    "name": name,
                    "description": description,
                    "default_code": product.get("item_number"),
                    "barcode": product.get("item_upccode"),
                    "type": product_type,
                    "tracking": tracking,
                    "is_storable": is_storable,
                    "categ_id": category_id,
                    "uom_id": uom_id,
                    "active": product.get("item_active"),
                    "sale_ok": is_sold,
                    "purchase_ok": item_type in ["P", "M", "F"],
                    "list_price": list_price,
                    "standard_price": standard_price,
                    "xtuple_item_id": product.get("item_id"),
                    "xtuple_item_number": product.get("item_number"),
                    "xtuple_item_type": item_type,
                    "xtuple_classcode": product.get("classcode_code"),
                }
            )

        _logger.info(f"Transformed {len(product_vals)} product records")
        return product_vals

    def _map_xtuple_uom_to_odoo(self, ctx: ETLContext, xtuple_uom_id):
        """Map xTuple UoM IDs to Odoo UoM records.

        In Odoo 19, UoM categories were removed. We map to standard UoMs only.
        """
        default_uom = ctx.env.ref("uom.product_uom_unit", raise_if_not_found=False)

        if not xtuple_uom_id:
            return default_uom

        # Map xTuple UoM IDs to Odoo XML IDs
        # For custom UoMs (Case, Pallet, etc.), fall back to Unit
        uom_mapping = {
            4: "uom.product_uom_unit",  # EA -> Unit
            5: "uom.product_uom_unit",  # CS (Case) -> Unit (no standard equivalent)
            6: "uom.product_uom_unit",  # PL (Pallet) -> Unit (no standard equivalent)
            7: "uom.product_uom_kgm",  # KG -> kg
            8: "uom.product_uom_litre",  # L -> Liter
            9: "uom.product_uom_lb",  # LB -> lb
            10: "uom.product_uom_gal",  # USGAL -> gal (US)
            11: "uom.product_uom_gal",  # IMP GAL -> gal (US) as fallback
            12: "uom.product_uom_ton",  # THSND -> Ton
            13: "uom.product_uom_yard",  # YD -> Yard
            14: "uom.product_uom_foot",  # FT -> Foot
        }

        xmlid = uom_mapping.get(xtuple_uom_id)
        if not xmlid:
            _logger.warning(f"No mapping found for xTuple UoM ID: {xtuple_uom_id}")
            return default_uom

        uom = ctx.env.ref(xmlid, raise_if_not_found=False)
        return uom or default_uom

    @ETL.load()
    def load_products(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load products into Odoo."""
        product_vals = transformed.get("transform_products", [])
        if product_vals:
            products = ctx.env["product.product"].create(product_vals)
            _logger.info(f"Created {len(products)} products")

            # Mark templates inactive for inactive variants
            inactive_products = ctx.env["product.product"].search(
                [("active", "=", False), ("xtuple_item_id", "!=", False)]
            )
            inactive_products.product_tmpl_id.write({"active": False})
        else:
            _logger.info("No new products to create")


# =============================================================================
# Product Supplier Info Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="product.supplierinfo",
    importer_name="xtuple.product.supplierinfo.importer",
    sap_source="itemsrc",
    depends_on=["xtuple.product.importer", "xtuple.partner.vendor.importer"],
)
class XtupleProductSupplierInfoImporter(models.AbstractModel):
    _name = "xtuple.product.supplierinfo.importer"
    _description = "xTuple Product Supplier Info Importer"

    @ETL.extract("itemsrc")
    def extract_supplierinfo(self, ctx: ETLContext) -> Dict[str, Any]:
        """Extract product supplier info from xTuple itemsrc table."""
        ctx.env.cr.execute(
            "SELECT xtuple_itemsrc_id FROM product_supplierinfo WHERE xtuple_itemsrc_id IS NOT NULL"
        )
        existing_itemsrc_ids = [row[0] for row in ctx.env.cr.fetchall()]
        _logger.info(
            f"Found {len(existing_itemsrc_ids)} existing product suppliers in Odoo"
        )

        select_clause = f"""
        SELECT
            {PRODUCT_SUPPLIER_SELECT}
        FROM itemsrc
        JOIN vendinfo ON (itemsrc_vend_id = vend_id)
        """

        if existing_itemsrc_ids:
            where_clause = "WHERE itemsrc_id NOT IN %s"
            ctx.cr.execute(select_clause + where_clause, (tuple(existing_itemsrc_ids),))
        else:
            ctx.cr.execute(select_clause)

        suppliers = ctx.cr.dictfetchall()

        # Get product mapping
        ctx.env.cr.execute(
            "SELECT xtuple_item_id, product_tmpl_id FROM product_product WHERE xtuple_item_id IS NOT NULL"
        )
        product_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Get vendor mapping
        ctx.env.cr.execute(
            "SELECT xtuple_vend_id, id FROM res_partner WHERE xtuple_vend_id IS NOT NULL"
        )
        vendor_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        _logger.info(f"Extracted {len(suppliers)} product suppliers from xTuple")
        return {
            "suppliers": suppliers,
            "product_map": product_map,
            "vendor_map": vendor_map,
        }

    @ETL.transform()
    def transform_supplierinfo(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple supplier info into Odoo supplierinfo values."""
        data = extracted.get("extract_supplierinfo", {})
        suppliers = data.get("suppliers", [])
        product_map = data.get("product_map", {})
        vendor_map = data.get("vendor_map", {})

        supplier_info_vals = []
        for supplier in suppliers:
            product_tmpl_id = product_map.get(supplier.get("itemsrc_item_id"))
            vendor_id = vendor_map.get(supplier.get("itemsrc_vend_id"))

            if not product_tmpl_id or not vendor_id:
                continue

            supplier_info_vals.append(
                {
                    "product_tmpl_id": product_tmpl_id,
                    "partner_id": vendor_id,
                    "product_name": supplier.get("itemsrc_vend_item_descrip"),
                    "product_code": supplier.get("itemsrc_vend_item_number"),
                    "min_qty": supplier.get("itemsrc_minordqty", 0.0),
                    "delay": supplier.get("itemsrc_leadtime", 0),
                    "xtuple_itemsrc_id": supplier.get("itemsrc_id"),
                    "xtuple_default": supplier.get("itemsrc_default", False),
                }
            )

        _logger.info(f"Transformed {len(supplier_info_vals)} supplier info records")
        return supplier_info_vals

    @ETL.load()
    def load_supplierinfo(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load supplier info into Odoo."""
        supplier_info_vals = transformed.get("transform_supplierinfo", [])
        if supplier_info_vals:
            supplierinfos = ctx.env["product.supplierinfo"].create(supplier_info_vals)
            _logger.info(f"Created {len(supplierinfos)} product supplier records")
        else:
            _logger.info("No new supplier info to create")


# =============================================================================
# Product Linker Pipeline (for deduplication)
# =============================================================================


@ETL.pipeline(
    target_model="product.product",
    importer_name="xtuple.product.linker",
    sap_source="item",
    depends_on=["xtuple.product.importer"],
)
class XtupleProductLinker(models.AbstractModel):
    """ETL Pipeline for linking existing products to xTuple items by default_code."""

    _name = "xtuple.product.linker"
    _description = "xTuple Product Linker"

    @ETL.extract("item")
    def extract_products_for_linking(self, ctx: ETLContext) -> List[Dict]:
        """Extract products from xTuple that need linking."""
        select_clause = f"""
        SELECT
            item_id,
            item_number
        FROM item
        WHERE item_number IS NOT NULL AND item_number != ''
        """
        ctx.cr.execute(select_clause)
        products = ctx.cr.dictfetchall()

        _logger.info(f"Extracted {len(products)} products with item_number for linking")
        return products

    @ETL.transform()
    def transform_products_for_linking(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Find existing products by default_code and prepare link updates."""
        products = extracted.get("extract_products_for_linking", [])

        # Build lookup of existing products by default_code that don't have xtuple_item_id
        ctx.env.cr.execute(
            """
            SELECT id, default_code FROM product_product
            WHERE default_code IS NOT NULL AND default_code != ''
            AND xtuple_item_id IS NULL
            """
        )
        product_by_code = {row[1]: row[0] for row in ctx.env.cr.fetchall()}

        # Get item IDs already assigned to products (to avoid duplicates)
        ctx.env.cr.execute(
            "SELECT xtuple_item_id FROM product_product WHERE xtuple_item_id IS NOT NULL"
        )
        existing_item_ids = {row[0] for row in ctx.env.cr.fetchall()}

        link_updates = []
        for product in products:
            item_id = product.get("item_id")
            # Skip if this item ID is already assigned to another product
            if item_id in existing_item_ids:
                continue
            item_number = product.get("item_number")
            if item_number and item_number in product_by_code:
                link_updates.append(
                    {
                        "product_id": product_by_code[item_number],
                        "xtuple_item_id": item_id,
                        "xtuple_item_number": item_number,
                    }
                )

        _logger.info(f"Found {len(link_updates)} products to link by default_code")
        return link_updates

    @ETL.load()
    def load_product_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing products with xTuple item IDs."""
        link_updates = transformed.get("transform_products_for_linking", [])

        if not link_updates:
            _logger.info("No products to link")
            return

        for update in link_updates:
            ctx.env.cr.execute(
                """
                UPDATE product_product
                SET xtuple_item_id = %s, xtuple_item_number = %s
                WHERE id = %s
                """,
                (
                    update["xtuple_item_id"],
                    update["xtuple_item_number"],
                    update["product_id"],
                ),
            )
            _logger.debug(
                f"Linked product {update['product_id']} (default_code={update['xtuple_item_number']}) "
                f"to xTuple item {update['xtuple_item_id']}"
            )

        _logger.info(f"Linked {len(link_updates)} existing products to xTuple items")
