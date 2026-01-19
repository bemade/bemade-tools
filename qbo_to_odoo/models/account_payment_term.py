"""Payment term model extensions for QBO integration."""

from odoo import fields, models


class AccountPaymentTerm(models.Model):
    """Extend account.payment.term with QBO tracking fields."""

    _inherit = "account.payment.term"

    qbo_term_id = fields.Integer(
        string="QBO Term ID",
        index=True,
        copy=False,
        help="QuickBooks Online Term ID",
    )
