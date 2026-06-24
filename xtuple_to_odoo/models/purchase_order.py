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
    # Persisted so the post-import receipt step can classify each imported PO as
    # an OPEN order (xTuple 'U'/Unreleased — the client's working/open POs, which
    # get real incoming pickings) versus a HISTORICAL order (xTuple 'O'/'C', which
    # are closed out fully delivered + invoiced regardless of actual qty). xTuple
    # drops this distinction once it lands as Odoo state='purchase', so we carry
    # the raw single-char status forward (task #3814).
    xtuple_pohead_status = fields.Char(
        string="xTuple PO Status",
        index=True,
        copy=False,
        help="Raw xTuple pohead_status: 'U' (Unreleased/open), "
        "'O' (Open/historical), 'C' (Closed/historical).",
    )


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    xtuple_poitem_id = fields.Integer(
        string="xTuple PO Item ID",
        index=True,
        copy=False,
    )
