import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    sap_card_code = fields.Char(index="btree")
    sap_parent_card = fields.Char(index="btree")
    sap_cntct_code = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "sap_cardcode_unique",
            "unique (sap_card_code)",
            "An partner with that SAP cardcode already exists.",
        ),
        (
            "sap_cntct_code_unique",
            "unique (sap_cntct_code)",
            "A partner with that SAP Contact Code already exists.",
        ),
    ]


class SapResPartnerImporter(models.AbstractModel):
    _name = "sap.res.partner.importer"
    _description = "SAP Partner Importer"

    _basic_pricelists_dict = {}
    _sap_users_dict = None
    _countries_dict = None
    _states_dict = None

    @api.model
    def _get_user(self, sap_slpcode):
        cls = self.__class__
        if cls._sap_users_dict is None:
            cls._sap_users_dict = {
                user.sap_slpcode: user.id
                for user in self.env["res.users"].search(
                    [
                        ("sap_slpcode", "!=", False),
                        ("active", "in", [False, True]),
                    ]
                )
            }
        return cls._sap_users_dict.get(sap_slpcode, False)

    @api.model
    def import_partners(self, cr):
        _logger.info("Starting SAP partner import.")
        partners = self._import_ocrd(cr)
        partners |= self._import_crd1(cr)
        partners |= self._import_ocpr(cr)
        self._link_children_parents(partners)

    @api.model
    def _get_state(self, code, country_code=None):
        """Get the state associated with code and country_code. If no country_code is
        set, try first a Canadian province and, if not found, a US state."""
        try:
            cls = self.__class__
            if cls._states_dict is None:
                cls._states_dict = {
                    state.country_id.code: {state.code: state}
                    for state in self.env["res.country.state"].search([])
                }
            if not code:
                return False
            if not country_code:
                return cls._states_dict.get("CA").get(
                    code, False
                ) or cls._states_dict.get("US").get(code, False)
            return cls._states_dict.get(country_code).get(code, False)
        except Exception as e:
            _logger.error(f"Error getting state {code} for country {country_code}")
            raise e

    @api.model
    def _get_country(self, code):
        cls = self.__class__
        if cls._countries_dict is None:
            cls._countries_dict = {
                country.code: country for country in self.env["res.country"].search([])
            }
        return cls._countries_dict.get(code, False)

    @api.model
    def _extract_sap_state_country(self, country, state):
        odoo_country = self._get_country(country)
        odoo_state = self._get_state(state, country)
        return odoo_country, odoo_state

    @api.model
    def _get_basic_pricelists_dict(self):
        lists_dict = self.__class__._basic_pricelists_dict
        if not lists_dict:
            pricelists = self.env["product.pricelist"].search(
                [("sap_listnum", "!=", False)]
            )
            lists_dict = self.__class__._basic_pricelists_dict = {
                pricelist.sap_listnum: pricelist for pricelist in pricelists
            }
        return lists_dict

    @api.model
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
        basic_pricelists = self._get_basic_pricelists_dict()
        for sap_partner in sap_partners:
            # Start with the parent company
            country = sap_partner["country"]
            state = sap_partner["state1"]
            country, state = self._extract_sap_state_country(country, state)
            street, street2 = self._extract_sap_street_street2(
                sap_partner["address"],
                sap_partner["block"],
            )
            user = self._get_user(sap_partner["slpcode"])
            partner_vals.append(
                {
                    "sap_card_code": sap_partner["cardcode"],
                    "name": sap_partner["cardname"],
                    "street": street,
                    "street2": street2,
                    "city": sap_partner["city"] or "",
                    "country_id": country and country.id or False,
                    "state_id": state and state.id or False,
                    "zip": sap_partner["zipcode"],
                    "sap_parent_card": sap_partner["fathercard"] or False,
                    "phone": sap_partner["phone1"] or sap_partner["phone2"],
                    "email": sap_partner["e_mail"],
                    "is_company": True,
                    "company_id": self.env.company.id,
                    "comment": sap_partner["notes"],
                    "user_id": user and user.id,
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
            user = self._get_user(sap_partner["slpcode"])
            if (street or street2) and country and state:
                partner_vals.append(
                    {
                        # No cardcode here, we are splitting out the shipping address
                        # so it goes in the parent card field
                        "name": "Réception / Receiving",
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
                        "company_id": self.env.company.id,
                        "comment": sap_partner["notes"],
                        "user_id": user and user.id,
                    }
                )

        return self.env["res.partner"].create(partner_vals)

    def _import_crd1(self, cr):
        cr.execute(f"SELECT * FROM crd1")
        sap_addresses = cr.dictfetchall()
        _logger.info(f"Importing {len(sap_addresses)} addresses.")
        partner_vals = []

        def _extract_name_street_street2(address, street, address2, address3):
            """Addresses in SAP have 4 possible lines that would match with street1
            and street2 from Odoo. Intelligently concatenate depending on which
            lines are set or not set."""
            addressParts = [
                part for part in [address, street, address2, address3] if part
            ]
            if len(addressParts) > 3:
                return addressParts[0], addressParts[1], ", ".join(addressParts[2:])
            else:
                addressParts += ["" for _ in range(3 - len(addressParts))]
                return tuple(addressParts)

        for sap_address in sap_addresses:
            name, street, street2 = _extract_name_street_street2(
                sap_address["address"],
                sap_address["street"],
                sap_address["address2"],
                sap_address["address3"],
            )
            parent_card = sap_address["cardcode"]
            country, state = self._extract_sap_state_country(
                sap_address["country"], sap_address["state"]
            )
            zip = sap_address["zipcode"]
            partner_vals.append(
                {
                    "name": name,
                    "street": street,
                    "street2": street2,
                    "city": sap_address["city"],
                    "country_id": country and country.id or False,
                    "state_id": state and state.id or False,
                    "sap_parent_card": parent_card,
                    "type": "delivery",
                    "is_company": False,
                    "user_id": False,
                }
            )
        return self.env["res.partner"].create(partner_vals)

    @api.model
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
                    "company_id": self.env.company.id,
                    "comment": sap_contact["notes1"] or sap_contact["notes2"] or "",
                }
            )
        return self.env["res.partner"].create(partner_vals)

    def _delete_all(self):
        self.env.cr.execute(
            """
            DELETE FROM res_partner WHERE sap_card_code is not null 
            or sap_cntct_code is not null 
            or sap_parent_card is not null
            """
        )
