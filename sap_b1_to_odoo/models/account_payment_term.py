from odoo import models, fields


class AccountPaymentTerm(models.Model):
    _inherit = "account.payment.term"

    sap_groupnum = fields.Integer(index="btree", copy=False)

    _sql_constraints = [
        (
            "unique_sap_groupnum",
            "UNIQUE(sap_groupnum)",
            "A payment term with this SAP ID already exists.",
        )
    ]
