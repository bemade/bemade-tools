from odoo import models, fields, api
from .sap_database import PAGE_SIZE
import logging

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    sap_card_code = fields.Char(index="trigram")
    sap_parent_card = fields.Char(index="trigram")
    sap_cntct_code = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "sap_cardcode_unique",
            "unique (sap_card_code)",
            "An partner with that SAP cardcode already exists",
        ),
        (
            "sap_cntct_code_unique",
            "unique (sap_cntct_code)",
            "A partner with that SAP Contact Code already exists",
        ),
    ]


class SapResPartnerImporter(models.AbstractModel):
    _name = "sap.res.partner.importer"

    @api.model
    def import_partners(self, cr):
        _logger.info("Starting SAP partner import.")
        partners = self._import_ocrd(cr)
        partners |= self._import_ocpr(cr)
        self._link_children_parents(partners)

    def _extract_sap_state_country(self, country, state):
        country = (
            self.env["res.country"].search([("code", "=", country)])
            if country and country != "XX"
            else None
        )
        if country and state:
            state = self.env["res.country.state"].search(
                [("code", "=", state), ("country_id", "=", country.id)]
            )
        elif state:
            state = self.env["res.country.state"].search(
                [
                    ("code", "=", state),
                    ("country_id.code", "in", ["CA", "US"]),
                ]
            )
        else:
            state = None
        return country, state

    def _extract_sap_street_street2(self, address, block):
        if block and not address:
            return block, ""
        return address, block

    def _import_ocrd(self, cr):
        """Import business partners (companies)"""
        cr.execute(f"SELECT * from OCRD WHERE cardname is not null and cardname <> ''")
        sap_partners = cr.dictfetchall()
        _logger.info(f"Importing {len(sap_partners)} companies.")
        partner_vals = []
        for sap_partner in sap_partners:
            # Start with the parent company
            country = sap_partner["country"]
            state = sap_partner["state1"]
            country, state = self._extract_sap_state_country(country, state)
            street, street2 = self._extract_sap_street_street2(
                sap_partner["address"],
                sap_partner["block"],
            )
            partner_vals.append(
                {
                    "sap_card_code": sap_partner["cardcode"],
                    "name": sap_partner["cardname"],
                    "street": street,
                    "street2": street2,
                    "city": sap_partner["city"] or "",
                    "country_id": country and country.id or False,
                    "state_id": state and state.id or False,
                    "sap_parent_card": sap_partner["fathercard"] or False,
                    "phone": sap_partner["phone1"] or sap_partner["phone2"],
                    "email": sap_partner["e_mail"],
                    "is_company": True,
                }
            )
            # Then the billing address
            if sap_partner["address"] and state and country:
                partner_vals.append(
                    {
                        # No cardcode here, we are splitting out the billing address
                        # so it goes in the parent card field
                        "name": sap_partner["cardname"],
                        "street": sap_partner["address"],
                        "street2": sap_partner["county"] or sap_partner["block"],
                        "city": sap_partner["city"] or "",
                        "country_id": country and country.id or False,
                        "state_id": state and state.id or False,
                        "sap_parent_card": sap_partner["cardcode"],
                        "is_company": False,
                        "phone": sap_partner["phone1"] or sap_partner["phone2"],
                        "email": sap_partner["e_mail"],
                        "type": "invoice",
                    }
                )
            # Then the shipping address
            country = sap_partner["mailcountr"]
            state = sap_partner["state2"]
            country, state = self._extract_sap_state_country(country, state)
            street, street2 = self._extract_sap_street_street2(
                sap_partner["mailaddres"],
                sap_partner["mailblock"],
            )
            if sap_partner["mailaddres"] and country and state:
                partner_vals.append(
                    {
                        # No cardcode here, we are splitting out the shipping address
                        # so it goes in the parent card field
                        "name": sap_partner["cardname"],
                        "street": street,
                        "street2": street2,
                        "city": sap_partner["mailcity"] or "",
                        "country_id": country and country.id or False,
                        "state_id": state and state.id or False,
                        "sap_parent_card": sap_partner["cardcode"],
                        "is_company": False,
                        "phone": sap_partner["phone1"] or sap_partner["phone2"],
                        "email": sap_partner["e_mail"],
                        "type": "delivery",
                    }
                )

        return self.env["res.partner"].create(partner_vals)

    def _link_children_parents(self, partners):
        partner_code_map = {partner.sap_card_code: partner for partner in partners}
        _logger.info("Linking partners to their parents in a hierarchy...")
        for partner in partners.filtered(
            lambda partner: partner.sap_parent_card != False
        ):
            partner.parent_id = partner_code_map[partner.sap_parent_card]

    def _import_ocpr(self, cr):
        """Import contacts"""
        cr.execute("SELECT * from OCPR WHERE name <> '' and name is not null ")
        sap_contacts = cr.dictfetchall()
        _logger.info(f"Importing {len(sap_contacts)} contacts.")
        partner_vals = []
        for sap_contact in sap_contacts:
            sap_country = sap_contact["residcntry"]
            sap_state = sap_contact["residstate"]
            country, state = self._extract_sap_state_country(sap_country, sap_state)
            partner_vals.append(
                {
                    "name": sap_contact["name"],
                    "sap_cntct_code": sap_contact["cntctcode"],
                    "sap_parent_card": sap_contact["cardcode"],
                    "street": sap_contact["address"],
                    "country_id": country and country.id or False,
                    "state_id": state and state.id or False,
                    "is_company": False,
                    "email": sap_contact["e_maill"],
                    "phone": sap_contact["tel1"] or sap_contact["tel2"],
                    "mobile": sap_contact["cellolar"],
                    "active": sap_contact["active"] == "Y",
                    "function": sap_contact["position"] or sap_contact["title"],
                }
            )
        return self.env["res.partner"].create(partner_vals)
