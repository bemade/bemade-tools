from odoo import fields, models


class AccountTax(models.Model):
    _inherit = "account.tax"

    sap_tax_code = fields.Char(
        string="SAP Tax Code",
        help="Original tax code from SAP B1 OSTC table",
        index=True,
    )
