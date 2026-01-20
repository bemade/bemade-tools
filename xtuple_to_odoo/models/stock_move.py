"""Stock Move extensions for xTuple integration."""

from odoo import fields, models


class StockMove(models.Model):
    _inherit = "stock.move"

    xtuple_womatl_id = fields.Integer(
        string="xTuple WO Material ID",
        index=True,
        copy=False,
        help="Reference to the xTuple womatl record",
    )
