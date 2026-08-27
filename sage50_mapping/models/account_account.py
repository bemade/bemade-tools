"""Where an Odoo account came from in Sage."""

from odoo import fields, models


class AccountAccount(models.Model):
    _inherit = "account.account"

    sage_account_id = fields.Integer(
        string="Sage account",
        index=True,
        copy=False,
        help="The 8-digit account number in the Sage 50 company file. Sage's "
             "numbers are trimmed on import to keep reports readable, so this "
             "is what makes the mapping reversible.",
    )
