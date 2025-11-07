import logging
import multiprocessing
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Tuple, Any

from odoo import api, fields, models
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext
from odoo.addons.sap_b1_to_odoo.tools import fix_quotes
from odoo.modules.registry import Registry
from odoo.sql_db import SQL

_logger = logging.getLogger(__name__)

MAX_WORKERS = 8


class ProductTemplate(models.Model):
    _inherit = "product.template"

    sap_item_code = fields.Char(index="btree", copy=False)
    sap_atcentry = fields.Integer(copy=False)
    _sql_constraints = [
        (
            "sap_item_code_unique",
            "unique (sap_item_code)",
            "A product with that SAP item code already exists.",
        )
    ]


class ProductCategory(models.Model):
    _inherit = "product.category"

    sap_itms_grp_cod = fields.Integer(index="btree", copy=False)
    _sql_constraints = [
        (
            "sap_itms_grp_cod_unique",
            "unique (sap_itms_grp_cod)",
            "A product category with that SAP code already exists.",
        )
    ]


##################################################################
# ETL Framework Pipelines
##################################################################


@ETL.pipeline(
    target_model="product.category",
    importer_name="product.category.importer",
    sap_source="oitb",
    depends_on=[],
)
class ProductCategoryImporter(models.AbstractModel):
    _name = "product.category.importer"
    _description = "SAP Product Category Importer (OITB)"

    @ETL.extract("oitb")
    def extract_categories(self, ctx: ETLContext) -> List[Dict]:
        """Extract product categories from SAP OITB table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of category dictionaries from SAP.
        """
        # Get existing categories to avoid duplicates
        ctx.env.cr.execute(
            "SELECT sap_itms_grp_cod FROM product_category WHERE sap_itms_grp_cod IS NOT NULL"
        )
        existing_codes = tuple(row[0] for row in ctx.env.cr.fetchall())

        # Query SAP - filter out empty names
        sql = "SELECT * FROM oitb WHERE itmsgrpnam <> '' AND itmsgrpnam IS NOT NULL"
        if existing_codes:
            sql += " AND itmsgrpcod NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_codes))
        else:
            ctx.cr.execute(sql)

        sap_categories = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(sap_categories)} categories from SAP OITB.")
        return sap_categories

    @ETL.transform()
    def transform_categories(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP categories into Odoo category values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of category value dictionaries ready for creation.
        """
        sap_categories = extracted["extract_categories"]
        
        category_vals = []
        for sap_cat in sap_categories:
            category_vals.append({
                "sap_itms_grp_cod": sap_cat["itmsgrpcod"],
                "name": fix_quotes(sap_cat["itmsgrpnam"]),
                "property_cost_method": "fifo",
            })
        
        _logger.info(f"Transformed {len(category_vals)} category records.")
        return category_vals

    @ETL.load()
    def load_categories(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load categories into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        category_vals = transformed["transform_categories"]

        if category_vals:
            categories = ctx.env["product.category"].create(category_vals)
            _logger.info(f"Created {len(categories)} product categories.")
        else:
            _logger.info("No new categories to create.")


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

        # Query SAP
        sql = "SELECT * FROM oitm"
        if existing_products:
            sql += " WHERE itemcode NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_products))
        else:
            ctx.cr.execute(sql)

        sap_products = ctx.cr.dictfetchall()

        # Pre-compute category mapping
        categories = ctx.env["product.category"].search([("sap_itms_grp_cod", "!=", False)])
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

            # Build product values
            vals = {
                "sap_item_code": sap_product["itemcode"],
                "sap_atcentry": sap_product["atcentry"],
                "default_code": fix_quotes(sap_product["itemname"]),
                "name": fix_quotes(sap_product["frgnname"] or "N/A"),
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


class SapProductImporter(models.AbstractModel):
    _name = "sap.product.importer"
    _description = "SAP Product Importer"

    ##################################################################
    # Public Interface and Main Entry Point Methods
    ##################################################################

    @api.model
    def import_products(self, cr) -> None:
        """Import products and product categories from SAP.

        Args:
            cr: Database cursor for the SAP database.
        """
        _logger.info("Importing products and categories...")
        category_ids = self._import_oitb(cr)
        self.env["product.category"].flush_model()
        self.env.cr.commit()
        self._import_oitm(cr, category_ids)

    @api.model
    def import_inventory(self, cr) -> None:
        """Import inventory valuations and stock quants from SAP.

        Args:
            cr: Database cursor for the SAP database.
        """
        self._import_inventory_valuation(cr)
        self._import_stock_quants(cr)

    @api.model
    def import_orderpoints(self, cr) -> None:
        """Import stock reordering rules (min/max levels) from SAP.

        Args:
            cr: Database cursor for the SAP database.
        """
        existing_orderpoints = tuple(
            self.env["stock.warehouse.orderpoint"]
            .search(
                [
                    ("product_id.sap_item_code", "!=", False),
                    ("active", "in", [True, False]),
                ],
            )
            .mapped("product_id.sap_item_code")
        )
        sql = (
            "SELECT itemcode, minlevel, maxlevel FROM oitm WHERE minlevel > 0 "
            "and validfor='Y'"
        )
        if existing_orderpoints:
            sql += " and itemcode not in %s"
            sql = SQL(sql, existing_orderpoints)
        else:
            sql = SQL(sql)
        cr.execute(sql)
        sap_min_levels = cr.dictfetchall()
        _logger.info(f"Importing {len(sap_min_levels)} orderpoints.")
        codes = [lvl["itemcode"] for lvl in sap_min_levels]
        products = self.env["product.product"].search(
            [("sap_item_code", "!=", False), ("active", "in", [False, True])]
        )
        _logger.info(f"Importing min/max levels for {len(products)} products.")
        products_dict = {
            p.sap_item_code: p for p in products if p.sap_item_code in codes
        }
        for lvl in sap_min_levels:
            product = products_dict[lvl["itemcode"]]
            self.env["stock.warehouse.orderpoint"].create(
                {
                    "product_id": product.id,
                    "product_min_qty": lvl["minlevel"],
                    "product_max_qty": max(lvl["maxlevel"], lvl["minlevel"]),
                }
            )

    @api.model
    def _import_oitm(self, cr, categories) -> None:
        """Import sellable products from SAP OITM table using multiprocessing.

        Args:
            cr: Database cursor for the SAP database.
            categories: Recordset of product.category records with SAP codes.
        """
        # Extract
        sap_products, chunks = self._extract_oitm_products(cr)

        # Transform: Build category mapping
        categories_map = {
            category.sap_itms_grp_cod: category.id for category in categories
        }

        # Load: Import products using multiprocessing
        start_method = multiprocessing.get_start_method()
        multiprocessing.set_start_method("fork", force=True)
        try:
            _logger.info(
                f"Importing {len(sap_products)} products in {len(chunks)} chunks."
            )
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [
                    executor.submit(
                        self._sub_import_oitm,
                        self.env.cr.dbname,
                        self.env.uid,
                        dict(self.env.context),
                        chunk,
                        categories_map,
                    )
                    for chunk in chunks
                ]
                for future in futures:
                    future.result()
        except Exception as e:
            raise e
        finally:
            multiprocessing.set_start_method(start_method, force=True)

    @staticmethod
    def _sub_import_oitm(
        dbname: str,
        uid: int,
        context: Dict[str, Any],
        chunk: List[Dict[str, Any]],
        categories_map: Dict[int, int],
    ) -> None:
        """Subprocess worker to import a chunk of products from SAP OITM table.

        Args:
            dbname: Database name.
            uid: User ID.
            context: Odoo environment context.
            chunk: List of SAP product dictionaries.
            categories_map: Mapping of SAP category codes to Odoo category IDs.
        """
        _logger.info(f"Subprocess: Importing {len(chunk)} products.")
        try:
            with Registry(dbname).cursor() as cr:
                env = api.Environment(cr, uid, context)

                # Transform: Convert SAP products to Odoo product values
                product_vals = SapProductImporter._transform_oitm_chunk(
                    chunk, categories_map, env.company.id
                )

                # Load: Create products in Odoo
                env["product.product"].create(product_vals)
                cr.commit()
        except Exception:
            _logger.error("An exception occurred in a subprocess.", exc_info=True)
            raise

    @api.model
    def _import_oitb(self, cr):
        """Import product categories from SAP OITB table.

        Args:
            cr: Database cursor for the SAP database.

        Returns:
            Recordset of created product.category records.
        """
        existing_groups = tuple(
            self.env["product.category"]
            .search([("sap_itms_grp_cod", "!=", False)])
            .mapped("sap_itms_grp_cod")
        )
        sql = "SELECT * FROM oitb WHERE itmsgrpnam <> '' and itmsgrpnam is not null"
        if existing_groups:
            sql += " and itmsgrpcod not in %s"
            sql = SQL(sql, existing_groups)
        else:
            sql = SQL(sql)
        cr.execute(sql)
        sap_product_groups = cr.dictfetchall()
        _logger.info(f"Importing {len(sap_product_groups)} product categories.")
        category_vals = []
        for sap_group in sap_product_groups:
            category_vals.append(
                {
                    "name": sap_group["itmsgrpnam"],
                    "sap_itms_grp_cod": sap_group["itmsgrpcod"],
                    "property_cost_method": "fifo",
                }
            )
        categs = self.env["product.category"].create(category_vals)
        self.env.cr.commit()
        return categs

    ##################################################################
    # Extraction Methods
    ##################################################################

    @api.model
    def _extract_oitm_products(
        self, cr, chunk_size: int = 500
    ) -> Tuple[List[Dict[str, Any]], List[List[Dict[str, Any]]]]:
        """Extract products from SAP OITM table, excluding already imported products.

        Args:
            cr: Database cursor for the SAP database.
            chunk_size: Size of chunks for multiprocessing.

        Returns:
            Tuple of (all_products, chunked_products) where:
                - all_products: Full list of SAP product dictionaries
                - chunked_products: List of product chunks for parallel processing
        """
        # Get existing products to exclude
        existing_products = tuple(
            [
                p["sap_item_code"]
                for p in self.env["product.product"].search_read(
                    [
                        ("sap_item_code", "!=", False),
                        ("active", "in", [True, False]),
                    ],
                    ["sap_item_code"],
                )
            ]
        )
        _logger.info(f"Found {len(existing_products)} existing products.")

        # Build SQL query to extract new products
        sql = "SELECT * FROM oitm"
        if existing_products:
            sql += " WHERE itemcode not in %s"
            sql = SQL(sql, existing_products)
        else:
            sql = SQL(sql)

        # Execute query
        cr.execute(sql)
        sap_products = cr.dictfetchall()

        # Split into chunks for parallel processing
        chunks = [
            sap_products[i : i + chunk_size]
            for i in range(0, len(sap_products), chunk_size)
        ]

        return sap_products, chunks

    ##################################################################
    # Transformation Methods
    ##################################################################

    @staticmethod
    def _transform_oitm_chunk(
        chunk: List[Dict[str, Any]], categories_map: Dict[int, int], company_id: int
    ) -> List[Dict[str, Any]]:
        """Transform a chunk of SAP OITM products into Odoo product values.

        Args:
            chunk: List of SAP product dictionaries.
            categories_map: Mapping of SAP category codes to Odoo category IDs.
            company_id: Company ID for the products.

        Returns:
            List of product value dicts ready for creation.
        """
        product_vals = []
        for sap_product in chunk:
            # Determine category
            categ = (
                categories_map[sap_product["itmsgrpcod"]]
                if sap_product["itmsgrpcod"]
                and sap_product["itmsgrpcod"] in categories_map
                else False
            )

            # Build product values
            vals = {
                "sap_item_code": sap_product["itemcode"],
                "sap_atcentry": sap_product["atcentry"],
                "default_code": fix_quotes(sap_product["itemname"]),
                "name": fix_quotes(sap_product["frgnname"] or "N/A"),
                "sale_ok": sap_product["sellitem"] == "Y",
                "purchase_ok": sap_product["prchseitem"] == "Y",
                "active": sap_product["validfor"] == "Y",
                "type": "consu",
                "is_storable": True,
                "company_id": company_id,
            }

            if categ:
                vals["categ_id"] = categ

            product_vals.append(vals)

        return product_vals

    @api.model
    def _get_kit_component_quantities(self, product, quantity: float) -> Dict:
        """Get the component quantities for a kit product.

        Args:
            product: product.product record that is a kit.
            quantity: Quantity of the kit needed.

        Returns:
            Dictionary mapping product records to required quantities.
        """
        # Get the phantom BOM for this product
        bom = self.env["mrp.bom"].sudo()._bom_find(product, bom_type="phantom")[product]
        if not bom:
            _logger.warning(
                "Product %s is marked as a kit but has no phantom BOM",
                product.display_name,
            )
            return {}

        components = defaultdict(float)

        # For each BOM line, calculate the needed component quantity
        for line in bom.bom_line_ids:
            # Skip this component if it shouldn't be included based on product variant
            if line._skip_bom_line(product):
                continue

            # Convert component quantity to the stock UOM if needed
            component = line.product_id
            line_qty = line.product_uom_id._compute_quantity(
                line.product_qty, component.uom_id
            )
            components[component] += line_qty * quantity

            # If the component is itself a kit, recursively get its components
            if component.is_kits:
                sub_components = self._get_kit_component_quantities(
                    component, components[component]
                )
                for sub_comp, sub_qty in sub_components.items():
                    components[sub_comp] += sub_qty
                del components[component]  # Remove the kit component itself

        return components

    ##################################################################
    # Loading Methods
    ##################################################################

    @api.model
    def _import_stock_quants(self, cr) -> None:
        """Import stock quantities from SAP, handling both regular products and kits.

        Args:
            cr: Database cursor for the SAP database.
        """
        self.env.flush_all()

        # Extract
        sap_stocks = self._extract_stock_quants(cr)
        if not sap_stocks:
            return

        # Transform
        regular_vals, kit_vals, location = self._transform_stock_quants(sap_stocks)

        # Load
        self._load_stock_quants(regular_vals, kit_vals, location)

    @api.model
    def _extract_stock_quants(self, cr) -> List[Dict[str, Any]]:
        """Extract stock quantities from SAP OITW table.

        Args:
            cr: Database cursor for the SAP database.

        Returns:
            List of stock dictionaries with itemcode and onhand quantity.
        """
        cr.execute(
            """
            SELECT oitw.itemcode, oitw.onhand
            FROM oitw 
            INNER JOIN oitm ON oitw.itemcode = oitm.itemcode and oitw.onhand > 0
            """
        )
        return cr.dictfetchall()

    @api.model
    def _transform_stock_quants(
        self, sap_stocks: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Tuple], Any]:
        """Transform SAP stock data into regular and kit product values.

        Args:
            sap_stocks: List of SAP stock dictionaries.

        Returns:
            Tuple of (regular_vals, kit_vals, location) where:
                - regular_vals: List of dicts for regular products
                - kit_vals: List of tuples (product, quantity, location) for kits
                - location: Stock location record
        """
        # Get products
        products = self.env["product.product"].search(
            [("sap_item_code", "in", [s["itemcode"] for s in sap_stocks])]
        )
        products_dict = {p.sap_item_code: p for p in products}

        # Get the stock location from the warehouse
        warehouse = self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1
        )
        location = warehouse.lot_stock_id

        # Separate kits from regular products
        kit_vals = []
        regular_vals = []

        for stock in sap_stocks:
            if stock["itemcode"] not in products_dict:
                continue

            product = products_dict[stock["itemcode"]]

            if product.is_kits:
                kit_vals.append((product, stock["onhand"], location))
            else:
                regular_vals.append(
                    {
                        "product_id": product.id,
                        "quantity": stock["onhand"],
                        "location_id": location.id,
                    }
                )

        return regular_vals, kit_vals, location

    @api.model
    def _load_stock_quants(self, regular_vals, kit_vals, location):
        """Load stock quantities into Odoo.

        Args:
            regular_vals: List of dicts for regular products
            kit_vals: List of tuples (product, quantity, location) for kits
            location: Stock location record
        """
        existing_quants = self._get_existing_quants(regular_vals, kit_vals)
        regular_create, regular_update = self._process_quant_vals(
            regular_vals, existing_quants
        )
        self._batch_create_quants(regular_create)
        self.env.cr.commit()  # Commit the quantity updates

        # Handle kit products
        components_to_update = defaultdict(float)
        for product, quantity, location in kit_vals:
            _logger.info(
                "Processing kit %s with quantity %s", product.display_name, quantity
            )
            components = self._get_kit_component_quantities(product, quantity)

            # Aggregate component quantities by location
            for component, comp_qty in components.items():
                components_to_update[(component, location)] += comp_qty

        # Convert component quantities to vals list
        component_vals = [
            {
                "product_id": component.id,
                "quantity": quantity,
                "location_id": location.id,
            }
            for (component, location), quantity in components_to_update.items()
        ]

        component_create, component_update = self._process_quant_vals(
            component_vals, existing_quants
        )
        self._batch_create_quants(component_create)
        if component_update:
            self.env.cr.commit()  # Commit the quantity updates

    @api.model
    def _get_existing_quants(self, regular_vals, kit_vals):
        """Get existing stock quants for the given products and locations.

        Args:
            regular_vals: List of dicts for regular products
            kit_vals: List of tuples (product, quantity, location) for kits

        Returns:
            Dict of existing quants keyed by (product, location)
        """
        all_product_ids = set()
        all_location_ids = set()

        # Gather IDs from regular vals
        for val in regular_vals:
            all_product_ids.add(val["product_id"])
            all_location_ids.add(val["location_id"])

        # Gather IDs from kit vals
        for product, _, location in kit_vals:
            all_product_ids.add(product.id)
            all_location_ids.add(location.id)

        # Pre-fetch all products and locations
        products = self.env["product.product"].browse(list(all_product_ids))
        locations = self.env["stock.location"].browse(list(all_location_ids))

        # Create lookup dictionaries
        products_by_id = {p.id: p for p in products}
        locations_by_id = {l.id: l for l in locations}

        # Get all existing quants
        existing_quants = {}
        for quant in self.env["stock.quant"].search(
            [
                ("location_id", "in", list(all_location_ids)),
                ("product_id", "in", list(all_product_ids)),
            ]
        ):
            existing_quants[(quant.product_id, quant.location_id)] = quant

        return existing_quants

    @api.model
    def _process_quant_vals(self, vals_list, existing_quants):
        """Process a list of quant values, either creating new quants or updating existing ones.

        Args:
            vals_list: List of dicts with product_id, location_id, and quantity
            existing_quants: Dict of existing quants keyed by (product, location)

        Returns:
            Tuple of (quants_to_create, quants_to_update)
        """
        to_create = []
        to_update = []

        for val in vals_list:
            product = self.env["product.product"].browse(val["product_id"])
            location = self.env["stock.location"].browse(val["location_id"])
            if not product or not location:
                continue

            key = (product, location)
            if key in existing_quants:
                quant = existing_quants[key]
                quant.quantity += val["quantity"]
                to_update.append(quant)
            else:
                to_create.append(val)

        return to_create, to_update

    @api.model
    def _batch_create_quants(self, to_create, batch_size=1000):
        """Create quants in batches."""
        for i in range(0, len(to_create), batch_size):
            batch = to_create[i : i + batch_size]
            self.env["stock.quant"].create(batch)
            self.env.cr.commit()

    ##################################################################
    # Utilities
    ##################################################################

    @api.model
    def _delete_all(self) -> None:
        """Delete all SAP-imported products and categories from Odoo.

        Warning: This is a destructive operation.
        """
        self.env.cr.execute(
            "DELETE from product_product WHERE sap_item_code is not null"
        )
        self.env.cr.execute(
            "DELETE from product_category WHERE sap_itms_grp_cod is not null"
        )

    ##################################################################
    # Inventory Valuation
    ##################################################################

    @api.model
    def _import_inventory_valuation(self, cr) -> None:
        """Import inventory valuation layers from SAP.

        Note: stock.valuation.layer model may not exist in Odoo 19.0.
        This method may need to be updated for the new inventory valuation system.

        Args:
            cr: Database cursor for the SAP database.
        """
        self.env.flush_all()

        # Extract
        sap_valuations = self._extract_inventory_valuations(cr)

        # Transform
        valuation_vals = self._transform_inventory_valuations(sap_valuations)

        # Load
        self._load_inventory_valuations(valuation_vals)

        self.env.flush_all()

    @api.model
    def _extract_inventory_valuations(self, cr) -> List[Dict[str, Any]]:
        """Extract inventory valuations from SAP OITW table.

        Args:
            cr: Database cursor for the SAP database.

        Returns:
            List of valuation dictionaries with itemcode, avgprice, and onhand.
        """
        cr.execute(
            """
            SELECT oitw.itemcode, oitw.avgprice, oitm.onhand
            FROM oitw 
            INNER JOIN oitm ON oitw.itemcode = oitm.itemcode and oitm.onhand > 0
            """
        )
        return cr.dictfetchall()

    @api.model
    def _transform_inventory_valuations(
        self, sap_valuations: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Transform SAP inventory valuations into Odoo valuation layer values.

        Args:
            sap_valuations: List of SAP valuation dictionaries.

        Returns:
            List of valuation layer value dicts ready for creation.
        """
        products = self.env["product.template"].search(
            [("sap_item_code", "in", [val["itemcode"] for val in sap_valuations])]
        )
        products_dict = {p.sap_item_code: p for p in products}

        vals_list = []
        for val in sap_valuations:
            product = products_dict.get(val["itemcode"])
            if not product:
                continue

            vals_list.append(
                {
                    "company_id": self.env.company.id,
                    "product_id": product.id,
                    "unit_cost": val["avgprice"],
                    "quantity": val["onhand"],
                }
            )

        return vals_list

    @api.model
    def _load_inventory_valuations(self, valuation_vals: List[Dict[str, Any]]) -> None:
        """Load inventory valuation layers into Odoo.

        Note: stock.valuation.layer model may not exist in Odoo 19.0.

        Args:
            valuation_vals: List of valuation layer value dicts.
        """
        if valuation_vals:
            self.env["stock.valuation.layer"].create(valuation_vals)
