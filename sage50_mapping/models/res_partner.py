"""Where an Odoo partner came from in Sage."""

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Two fields, not one. Sage keeps customers (`tcustomr`) and vendors
    # (`tvendor`) in unrelated tables with independent id sequences, so a
    # company that is both has two Sage ids and one Odoo partner, and a
    # single field could only record one of them.
    sage_customer_id = fields.Integer(
        string="Sage customer", index=True, copy=False,
        help="Row id in the Sage 50 `tcustomr` table.",
    )
    sage_vendor_id = fields.Integer(
        string="Sage vendor", index=True, copy=False,
        help="Row id in the Sage 50 `tvendor` table.",
    )
