"""Account move model extensions for QBO integration."""

from odoo import fields, models


class AccountMove(models.Model):
    """Extend account.move with QBO tracking fields."""

    _inherit = "account.move"

    qbo_journal_entry_id = fields.Integer(
        string="QBO Journal Entry ID",
        index=True,
        copy=False,
        help="QuickBooks Online Journal Entry ID",
    )
    qbo_invoice_id = fields.Integer(
        string="QBO Invoice ID",
        index=True,
        copy=False,
        help="QuickBooks Online Invoice ID",
    )
    qbo_bill_id = fields.Integer(
        string="QBO Bill ID",
        index=True,
        copy=False,
        help="QuickBooks Online Bill ID",
    )
