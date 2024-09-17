from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class Product(models.Model):
    _inherit = "product.product"

    sap_item_code = fields.Char(index="trigram")
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
        categories = self._import_oitb(cr)
        products = self._import_oitm(cr, categories)
        # TODO: implement BOMs with treetype, treeqty and the ITT1 table
        # TODO: grab the inventory "on hand" fields, min max, etc.
        # TODO: grab the revenue and expense accounts

    def _import_oitb(self, cr):
        """Import product categories from SAP"""
        cr.execute(
            "SELECT * FROM oitb WHERE itmsgrpnam <> '' and itmsgrpnam is not null"
        )
        sap_product_groups = cr.dictfetchall()
        _logger.info(f"Importing {len(sap_product_groups)} product categories.")
        category_vals = []
        for sap_group in sap_product_groups:
            category_vals.append(
                {
                    "name": sap_group["itmsgrpnam"],
                    "sap_itms_grp_cod": sap_group["itmsgrpcod"],
                }
            )
        return self.env["product.category"].create(category_vals)
        # TODO: import related account info logically if data exists

    def _import_oitm(self, cr, categories):
        """Import sellable products"""
        cr.execute("SELECT * FROM oitm WHERE frgnname <> '' and frgnname is not null")
        sap_products = cr.dictfetchall()
        _logger.info(f"Importing {len(sap_products)} products.")
        product_vals = []
        categories_map = {
            category.sap_itms_grp_cod: category for category in categories
        }
        for sap_product in sap_products:
            product_vals.append(
                {
                    "sap_item_code": sap_product["itemcode"],
                    "code": fix_quotes(sap_product["itemname"]),
                    "name": fix_quotes(sap_product["frgnname"]),
                    "categ_id": categories_map[
                        sap_product["itmsgrpcod"]
                    ].id,  # No nulls
                    "sale_ok": sap_product["sellitem"] == "Y",
                    "purchase_ok": sap_product["prchseitem"] == "Y",
                    "active": sap_product["validfor"] == "Y",
                }
            )
        return self.env["product.product"].create(product_vals)

    def delete_all(self):
        self.env["product.product"].search(
            [
                ("sap_item_code", "!=", False),
                ("active", "in", [True, False]),
            ]
        ).unlink()
        self.env["product.category"].search(
            [("sap_itms_grp_cod", "!=", False)]
        ).unlink()


def fix_quotes(string):
    return string and string.strip('"').replace('""', '"')
