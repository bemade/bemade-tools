"""Where an Odoo product came from in Sage."""

from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    sage_product_id = fields.Integer(
        string="Sage item", index=True, copy=False,
        help="Row id in the Sage 50 `tinvent` table.",
    )
    sage_unit = fields.Char(
        string="Sage unit",
        help="The free-text unit Sage carried. Kept because Sage's selling "
             "and stocking units are labels rather than convertible units — "
             "the conversion factor is 1.0 even where the two differ — so "
             "they cannot be imported as a UoM tree without inventing "
             "relationships that do not exist.",
    )
