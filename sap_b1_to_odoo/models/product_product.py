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


def _dummy_check_kits(self):
    pass


class ProductTemplate(models.Model):
    _inherit = "product.template"

    sap_item_code = fields.Char(index="btree")
    sap_atcentry = fields.Integer()
    _sql_constraints = [
        (
            "sap_item_code_unique",
            "unique (sap_item_code)",
            "A product with that SAP item code already exists.",
        )
    ]


class ProductCategory(models.Model):
    _inherit = "product.category"

    sap_itms_grp_cod = fields.Integer(index="btree")
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
                    country_of_origin = sap_product["u_fcsdk_coo"]
                    if country_of_origin:
                        country_of_origin = env["res.country"].search(
                            [("code", "=", country_of_origin)]
                        )
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
                        "hs_code": sap_product["u_fcsdk_hst"] or None,
                        "country_of_origin": country_of_origin
                        and country_of_origin.id
                        or None,
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

    def _import_stock_quants(self, cr):
        products = self.env["product.product"].search(
            [("active", "in", [True, False]), ("sap_item_code", "!=", False)]
        )
        warehouse = self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1
        )
        location = warehouse.lot_stock_id
        cr.execute("SELECT itemcode, onhand FROM oitm WHERE validfor='Y'")
        sap_stock = cr.dictfetchall()
        codes = [stock["itemcode"] for stock in sap_stock]
        products_dict = {
            p.sap_item_code: p for p in products if p.sap_item_code in codes
        }
        vals = []
        for stock in sap_stock:
            vals.append(
                {
                    "product_id": products_dict[stock["itemcode"]].id,
                    "quantity": stock["onhand"],
                    "location_id": location.id,
                }
            )
        real_check_kits = StockQuant._check_kits
        StockQuant._check_kits = _dummy_check_kits
        try:
            self.env["stock.quant"].create(vals)
        finally:
            StockQuant._check_kits = real_check_kits

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
