"""Product model extensions for QBO integration."""

from odoo import fields, models


class ProductProduct(models.Model):
    """Extend product.product with QBO tracking fields."""

    _inherit = "product.product"

    qbo_item_id = fields.Integer(
        string="QBO Item ID",
        index=True,
        copy=False,
        help="QuickBooks Online Item ID",
    )
