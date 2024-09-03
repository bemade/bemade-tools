from odoo import models, fields, api, _
from odoo.sql_db import db_connect


class SapDatabase(models.Model):
    _name = "sap.database"
    _description = "SAP Database"

    database_host = fields.Char()
    database_name = fields.Char()
    database_username = fields.Char()
    database_password = fields.Char()
    database_port = fields.Integer()

    def get_cursor(self):
        self.ensure_one()
        uri = "postgresql://%{user}:%{pass}@%{host}:%{port}/%{database}" % {
            "user": self.database_username,
            "pass": self.database_password,
            "host": self.database_host,
            "port": self.database_port,
            "database": self.database_name,
        }
        connection = db_connect(uri)
        return connection.cursor()