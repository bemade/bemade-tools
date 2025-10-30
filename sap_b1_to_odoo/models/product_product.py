from collections import defaultdict
from odoo import models, fields, api
from odoo.addons.mrp.models.stock_quant import StockQuant
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from odoo.modules.registry import Registry
from odoo.sql_db import SQL
from odoo.addons.sap_b1_to_odoo.tools import fix_quotes

workers = 8

_logger = logging.getLogger(__name__)


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


class SapProductImporter(models.AbstractModel):
    _name = "sap.product.importer"
    _description = "SAP Product Importer"

    @api.model
    def import_products(self, cr):
        _logger.info("Importing products and categories...")
        category_ids = self._import_oitb(cr)
        self.env["product.category"].flush_model()
        self.env.cr.commit()
        self._import_oitm(cr, category_ids)

    @api.model
    def import_inventory(self, cr):
        self._import_inventory_valuation(cr)
        self._import_stock_quants(cr)

    @staticmethod
    def _sub_import_oitm(dbname, uid, context, chunk, categories_map):
        _logger.info(f"Subprocess: Importing {len(chunk)} products.")
        try:
            with Registry(dbname).cursor() as cr:
                env = api.Environment(cr, uid, context)
                product_vals = []
                for sap_product in chunk:
                    # # country_of_origin = sap_product["u_fcsdk_coo"]
                    # if country_of_origin:
                    #     country_of_origin = env["res.country"].search(
                    #         [("code", "=", country_of_origin)]
                    #     )
                    categ = (
                        categories_map[sap_product["itmsgrpcod"]]
                        if sap_product["itmsgrpcod"]
                        and sap_product["itmsgrpcod"] in categories_map
                        else False
                    )
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
                        "company_id": env.company.id,
                        # "hs_code": sap_product["u_fcsdk_hst"] or None,
                        # "country_of_origin": country_of_origin
                        # and country_of_origin.id
                        # or None,
                    }
                    if categ:
                        vals["categ_id"] = categ
                    product_vals.append(vals)
                env["product.product"].create(product_vals)
                cr.commit()
        except Exception:
            _logger.error("An exception occurred in a subprocess.", exc_info=True)
            raise

    def _import_oitm(self, cr, categories):
        """Import sellable products"""
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
        sql = "SELECT * FROM oitm"
        if existing_products:
            sql += " WHERE itemcode not in %s"
            sql = SQL(sql, existing_products)
        else:
            sql = SQL(sql)
        cr.execute(sql)
        sap_products = cr.dictfetchall()
        chunk_size = 500
        chunks = [
            sap_products[i : i + chunk_size]
            for i in range(0, len(sap_products), chunk_size)
        ]
        start_method = multiprocessing.get_start_method()
        multiprocessing.set_start_method("fork", force=True)
        categories_map = {
            category.sap_itms_grp_cod: category.id for category in categories
        }
        try:
            _logger.info(
                f"Importing {len(sap_products)} products in {len(chunks)} chunks."
            )
            with ProcessPoolExecutor(max_workers=workers) as executor:
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

    def _import_oitb(self, cr):
        """Import product categories from SAP"""
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

    def import_orderpoints(self, cr):
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

    def _get_kit_component_quantities(self, product, quantity):
        """Get the component quantities for a kit product.

        Args:
            product: product.product record that is a kit
            quantity: quantity of the kit needed

        Returns:
            dict: product_id -> quantity mapping for components
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

    def _import_stock_quants(self, cr):
        self.env.flush_all()
        cr.execute(
            """
            SELECT oitw.itemcode,oitw.onhand
            FROM oitw 
            INNER JOIN oitm ON oitw.itemcode = oitm.itemcode and oitw.onhand > 0
            """
        )
        stocks = cr.dictfetchall()
        if not stocks:
            return

        products = self.env["product.product"].search(
            [("sap_item_code", "in", [s["itemcode"] for s in stocks])]
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

        for stock in stocks:
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

        # Pre-fetch all products and locations we'll need
        all_product_ids = set()
        all_location_ids = set()

        # Gather IDs from regular vals
        for val in regular_vals:
            all_product_ids.add(val["product_id"])
            all_location_ids.add(val["location_id"])

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

        def process_quants(vals_list, existing_quants, products_dict, locations_dict):
            """Process a list of quant values, either creating new quants or updating existing ones.

            Args:
                vals_list: List of dicts with product_id, location_id, and quantity
                existing_quants: Dict of existing quants keyed by (product, location)
                products_dict: Dict of product records keyed by id
                locations_dict: Dict of location records keyed by id

            Returns:
                Tuple of (quants_to_create, quants_to_update)
            """
            to_create = []
            to_update = []

            for val in vals_list:
                product = products_dict.get(val["product_id"])
                location = locations_dict.get(val["location_id"])
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

        def batch_create(to_create, batch_size=1000):
            """Create quants in batches."""
            for i in range(0, len(to_create), batch_size):
                batch = to_create[i : i + batch_size]
                self.env["stock.quant"].create(batch)
                self.env.cr.commit()

        # Process regular products
        regular_create, regular_update = process_quants(
            regular_vals, existing_quants, products_by_id, locations_by_id
        )
        batch_create(regular_create)
        if regular_update:
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

        # Add component product IDs to our lookup
        for val in component_vals:
            all_product_ids.add(val["product_id"])

        # Update product lookup with any new components
        new_products = self.env["product.product"].browse(
            list(all_product_ids - set(products_by_id.keys()))
        )
        products_by_id.update({p.id: p for p in new_products})

        # Process component quants
        component_create, component_update = process_quants(
            component_vals, existing_quants, products_by_id, locations_by_id
        )
        batch_create(component_create)
        if component_update:
            self.env.cr.commit()  # Commit the quantity updates

    def _import_inventory_valuation(self, cr):
        self.env.flush_all()
        cr.execute(
            """
            SELECT oitw.itemcode,oitw.avgprice,oitm.onhand
            FROM oitw 
            INNER JOIN oitm ON oitw.itemcode = oitm.itemcode and oitm.onhand > 0
            """
        )
        valuations = cr.dictfetchall()
        products = self.env["product.template"].search(
            [("sap_item_code", "in", [val["itemcode"] for val in valuations])]
        )
        products_dict = {p.sap_item_code: p for p in products}
        vals_list = []
        for val in valuations:
            product = products_dict[val["itemcode"]]
            vals = {
                "company_id": self.env.company.id,
                "product_id": product.id,
                "unit_cost": val["avgprice"],
                "quantity": val["onhand"],
            }
            vals_list.append(vals)
        self.env["stock.valuation.layer"].create(vals_list)
        self.env.flush_all()

    def _delete_all(self):
        self.env.cr.execute(
            "DELETE from product_product WHERE sap_item_code is not null"
        )
        self.env.cr.execute(
            "DELETE from product_category WHERE sap_itms_grp_cod is not null"
        )
