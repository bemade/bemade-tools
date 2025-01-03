from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class Users(models.Model):
    _inherit = "res.users"

    sap_slpcode = fields.Char(
        string="SAP SLP Code",
    )


class ResUsersImporter(models.AbstractModel):
    _name = "res.users.importer"
    _description = "Users Importer"

    @api.model
    def import_salespeople(self, cr):
        cr.execute("SELECT * FROM oslp")
        salespeople = cr.dictfetchall()
        _logger.info(f"Importing {len(salespeople)} salespeople...")
        vals = []
        for salesperson in salespeople:
            name = salesperson["slpname"]
            slp_code = salesperson["slpcode"]
            login = "_".join(name.split()).lower()
            company = self.env.company
            vals.append(
                {
                    "name": name,
                    "login": login,
                    "company_id": company.id,
                    "sap_slpcode": slp_code,
                    "active": False,
                }
            )
        users = self.env["res.users"].create(vals)
        partners = self.env["res.partner"].search([("user_ids", "in", users.ids)])
        partners.write({"active": False})
        _logger.info(f"Salespeople imported.")
