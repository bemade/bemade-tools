"""HR Employee model extensions for QBO integration."""

from odoo import fields, models


class HrEmployee(models.Model):
    """Extend hr.employee with QBO tracking fields."""

    _inherit = "hr.employee"

    qbo_employee_id = fields.Integer(
        string="QBO Employee ID",
        index=True,
        copy=False,
        help="QuickBooks Online Employee ID",
    )
