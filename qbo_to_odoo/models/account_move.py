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
    qbo_transfer_id = fields.Integer(
        string="QBO Transfer ID",
        index=True,
        copy=False,
        help="QuickBooks Online Transfer ID (bank transfers)",
    )
    qbo_deposit_id = fields.Integer(
        string="QBO Deposit ID",
        index=True,
        copy=False,
        help="QuickBooks Online Deposit ID (bank deposits)",
    )
    qbo_sales_receipt_id = fields.Integer(
        string="QBO Sales Receipt ID",
        index=True,
        copy=False,
        help="QuickBooks Online SalesReceipt ID (cash sales)",
    )
    qbo_refund_receipt_id = fields.Integer(
        string="QBO Refund Receipt ID",
        index=True,
        copy=False,
        help="QuickBooks Online RefundReceipt ID (customer refunds)",
    )
    qbo_tax_payment_id = fields.Integer(
        string="QBO Tax Payment ID",
        index=True,
        copy=False,
        help="QuickBooks Online TaxPayment ID (sales tax remittances)",
    )
    qbo_cc_payment_id = fields.Integer(
        string="QBO CC Payment ID",
        index=True,
        copy=False,
        help="QuickBooks Online CreditCardPayment ID",
    )
