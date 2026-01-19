"""Account model extensions for QBO integration."""

from odoo import fields, models


class AccountAccount(models.Model):
    """Extend account.account with QBO tracking fields."""

    _inherit = "account.account"

    qbo_id = fields.Integer(
        string="QBO Account ID",
        index=True,
        copy=False,
        help="QuickBooks Online Account ID",
    )
