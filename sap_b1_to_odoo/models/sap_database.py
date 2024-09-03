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
        uri = "postgresql://%{user}:%{pass}@%{host}:%{port}/%{database}" % {
            "user": self.database_username,
            "pass": self.database_password,
            "host": self.database_host,
            "port": self.database_port,
            "database": self.database_name,
        }
        connection = db_connect(uri)
        return connection.cursor()

    def _import_all(self):
        for rec in self:
            rec._import_partners()

    def _import_partners(self):
        self.ensure_one()
        cr = self.get_cursor()
        sch = self.database_schema
        # Get count for pagination
        partner_count = cr.execute(f"SELECT count(*) from {sch}.OCRD")
        for offset in range(0, partner_count, PAGE_SIZE):
            sap_partners = cr.execute(
                f"SELECT * from {sch}.OCRD limit {PAGE_SIZE} offset {offset}"
            ).dictfetchall()
            partner_vals = []
            for sap_partner in sap_partners:
                mailcountr = sap_partner["mailcountr"]
                country = (
                    self.env["res.country"].search(
                        [
                            ("|"),
                            ("code", "=", mailcountr),
                        ]
                    )
                    if mailcountr and mailcountr != "XX"
                    else None
                )
                state1 = sap_partner["state1"]
                if country and state1:
                    state = self.env["res.country.state"].search(
                        [("code", "=", state1), ("country_id", "=", country.id)]
                    )
                elif state1:
                    state = self.env["res.country.state"].search(
                        [
                            ("code", "=", state1),
                            ("country_id.code", "in", ["CA", "US"]),
                        ]
                    )
                else:
                    state = None
                partner_vals.append(
                    {
                        "sap_docentry": sap_partner["docentry"],
                        "name": sap_partner["cardname"],
                        "street": sap_partner["address"],
                        "street2": sap_partner["mailcounty"] or "",
                        "city": sap_partner["city"] or "",
                        "country_id": country and country.id or False,
                        "state_id": state and state.id or False,
                    }
                )
            self.env["res.partner"].create(partner_vals)
