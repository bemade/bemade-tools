"""Where an Odoo journal entry came from in Sage."""

from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    sage_doc_id = fields.Integer(
        string="Sage document", index=True, copy=False,
        help="Row id of the source document in Sage 50 (`tcustr` for a "
             "receivable, `tventr` for a payable).",
    )
