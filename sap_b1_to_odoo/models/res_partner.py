from odoo import models, fields, api


class ResPartner(models.Model):
    _inherit = "res.partner"

    sap_docentry = fields.Integer()

    _sql_constraints = [
        (
            "sap_docentry_unique",
            "unique (sap_docentry)",
            "An partner with that SAP docentry already exists",
        )
    ]
