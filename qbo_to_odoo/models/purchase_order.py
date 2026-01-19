"""Purchase order model extensions for QBO integration."""

from odoo import fields, models


class PurchaseOrder(models.Model):
    """Extend purchase.order with QBO tracking fields."""

    _inherit = "purchase.order"

    qbo_purchase_order_id = fields.Integer(
        string="QBO Purchase Order ID",
        index=True,
        copy=False,
        help="QuickBooks Online PurchaseOrder ID",
    )
