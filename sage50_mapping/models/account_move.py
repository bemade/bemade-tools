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
    sage_gl_entry_ref = fields.Char(
        string="Sage GL entry", index=True, copy=False,
        help="The Sage journal entry this move came from, or that it "
             "replaces, as `<generation table>:<row id>` — for example "
             "`tjourent:4812`. A document imported as a real invoice carries "
             "the entry Sage posted behind it, so the general-ledger replay "
             "can skip it instead of posting the document a second time. "
             "The generation is part of the key because Sage restarts row "
             "ids in every fiscal generation: the bare id names a different "
             "entry in each one.",
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
