from odoo import fields, models


class Users(models.Model):
    _inherit = "res.users"

    sap_slpcode = fields.Integer(
        string="SAP SLP Code",
        copy=False,
    )
