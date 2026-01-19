"""Partner model extensions for QBO integration."""

from odoo import fields, models


class ResPartner(models.Model):
    """Extend res.partner with QBO tracking fields."""

    _inherit = "res.partner"

    qbo_customer_id = fields.Integer(
        string="QBO Customer ID",
        index=True,
        copy=False,
        help="QuickBooks Online Customer ID",
    )
    qbo_vendor_id = fields.Integer(
        string="QBO Vendor ID",
        index=True,
        copy=False,
        help="QuickBooks Online Vendor ID",
    )
