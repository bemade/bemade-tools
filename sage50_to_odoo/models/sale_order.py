"""Which Sage order an imported open sales order came from.

Kept on the migration module rather than on `sage50_mapping`: it only makes
the import re-runnable, and nothing needs it once the take-on is promoted.
"""

from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    sage_order_id = fields.Integer(
        string="Sage order", index=True, copy=False,
        help="Row id of the order in Sage 50 (`tsalordr`).",
    )
