"""xTuple Purchase Order Model Extensions

This module adds xTuple-specific fields to purchase order models
for tracking imported purchase orders.
"""

from odoo import fields, models


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    xtuple_pohead_id = fields.Integer(
        string="xTuple PO Head ID",
        index=True,
        copy=False,
    )


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    xtuple_poitem_id = fields.Integer(
        string="xTuple PO Item ID",
        index=True,
        copy=False,
    )
