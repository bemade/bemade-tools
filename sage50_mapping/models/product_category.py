"""Where an Odoo product category came from in Sage."""

from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = "product.category"

    sage_income_account = fields.Integer(
        string="Sage revenue account", index=True, copy=False,
        help="The Sage revenue account this category was derived from. Sage "
             "50 has a category table but sites routinely leave it unused and "
             "encode the same dimension in the per-item revenue account "
             "instead, which is what the importer reads.",
    )
