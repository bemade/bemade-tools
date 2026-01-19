"""Sale order model extensions for QBO integration."""

from odoo import fields, models


class SaleOrder(models.Model):
    """Extend sale.order with QBO tracking fields."""

    _inherit = "sale.order"

    qbo_estimate_id = fields.Integer(
        string="QBO Estimate ID",
        index=True,
        copy=False,
        help="QuickBooks Online Estimate ID",
    )
