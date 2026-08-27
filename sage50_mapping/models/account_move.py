"""Where an Odoo journal entry came from in Sage."""

from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    sage_doc_id = fields.Integer(
        string="Sage document", index=True, copy=False,
        help="Row id of the source document in Sage 50 (`tcustr` for a "
             "receivable, `tventr` for a payable).",
    )
    sage_application_id = fields.Integer(
        string="Sage application", index=True, copy=False,
        help="Set on the journal entry behind an imported payment. Carried on "
             "the move as well as the payment so the take-on's reversing "
             "entry can find every imported line with one search.",
    )


class AccountPayment(models.Model):
    _inherit = "account.payment"

    sage_application_id = fields.Integer(
        string="Sage application", index=True, copy=False,
        help="Row id of the application in Sage 50 (`tcustrdt` for a receipt, "
             "`tventrdt` for a payment). An application is what Sage records "
             "when a receipt, a payment or a credit note is set against a "
             "document.",
    )
