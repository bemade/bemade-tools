# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountAccount(models.Model):
    _inherit = "account.account"

    sap_acct_code = fields.Char(
        string="SAP Account Code",
        help="Original account code from SAP B1 OACT table",
        index=True,
    )
