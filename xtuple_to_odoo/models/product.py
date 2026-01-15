"""xTuple Product Model Extensions

This module adds xTuple-specific fields to product models for tracking
imported product categories, products, and supplier info.
"""

from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = "product.category"

    xtuple_prodcat_id = fields.Integer(string="xTuple Product Category ID", index=True)
    xtuple_prodcat_code = fields.Char(string="xTuple Product Category Code")


class ProductProduct(models.Model):
    _inherit = "product.product"

    xtuple_item_id = fields.Integer(string="xTuple Item ID", index=True)
    xtuple_item_number = fields.Char(string="xTuple Item Number")
    xtuple_item_type = fields.Char(
        string="xTuple Item Type", help="P=Purchased, M=Manufactured, F=Phantom, etc."
    )
    xtuple_classcode = fields.Char(string="xTuple Class Code")


class ProductSupplierInfo(models.Model):
    _inherit = "product.supplierinfo"

    xtuple_itemsrc_id = fields.Integer(string="xTuple Item Source ID", index=True)
    xtuple_default = fields.Boolean(string="xTuple Default Supplier")
