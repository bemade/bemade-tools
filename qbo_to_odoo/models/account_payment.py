"""Payment model extensions for QBO integration."""

from odoo import fields, models


class AccountPayment(models.Model):
    """Extend account.payment with QBO tracking fields."""

    _inherit = "account.payment"

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
