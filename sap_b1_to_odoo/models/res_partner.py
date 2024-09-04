from odoo import models, fields, api
from .sap_database import PAGE_SIZE


class ResPartner(models.Model):
    _inherit = "res.partner"

    cardcode = fields.Char(index="trigram")

    _sql_constraints = [
        (
            "sap_cardcode_unique",
            "unique (cardcode)",
            "An partner with that SAP cardcode already exists",
        )
    ]


class SapResPartnerImporter(models.AbstractModel):
    _name = "sap.res.partner.importer"

    @api.model
    def import_partners(self, cr):
        # Import companies
        self._import_ocrd(cr)
        # Get count for pagination

    def _import_ocrd(self, cr):
        where = f"WHERE cardname is not null and cardname <> ''"

        cr.execute(f"SELECT * from OCRD {where}")
        sap_partners = cr.dictfetchall()
        partner_vals = []
        for sap_partner in sap_partners:
            mailcountr = sap_partner["mailcountr"]
            country = (
                self.env["res.country"].search([("code", "=", mailcountr)])
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
                    "cardcode": sap_partner["cardcode"],
                    "name": sap_partner["cardname"],
                    "street": sap_partner["address"],
                    "street2": sap_partner["mailcounty"] or "",
                    "city": sap_partner["city"] or "",
                    "country_id": country and country.id or False,
                    "state_id": state and state.id or False,
                }
            )
        self.env["res.partner"].create(partner_vals)
        # second pass to connect children to parents
        cr.execute(f"SELECT * FROM OCRD {where} AND fathercard is not null")
        sap_children = cr.dictfetchall()
        parent_codes = [child["fathercard"] for child in sap_children]
        child_codes = [child["cardcode"] for child in sap_children]
        parents = self.env["res.partner"].search([("cardcode", "in", parent_codes)])
        children = self.env["res.partner"].search([("cardcode", "in", child_codes)])
        for sap_child in sap_children:
            parent = parents.filtered(
                lambda partner: partner.cardcode == sap_child["fathercard"]
            )
            child = children.filtered(
                lambda partner: partner.cardcode == sap_child["cardcode"]
            )
            child.parent_id = parent
