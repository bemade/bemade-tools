"""Expose the Sage document id to Invoices Analysis.

Historical invoices are imported and then cancelled: a cancelled move has no
effect whatsoever on the ledger or the tax return, but keeps its product,
quantity and price lines. `account.invoice.report` has no state filter of its
own — the exclusion of drafts and cancelled documents is a removable search
filter — so the history is already in the dataset.

What is missing is a way to tell an imported historical invoice apart from an
invoice somebody cancelled by mistake. That is what this field gives the saved
filter shipped alongside it.
"""

from odoo import api, models, fields
from odoo.tools import SQL


class AccountInvoiceReport(models.Model):
    _inherit = "account.invoice.report"

    sage_doc_id = fields.Integer(string="Sage document", readonly=True)

    @api.model
    def _select(self):
        return SQL("%s, move.sage_doc_id AS sage_doc_id", super()._select())
