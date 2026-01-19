"""Product category model extensions for QBO integration."""

from odoo import fields, models


class ProductCategory(models.Model):
    """Extend product.category with QBO tracking fields."""

    _inherit = "product.category"

    qbo_category_id = fields.Char(
        string="QBO Category ID",
        index=True,
        copy=False,
        help="QuickBooks Online Category ID",
    )
