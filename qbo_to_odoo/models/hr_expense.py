"""HR Expense model extensions for QBO integration."""

from odoo import fields, models


class HrExpense(models.Model):
    """Extend hr.expense with QBO tracking fields."""

    _inherit = "hr.expense"

    qbo_purchase_id = fields.Integer(
        string="QBO Purchase ID",
        index=True,
        copy=False,
        help="QuickBooks Online Purchase ID (expense transaction)",
    )
