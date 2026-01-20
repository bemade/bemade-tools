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
    qbo_payment_id = fields.Integer(
        string="QBO Payment ID",
        index=True,
        copy=False,
        help="QuickBooks Online Payment ID (customer payments)",
    )
    qbo_bill_payment_id = fields.Integer(
        string="QBO Bill Payment ID",
        index=True,
        copy=False,
        help="QuickBooks Online BillPayment ID (vendor payments)",
    )
    qbo_credit_memo_id = fields.Integer(
        string="QBO Credit Memo ID",
        index=True,
        copy=False,
        help="QuickBooks Online CreditMemo ID (customer refunds)",
    )
    qbo_vendor_credit_id = fields.Integer(
        string="QBO Vendor Credit ID",
        index=True,
        copy=False,
        help="QuickBooks Online VendorCredit ID (vendor refunds)",
    )
    qbo_expense_id = fields.Integer(
        string="QBO Expense ID",
        index=True,
        copy=False,
        help="QuickBooks Online Purchase ID (expenses)",
    )
