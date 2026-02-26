from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = "account.payment"

    qbo_payment_id = fields.Integer(index=True, copy=False)
    qbo_bill_payment_id = fields.Integer(index=True, copy=False)
