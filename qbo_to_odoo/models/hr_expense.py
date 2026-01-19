"""HR Expense model extensions for QBO integration."""

from odoo import fields, models


class HrExpense(models.Model):
    """Extend hr.expense with QBO tracking fields."""

    _inherit = "hr.expense"

    qbo_expense_id = fields.Integer(
        string="QBO Expense ID",
        index=True,
        copy=False,
        help="QuickBooks Online Expense ID",
    )
