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

from odoo import models

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
    classcode_code,
    COALESCE(itemsite_tracking.item_controlmethod, 'N') as item_controlmethod
"""

# Subquery to get the most restrictive lot tracking method across all warehouses.
# Priority: S (serial) > R/L (lot) > N (none)
ITEMSITE_TRACKING_JOIN = """
    LEFT JOIN (
        SELECT
            itemsite_item_id,
            CASE
                WHEN bool_or(itemsite_controlmethod = 'S') THEN 'S'
                WHEN bool_or(itemsite_controlmethod IN ('R', 'L')) THEN 'R'
                ELSE 'N'
            END AS item_controlmethod
        FROM itemsite
        GROUP BY itemsite_item_id
    ) itemsite_tracking ON (item_id = itemsite_tracking.itemsite_item_id)
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
            "product.product_category_goods", raise_if_not_found=False
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

    def _get_lookup_dicts(self, ctx: ETLContext):
        """Build lookup dicts for category and UoM mapping."""
        # Build category lookup dict
        ctx.env.cr.execute(
            "SELECT xtuple_prodcat_id, id FROM product_category WHERE xtuple_prodcat_id IS NOT NULL"
        )
        category_dict = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        all_category = ctx.env.ref(
            "product.product_category_goods", raise_if_not_found=False
        )
        default_category_id = all_category.id if all_category else False

        # Build UoM lookup dict by querying xTuple's uom table and mapping by name.
        # This avoids relying on installation-specific integer IDs.
        uom_name_to_xmlid = {
            "EA": "uom.product_uom_unit",
            "CS": "uom.product_uom_unit",
            "PL": "uom.product_uom_unit",
            "EACH": "uom.product_uom_unit",
            "KG": "uom.product_uom_kgm",
            "KGM": "uom.product_uom_kgm",
            "L": "uom.product_uom_litre",
            "LT": "uom.product_uom_litre",
            "LITRE": "uom.product_uom_litre",
            "LITER": "uom.product_uom_litre",
            "LB": "uom.product_uom_lb",
            "USGAL": "uom.product_uom_gal",
            "GAL": "uom.product_uom_gal",
            "IMP GAL": "uom.product_uom_gal",
            "THSND": "uom.product_uom_ton",
            "YD": "uom.product_uom_yard",
            "FT": "uom.product_uom_foot",
        }
        default_uom = ctx.env.ref("uom.product_uom_unit", raise_if_not_found=False)
        default_uom_id = default_uom.id if default_uom else False

        # Resolve xmlids to Odoo DB ids once
        xmlid_to_odoo_id = {}
        for xmlid in set(uom_name_to_xmlid.values()):
            uom = ctx.env.ref(xmlid, raise_if_not_found=False)
            xmlid_to_odoo_id[xmlid] = uom.id if uom else default_uom_id

        # Query xTuple uom table and build id -> Odoo uom_id mapping by name
        ctx.cr.execute("SELECT uom_id, uom_name FROM uom")
        uom_dict = {}
        for row in ctx.cr.dictfetchall():
            uom_name = (row["uom_name"] or "").strip().upper()
            xmlid = uom_name_to_xmlid.get(uom_name)
            uom_dict[row["uom_id"]] = (
                xmlid_to_odoo_id[xmlid] if xmlid else default_uom_id
            )

        return category_dict, default_category_id, uom_dict, default_uom_id

    @ETL.extract("item")
    def extract_new_products(self, ctx: ETLContext) -> List[Dict]:
        """Extract new products from xTuple that don't exist in Odoo."""
        ctx.env.cr.execute(
            "SELECT xtuple_item_id FROM product_product WHERE xtuple_item_id IS NOT NULL"
        )
        existing_item_ids = [row[0] for row in ctx.env.cr.fetchall()]

        ctx.env.cr.execute(
            "SELECT default_code FROM product_product WHERE default_code IS NOT NULL AND default_code != ''"
        )
        existing_default_codes = {row[0] for row in ctx.env.cr.fetchall()}

        category_dict, default_category_id, uom_dict, default_uom_id = (
            self._get_lookup_dicts(ctx)
        )

        select_clause = f"""
        SELECT
            {PRODUCT_SELECT}
        FROM item
        LEFT JOIN uom invuom ON (item_inv_uom_id = invuom.uom_id)
        LEFT JOIN uom priceuom ON (item_price_uom_id = priceuom.uom_id)
        LEFT JOIN prodcat ON (item_prodcat_id = prodcat_id)
        LEFT JOIN classcode ON (item_classcode_id = classcode_id)
        {ITEMSITE_TRACKING_JOIN}
        """

        if existing_item_ids:
            where_clause = "WHERE item_id NOT IN %s"
            ctx.cr.execute(select_clause + where_clause, (tuple(existing_item_ids),))
        else:
            ctx.cr.execute(select_clause)

        products = ctx.cr.dictfetchall()

        # Embed lookup data and filter to only new products
        new_products = []
        for product in products:
            item_number = product.get("item_number")
            if item_number and item_number in existing_default_codes:
                continue  # Skip - will be handled by extract_products_to_update

            prodcat_id = product.get("item_prodcat_id")
            product["_category_id"] = category_dict.get(prodcat_id, default_category_id)
            inv_uom_id = product.get("item_inv_uom_id")
            product["_uom_id"] = uom_dict.get(inv_uom_id, default_uom_id)
            new_products.append(product)

        _logger.info(f"Extracted {len(new_products)} new products from xTuple")
        return new_products

    @ETL.extract("item_update")
    def extract_products_to_update(self, ctx: ETLContext) -> List[Dict]:
        """Extract products that exist in Odoo by default_code but need xTuple fields."""
        ctx.env.cr.execute(
            "SELECT xtuple_item_id FROM product_product WHERE xtuple_item_id IS NOT NULL"
        )
        existing_item_ids = set(row[0] for row in ctx.env.cr.fetchall())

        ctx.env.cr.execute(
            "SELECT default_code FROM product_product WHERE default_code IS NOT NULL AND default_code != ''"
        )
        existing_default_codes = {row[0] for row in ctx.env.cr.fetchall()}

        _, _, uom_dict, default_uom_id = self._get_lookup_dicts(ctx)

        select_clause = f"""
        SELECT
            {PRODUCT_SELECT}
        FROM item
        LEFT JOIN uom invuom ON (item_inv_uom_id = invuom.uom_id)
        LEFT JOIN uom priceuom ON (item_price_uom_id = priceuom.uom_id)
        LEFT JOIN prodcat ON (item_prodcat_id = prodcat_id)
        LEFT JOIN classcode ON (item_classcode_id = classcode_id)
        {ITEMSITE_TRACKING_JOIN}
        """

        if existing_item_ids:
            where_clause = "WHERE item_id NOT IN %s"
            ctx.cr.execute(select_clause + where_clause, (tuple(existing_item_ids),))
        else:
            ctx.cr.execute(select_clause)

        products = ctx.cr.dictfetchall()

        # Filter to only products that exist by default_code (need update)
        # Embed UoM so the transform can include it in update_vals
        products_to_update = []
        for p in products:
            if not p.get("item_number") or p.get("item_number") not in existing_default_codes:
                continue
            inv_uom_id = p.get("item_inv_uom_id")
            p["_uom_id"] = uom_dict.get(inv_uom_id, default_uom_id)
            products_to_update.append(p)

        _logger.info(
            f"Extracted {len(products_to_update)} products to update with xTuple fields"
        )
        return products_to_update

    def _transform_product(self, product: Dict) -> Dict:
        """Transform a single xTuple product into Odoo product values."""
        item_type = product.get("item_type", "")
        product_type = "consu"
        is_storable = False
        tracking = {
            "S": "serial",
            "R": "lot",
            "L": "lot",
        }.get(product.get("item_controlmethod", "N"), "none")

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

        # Get product name (from item_number per client requirement)
        # Client wants: item_number -> name, item_descrip1 -> default_code (Reference)
        name = product.get("item_number", "")

        # Reference field gets the description
        default_code = product.get("item_descrip1", "")

        description = product.get("item_descrip2", "")

        # Get category and UoM (lookup done in extract phase)
        category_id = product.get("_category_id")
        uom_id = product.get("_uom_id")

        # Get price and cost
        list_price = product.get("item_listprice", 0.0)
        standard_price = product.get("item_listcost", 0.0)
        if not standard_price and product.get("item_maxcost"):
            standard_price = product.get("item_maxcost", 0.0)

        return {
            "name": name,
            "description": description,
            "default_code": default_code,
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

    @ETL.transform()
    def transform_products(self, ctx: ETLContext, extracted: Dict) -> Dict:
        """Transform xTuple products into Odoo product values."""
        new_products = extracted.get("extract_new_products", [])
        products_to_update = extracted.get("extract_products_to_update", [])

        # Transform new products for creation
        create_vals = [self._transform_product(p) for p in new_products]

        # Transform products that need xTuple fields updated
        # Note: Fields were reversed in original import - fixing both name and default_code
        # xTuple item_number (Product name) -> Odoo name
        # xTuple item_descrip1 (Description) -> Odoo default_code (Reference)
        # IMPORTANT: Products in Odoo currently have default_code = item_number (wrong mapping)
        # We need to look up by item_number (current default_code) to find them
        update_vals = []
        for product in products_to_update:
            item_type = product.get("item_type", "")
            update_vals.append(
                {
                    "name": product.get("item_number"),
                    "default_code": product.get("item_descrip1"),
                    "_lookup_code": product.get("item_number"),  # Current default_code in Odoo
                    "xtuple_item_id": product.get("item_id"),
                    "xtuple_item_number": product.get("item_number"),
                    "xtuple_item_type": item_type,
                    "xtuple_classcode": product.get("classcode_code"),
                    "uom_id": product.get("_uom_id"),
                }
            )

        _logger.info(
            f"Transformed {len(create_vals)} new products, "
            f"{len(update_vals)} products to update"
        )
        return {"create": create_vals, "update": update_vals}

    @ETL.load()
    def load_products(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load products into Odoo."""
        data = transformed.get("transform_products", {})
        create_vals = data.get("create", [])
        update_vals = data.get("update", [])

        # Create new products
        if create_vals:
            products = ctx.env["product.product"].create(create_vals)
            _logger.info(f"Created {len(products)} products")

            # Mark templates inactive for inactive variants
            inactive_products = ctx.env["product.product"].search(
                [("active", "=", False), ("xtuple_item_id", "!=", False)]
            )
            inactive_products.product_tmpl_id.write({"active": False})

        # Update existing products with xTuple fields
        if update_vals:
            updated = 0
            tmpl_ids_to_invalidate = []
            for vals in update_vals:
                # Pop the fields we need for lookup before writing
                lookup_code = vals.pop("_lookup_code")  # Current default_code in Odoo (item_number)
                xtuple_item_id = vals.get("xtuple_item_id")
                uom_id = vals.pop("uom_id", None)
                
                # Look up product by xtuple_item_id (most reliable since that's already correct)
                # Fallback to _lookup_code (current default_code = item_number in Odoo)
                product = ctx.env["product.product"].search(
                    [("xtuple_item_id", "=", xtuple_item_id)], limit=1
                )
                if not product:
                    product = ctx.env["product.product"].search(
                        [("default_code", "=", lookup_code)], limit=1
                    )
                if not product:
                    continue
                
                # Pop uom_id before the ORM write and apply it separately:
                # product.template.write() calls _update_uom() then fires the
                # account @api.constrains which blocks cross-UoM changes on
                # products with posted invoices. Instead we call _update_uom()
                # directly (to cascade the new UoM onto moves, BOMs, etc.) and
                # then write uom_id/uom_po_id via SQL.
                if vals:
                    # name is on product.template, other fields on product.product
                    name_val = vals.pop("name", None)
                    if name_val:
                        product.product_tmpl_id.write({"name": name_val})
                    if vals:
                        product.write(vals)
                if uom_id and uom_id != product.uom_id.id:
                    product.product_variant_ids.with_context(
                        skip_uom_conversion=True
                    )._update_uom(uom_id)
                    ctx.env.cr.execute(
                        """
                        UPDATE product_template
                        SET uom_id = %(uom)s, uom_po_id = %(uom)s
                        WHERE id = %(tmpl_id)s
                        """,
                        {"uom": uom_id, "tmpl_id": product.product_tmpl_id.id},
                    )
                    tmpl_ids_to_invalidate.append(product.product_tmpl_id.id)
                updated += 1
            if tmpl_ids_to_invalidate:
                ctx.env["product.template"].invalidate_model(["uom_id", "uom_po_id"])
            _logger.info(f"Updated {updated} existing products with xTuple fields")

        if not create_vals and not update_vals:
            _logger.info("No products to create or update")


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
# Product Cost Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="product.template",
    importer_name="xtuple.product.cost.importer",
    sap_source="itemcost",
    depends_on=["xtuple.product.importer"],
)
class XtupleProductCostImporter(models.AbstractModel):
    """ETL Pipeline for importing standard costs from xTuple itemcost table.

    Reads the rolled-up standard cost (SUM of all cost elements) per item and
    writes it to standard_price on the matching product.template. Must run after
    product import and before stock quant import so that inventory adjustment SVLs
    are created with the correct per-unit value.
    """

    _name = "xtuple.product.cost.importer"
    _description = "xTuple Product Cost Importer"

    @ETL.extract("itemcost")
    def extract_costs(self, ctx: ETLContext) -> Dict:
        """Extract rolled-up standard costs per item from xTuple itemcost table."""
        ctx.cr.execute(
            """
            SELECT itemcost_item_id, SUM(itemcost_stdcost) AS total_stdcost
            FROM itemcost
            GROUP BY itemcost_item_id
            HAVING SUM(itemcost_stdcost) > 0
            """
        )
        cost_rows = ctx.cr.dictfetchall()
        cost_by_item_id = {
            row["itemcost_item_id"]: row["total_stdcost"] for row in cost_rows
        }
        _logger.info(
            f"Extracted standard costs for {len(cost_by_item_id)} items from xTuple"
        )

        ctx.env.cr.execute(
            """
            SELECT pp.xtuple_item_id, pt.id AS product_tmpl_id
            FROM product_product pp
            JOIN product_template pt ON pp.product_tmpl_id = pt.id
            WHERE pp.xtuple_item_id IS NOT NULL
            """
        )
        tmpl_by_item_id = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        return {
            "cost_by_item_id": cost_by_item_id,
            "tmpl_by_item_id": tmpl_by_item_id,
        }

    @ETL.transform()
    def transform_costs(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Map xTuple item costs to Odoo product template IDs."""
        data = extracted.get("extract_costs", {})
        cost_by_item_id = data.get("cost_by_item_id", {})
        tmpl_by_item_id = data.get("tmpl_by_item_id", {})

        cost_vals = []
        skipped = 0
        for item_id, cost in cost_by_item_id.items():
            tmpl_id = tmpl_by_item_id.get(item_id)
            if not tmpl_id:
                skipped += 1
                continue
            cost_vals.append({"product_tmpl_id": tmpl_id, "standard_price": cost})

        if skipped:
            _logger.warning(
                f"Skipped {skipped} itemcost records - no matching product in Odoo"
            )
        _logger.info(f"Transformed standard costs for {len(cost_vals)} products")
        return cost_vals

    @ETL.load()
    def load_costs(self, ctx: ETLContext, transformed: Dict) -> None:
        """Write standard_price to product.template records."""
        cost_vals = transformed.get("transform_costs", [])
        if not cost_vals:
            _logger.info("No product costs to update")
            return

        updated = 0
        for vals in cost_vals:
            ctx.env["product.template"].browse(vals["product_tmpl_id"]).write(
                {"standard_price": vals["standard_price"]}
            )
            updated += 1

        _logger.info(f"Updated standard_price for {updated} product templates")


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
        # Build lookup of existing products by default_code that don't have xtuple_item_id
        # (needed for transform, done here for multiprocessing compatibility)
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

        select_clause = """
        SELECT
            item_id,
            item_number
        FROM item
        WHERE item_number IS NOT NULL AND item_number != ''
        """
        ctx.cr.execute(select_clause)
        products = ctx.cr.dictfetchall()

        # Embed lookup results in each product record for multiprocessing
        for product in products:
            item_number = product.get("item_number")
            item_id = product.get("item_id")
            product["_odoo_product_id"] = product_by_code.get(item_number)
            product["_already_linked"] = item_id in existing_item_ids

        _logger.info(f"Extracted {len(products)} products with item_number for linking")
        return products

    @ETL.transform()
    def transform_products_for_linking(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Find existing products by default_code and prepare link updates."""
        products = extracted.get("extract_products_for_linking", [])

        link_updates = []
        for product in products:
            # Skip if this item ID is already assigned to another product (lookup done in extract)
            if product.get("_already_linked"):
                continue
            odoo_product_id = product.get("_odoo_product_id")
            if odoo_product_id:
                link_updates.append(
                    {
                        "product_id": odoo_product_id,
                        "xtuple_item_id": product.get("item_id"),
                        "xtuple_item_number": product.get("item_number"),
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


# =============================================================================
# Product Field Fix Pipeline (corrects name/reference swap)
# =============================================================================


@ETL.pipeline(
    target_model="product.product",
    importer_name="xtuple.product.fieldfix.importer",
    sap_source="item",
    depends_on=["xtuple.product.importer"],
)
class XtupleProductFieldFixImporter(models.AbstractModel):
    """ETL Pipeline to fix products where name and default_code were swapped.

    Original import incorrectly mapped:
        - item_number -> default_code (Reference)
        - item_descrip1 -> name (Product Name)

    Correct mapping (per client requirement):
        - item_number -> name (Product Name)
        - item_descrip1 -> default_code (Reference)

    This pipeline reimports products that already have xtuple_item_id set
    and fixes the name and default_code fields.
    """

    _name = "xtuple.product.fieldfix.importer"
    _description = "xTuple Product Field Fix Importer"

    @ETL.extract("item")
    def extract_products_to_fix(self, ctx: ETLContext) -> List[Dict]:
        """Extract products from Odoo that have xtuple_item_id and need fixing."""
        # Get all products with xtuple_item_id
        ctx.env.cr.execute(
            """
            SELECT pp.id, pp.default_code, pt.name, pp.xtuple_item_id, pp.xtuple_item_number
            FROM product_product pp
            JOIN product_template pt ON pp.product_tmpl_id = pt.id
            WHERE pp.xtuple_item_id IS NOT NULL
            """
        )
        odoo_products = ctx.env.cr.dictfetchall()
        _logger.info(f"Found {len(odoo_products)} products with xtuple_item_id in Odoo")

        if not odoo_products:
            return []

        # Get the xtuple item data for these products
        item_ids = [p["xtuple_item_id"] for p in odoo_products]

        # We only need item_id, item_number, and item_descrip1 for the fix
        select_clause = """
        SELECT
            item_id,
            item_number,
            item_descrip1
        FROM item
        WHERE item_id IN %s
        """
        ctx.cr.execute(select_clause, (tuple(item_ids),))
        xtuple_items = ctx.cr.dictfetchall()
        _logger.info(f"Found {len(xtuple_items)} xTuple items to check")

        # Create lookup by item_id
        xtuple_by_id = {item["item_id"]: item for item in xtuple_items}

        # Embed Odoo data in xTuple items
        odoo_by_item_id = {p["xtuple_item_id"]: p for p in odoo_products}
        for item in xtuple_items:
            item["_odoo_product_id"] = odoo_by_item_id.get(item["item_id"], {}).get("id")
            item["_odoo_name"] = odoo_by_item_id.get(item["item_id"], {}).get("name")
            item["_odoo_default_code"] = odoo_by_item_id.get(item["item_id"], {}).get("default_code")

        return xtuple_items

    @ETL.transform()
    def transform_products_to_fix(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple items into field fix updates."""
        items = extracted.get("extract_products_to_fix", [])

        fix_vals = []
        for item in items:
            odoo_product_id = item.get("_odoo_product_id")
            if not odoo_product_id:
                continue

            # Correct mapping:
            # item_number -> name (Product Name)
            # item_descrip1 -> default_code (Reference)
            correct_name = item.get("item_number", "")
            correct_default_code = item.get("item_descrip1", "")

            fix_vals.append(
                {
                    "product_id": odoo_product_id,
                    "name": correct_name,
                    "default_code": correct_default_code,
                }
            )

        _logger.info(f"Prepared {len(fix_vals)} products for field fix")
        return fix_vals

    @ETL.load()
    def load_field_fixes(self, ctx: ETLContext, transformed: Dict) -> None:
        """Apply field fixes to products."""
        fix_vals = transformed.get("transform_products_to_fix", [])

        if not fix_vals:
            _logger.info("No products to fix")
            return

        fixed = 0
        for vals in fix_vals:
            product_id = vals.pop("product_id")

            product = ctx.env["product.product"].browse(product_id)
            if not product.exists():
                continue

            # Update name on template and default_code on variant
            product.product_tmpl_id.write({"name": vals["name"]})
            product.write({"default_code": vals["default_code"]})

            fixed += 1

        _logger.info(f"Fixed {fixed} products with correct name/reference mapping")
