"""Tax model extensions for QBO integration."""

from odoo import fields, models


class AccountTax(models.Model):
    """Extend account.tax with QBO tracking fields."""

    _inherit = "account.tax"

    qbo_tax_id = fields.Char(
        string="QBO Tax Code ID",
        index=True,
        copy=False,
        help="QuickBooks Online TaxCode ID",
    )
    qbo_tax_rate_id = fields.Char(
        string="QBO Tax Rate ID",
        index=True,
        copy=False,
        help="QuickBooks Online TaxRate ID",
    )
