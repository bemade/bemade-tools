"""Stock Quant extensions for xTuple integration."""

from odoo import fields, models


class StockQuant(models.Model):
    _inherit = "stock.quant"

    xtuple_itemsite_id = fields.Integer(
        string="xTuple Itemsite ID",
        index=True,
        copy=False,
        help="Reference to the xTuple itemsite record",
    )
