from odoo import models, fields, api, _
from odoo.sql_db import db_connect

PAGE_SIZE = 1000


class SapDatabase(models.Model):
    _name = "sap.database"
    _description = "SAP Database"

    database_host = fields.Char(required=True)
    database_name = fields.Char(required=True)
    database_username = fields.Char(required=True)
    database_password = fields.Char(required=True)
    database_port = fields.Integer(required=True)
    database_schema = fields.Char(required=True)
    data_mapping_ids = fields.One2many(
        comodel_name="sap.data.mapping",
        inverse_name="sap_db_id",
    )

    @api.depends("database_host", "database_name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.database_host}/{rec.database_name}"

    def get_cursor(self):
        self.ensure_one()
        uri = (
            "postgresql://{user}:{password}@{host}:{port}/{database}?"
            "options=-c%20search_path%3D{schema}"
        ).format(
            user=self.database_username,
            password=self.database_password,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
            schema=self.database_schema,
        )
        connection = db_connect(uri, allow_uri=True)
        return connection.cursor()

    def _import_all(self):
        for rec in self:
            self.env["sap.res.partner.importer"].import_partners(self.get_cursor())
