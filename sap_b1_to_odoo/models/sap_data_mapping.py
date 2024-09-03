from odoo import models, fields, api


class SapDataMapping(models.Model):
    _name = "sap.data.mapping"
    _description = "SAP Data Mapping"

    sap_db_id = fields.Many2one(
        comodel_name="sap.database",
    )
    sap_table_name = fields.Char()
    sap_col_name = fields.Char()
    odoo_model_name = fields.Char()
    odoo_field_name = fields.Char()