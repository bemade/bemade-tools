# TODO: add a fix_quotes here for contact names
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from odoo.tools.sql import SQL

from odoo import models, fields, api, Command
from odoo.modules.registry import Registry
from odoo.addons.sap_b1_to_odoo.tools import fix_quotes

_logger = logging.getLogger(__name__)
max_workers = os.cpu_count() - 1


def _create_partners_concurrent(dbname, uid, context, sap_partners, vals_func_name):
    _logger.info(f"Creating partners from {len(sap_partners)} SAP partners.")
    with Registry(dbname).cursor() as cr:
        env = api.Environment(cr, uid, context)
        vals_func = getattr(env["sap.res.partner.importer"], vals_func_name)
        partner_vals = vals_func(sap_partners)
        return env["res.partner"].create(partner_vals).ids


class ResPartner(models.Model):
    _inherit = "res.partner"

    sap_card_code = fields.Char(index="btree")
    sap_parent_card = fields.Char(index="btree")
    sap_cntct_code = fields.Integer(index="btree")
    sap_atcentry = fields.Integer(index="btree")
    sap_partner_type = fields.Char(index="btree")

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

    @api.model
    def _get_users_dict(self):
        return {
            user.sap_slpcode: user.id
            for user in self.env["res.users"].search(
                [
                    ("sap_slpcode", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
        }

    def import_payment_terms(self, cr):
        self._import_octg(cr)

    def _import_octg(self, cr):
        """Import payment terms."""
        cr.execute("SELECT * from octg")
        sap_terms = cr.dictfetchall()
        vals = []
        for term in sap_terms:
            vals.append(
                {
                    "name": term["pymntgroup"],
                    "sap_groupnum": term["groupnum"],
                    "line_ids": [
                        Command.create(
                            {
                                "value_amount": 100.0,
                                "value": "percent",
                                "nb_days": term["extradays"],
                                "delay_type": "days_after",
                            }
                        )
                    ],
                }
            )
        return self.env["account.payment.term"].create(vals)

    def _get_payment_terms_dict(self):
        terms = self.env["account.payment.term"].search([])
        return {term.sap_groupnum: term.id for term in terms}

    @api.model
    def import_partners_concurrent(self, cr):
        _logger.info("Starting SAP partner import.")
        partner_ids = self._import_ocrd_concurrent(cr)
        partner_ids += self._import_crd1_concurrent(cr)
        partner_ids += self._import_ocpr_concurrent(cr)
        self.env.invalidate_all()
        self._link_children_parents()
        self._set_payable_receivable_accounts()

    @api.model
    def _set_payable_receivable_accounts(self):
        self.env.flush_all()
        cad_receivable = (
            self.env["account.account"]
            .search(
                [("account_type", "=", "asset_receivable"), ("name", "ilike", "%CDN")]
            )
            .id
        )
        cad_payable = (
            self.env["account.account"]
            .search(
                [("account_type", "=", "liability_payable"), ("name", "ilike", "%CDN")]
            )
            .id
        )
        usd_receivable = (
            self.env["account.account"]
            .search(
                [("account_type", "=", "asset_receivable"), ("name", "ilike", "%US")]
            )
            .id
        )
        usd_payable = (
            self.env["account.account"]
            .search(
                [("account_type", "=", "liability_payable"), ("name", "ilike", "%US")]
            )
            .id
        )
        usd_currency = self.env["res.currency"].search([("name", "=", "USD")]).id
        usd_pricelist_partners = self.env["res.partner"].search(
            [("specific_property_product_pricelist.currency_id", "=", usd_currency)]
        )
        _logger.info(
            f"Updating {len(usd_pricelist_partners)} USD pricelist partners with account {usd_receivable}"
        )
        usd_pricelist_partners.write({"property_account_receivable_id": usd_receivable})
        cad_pricelist_partners = self.env["res.partner"].search(
            [
                "|",
                ("specific_property_product_pricelist.currency_id", "!=", usd_currency),
                ("specific_property_product_pricelist", "=", False),
            ]
        )
        _logger.info(
            f"Updating {len(cad_pricelist_partners)} CAD pricelist partners with account {cad_receivable}"
        )
        cad_pricelist_partners.write({"property_account_receivable_id": cad_receivable})
        usd_purchase_partners = self.env["res.partner"].search(
            [("property_purchase_currency_id", "=", usd_currency)]
        )
        _logger.info(
            f"Updating {len(usd_purchase_partners)} USD purchase partners with account {usd_payable}"
        )
        usd_purchase_partners.write({"property_account_payable_id": usd_payable})
        cad_purchase_partners = self.env["res.partner"].search(
            [("property_purchase_currency_id", "!=", usd_currency)]
        )
        _logger.info(
            f"Updating {len(cad_purchase_partners)} CAD purchase partners with account {cad_payable}"
        )
        cad_purchase_partners.write({"property_account_payable_id": cad_payable})

    def _import_ocrd_concurrent(self, cr):
        return self._import_concurrent(
            cr,
            self._get_sap_partners_ocrd,
            self._get_ocrd_partner_vals,
        )

    def _import_crd1_concurrent(self, cr):
        return self._import_concurrent(
            cr,
            self._get_sap_partners_crd1,
            self._get_crd1_partner_vals,
        )

    def _import_ocpr_concurrent(self, cr):
        return self._import_concurrent(
            cr,
            self._get_sap_partners_ocpr,
            self._get_ocpr_partner_vals,
        )

    @api.model
    def _get_payment_terms(self, terms_dict, sap_partner):
        """Returns a tuple of (payment_term_id, supplier_payment_term_id) with only
        one value set depending if cardtype matches a customer or vendor entry in SAP"""
        if sap_partner["cardtype"] in ["C", "L"]:
            return terms_dict.get(sap_partner["groupnum"]), False
        else:
            return False, terms_dict.get(sap_partner["groupnum"])

    @api.model
    def _get_ocrd_partner_vals(self, sap_partners):
        partner_vals = []
        countries_dict = self._get_countries_dict()
        states_dict = self._get_states_dict()
        users_dict = self._get_users_dict()
        terms_dict = self._get_payment_terms_dict()
        for sap_partner in sap_partners:
            # Start with the parent company
            country = sap_partner["country"]
            state = sap_partner["state1"]
            country, state = self._extract_sap_state_country(
                country, state, countries_dict, states_dict
            )
            street, street2 = self._extract_sap_street_street2(
                sap_partner["address"],
                sap_partner["block"],
            )
            user = users_dict.get(sap_partner["slpcode"], False)
            currency = self.env["res.currency"].search(
                [("name", "=", sap_partner["currency"])]
            )
            if not currency:
                currency = self.env.ref("base.CAD")
            property_payment_term_id, property_supplier_payment_term_id = (
                self._get_payment_terms(terms_dict, sap_partner)
            )
            picking_policy = "one" if sap_partner["partdelivr"] == "Y" else "direct"
            partner_vals.append(
                {
                    "sap_card_code": sap_partner["cardcode"],
                    "sap_atcentry": sap_partner["atcentry"],
                    "name": fix_quotes(sap_partner["cardname"]),
                    "street": street,
                    "street2": street2,
                    "city": sap_partner["city"] or "",
                    "country_id": country and country.id or False,
                    "state_id": state and state.id or False,
                    "zip": sap_partner["zipcode"],
                    "sap_parent_card": sap_partner["fathercard"] or False,
                    "sap_partner_type": sap_partner["cardtype"],
                    "phone": sap_partner["phone1"] or sap_partner["phone2"],
                    "email": sap_partner["e_mail"],
                    "is_company": True,
                    "company_id": self.env.company.id,
                    "comment": sap_partner["notes"],
                    "user_id": user,
                    "property_purchase_currency_id": currency.id,
                    "property_payment_term_id": property_payment_term_id,
                    "property_supplier_payment_term_id": property_supplier_payment_term_id,
                    "picking_policy": picking_policy,
                }
            )

            # Then the shipping address
            country = sap_partner["mailcountr"]
            state = sap_partner["state2"]
            users_dict = self._get_users_dict()
            country, state = self._extract_sap_state_country(
                country, state, countries_dict, states_dict
            )
            street, street2 = self._extract_sap_street_street2(
                sap_partner["mailaddres"],
                sap_partner["mailblock"],
            )
            user = users_dict.get(sap_partner["slpcode"], False)
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
                        "zip": sap_partner["zipcode"],
                        "sap_parent_card": sap_partner["cardcode"],
                        "is_company": False,
                        "phone": sap_partner["phone1"] or sap_partner["phone2"],
                        "email": sap_partner["e_mail"],
                        "type": "delivery",
                        "company_id": self.env.company.id,
                        "comment": sap_partner["notes"],
                        "user_id": user,
                    }
                )
        return partner_vals

    @api.model
    def import_partners(self, cr):
        _logger.info("Starting SAP partner import.")
        partners = self._import_ocrd(cr)
        partners |= self._import_crd1(cr)
        partners |= self._import_ocpr(cr)
        self.env.cr.commit()
        self._link_children_parents()

    @api.model
    def _get_state(self, states_dict, code, country_code=None):
        """Get the state associated with code and country_code. If no country_code is
        set, try first a Canadian province and, if not found, a US state."""
        if not code:
            return False
        if not country_code:
            return states_dict.get("CA").get(code, False) or states_dict.get("US").get(
                code, False
            )
        return states_dict.get(country_code).get(code, False)

    @api.model
    def _get_countries_dict(self):
        return {country.code: country for country in self.env["res.country"].search([])}

    @api.model
    def _get_states_dict(self):
        return {
            state.country_id.code: {state.code: state}
            for state in self.env["res.country.state"].search([])
        }

    @api.model
    def _extract_sap_state_country(self, country, state, country_dict, states_dict):
        odoo_country = country_dict.get(country)
        odoo_state = self._get_state(states_dict, state, country)
        return odoo_country, odoo_state

    @api.model
    def _get_basic_pricelists_dict(self):
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
        sap_partners = self._get_sap_partners_ocrd(cr)
        _logger.info(f"Importing {len(sap_partners)} companies.")
        partner_vals = self._get_ocrd_partner_vals(sap_partners)
        return self.env["res.partner"].create(partner_vals)

    def _import_concurrent(self, cr, sap_partners_func, vals_func):
        sap_partners = sap_partners_func(cr)
        chunk_size = min(500, len(sap_partners) // max_workers + 1)
        _logger.info(f"Importing {len(sap_partners)} partners.")
        chunks = [
            sap_partners[i : i + chunk_size]
            for i in range(0, len(sap_partners), chunk_size)
        ]
        partners = []
        spawn_method = multiprocessing.get_start_method()
        multiprocessing.set_start_method("fork", force=True)
        try:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        _create_partners_concurrent,
                        self.env.cr.dbname,
                        self.env.uid,
                        dict(self.env.context),
                        chunk,
                        vals_func.__name__,
                    )
                    for chunk in chunks
                ]

                for future in futures:
                    partners += future.result()
            return partners
        finally:
            multiprocessing.set_start_method(spawn_method, force=True)

    def _get_sap_partners_ocrd(self, cr):
        self.env.cr.execute(
            "SELECT distinct sap_card_code FROM res_partner WHERE sap_card_code is not null"
        )
        existing_cardcodes = tuple([row[0] for row in self.env.cr.fetchall()])
        if existing_cardcodes:
            sql = SQL(
                "SELECT * from OCRD WHERE cardname is not null and cardname <> ''"
                "AND cardcode not in %s",
                existing_cardcodes,
            )
        else:
            sql = SQL(
                "SELECT * from OCRD WHERE cardname is not null and cardname <> ''"
            )
        cr.execute(sql)
        sap_partners = cr.dictfetchall()
        return sap_partners

    @api.model
    def _import_crd1(self, cr):
        sap_addresses = self._get_sap_partners_crd1(cr)
        _logger.info(f"Importing {len(sap_addresses)} addresses.")
        partner_vals = self._get_crd1_partner_vals(sap_addresses)
        return self.env["res.partner"].create(partner_vals)

    @api.model
    def _get_sap_partners_crd1(self, cr):
        cr.execute(f"SELECT * FROM crd1")
        sap_addresses = cr.dictfetchall()
        return sap_addresses

    def _get_crd1_partner_vals(self, sap_addresses):
        partner_vals = []

        def _extract_name_street_street2(address, address2, address3, street, block):
            """Addresses in SAP have 4 possible lines that would match with street1
            and street2 from Odoo. Intelligently concatenate depending on which
            lines are set or not set."""
            address_parts = [
                part for part in [address, street, address2, address3, block] if part
            ]
            if len(address_parts) > 3:
                return address_parts[0], address_parts[1], ", ".join(address_parts[2:])
            else:
                address_parts += ["" for _ in range(3 - len(address_parts))]
                return tuple(address_parts)

        countries_dict = self._get_countries_dict()
        states_dict = self._get_states_dict()

        for sap_address in sap_addresses:
            name, street, street2 = _extract_name_street_street2(
                sap_address["address"],
                sap_address["address2"],
                sap_address["address3"],
                sap_address["street"],
                sap_address["block"],
            )
            parent_card = sap_address["cardcode"]
            country, state = self._extract_sap_state_country(
                sap_address["country"],
                sap_address["state"],
                countries_dict,
                states_dict,
            )
            zip_code = sap_address["zipcode"]
            address_type = "delivery" if sap_address["adrestype"] == "S" else "invoice"
            partner_vals.append(
                {
                    "name": fix_quotes(name),
                    "street": street,
                    "street2": street2,
                    "city": sap_address["city"],
                    "country_id": country and country.id or False,
                    "state_id": state and state.id or False,
                    "sap_parent_card": parent_card,
                    "type": address_type,
                    "is_company": False,
                    "user_id": False,
                    "zip": zip_code,
                }
            )
        return partner_vals

    @api.model
    def _link_children_parents(self):
        _logger.info("Linking children to parents.")
        self.env.flush_all()
        self.env.cr.execute(
            """
            -- First, let's create a CTE to match children with their parents based on SAP codes
            WITH parent_matches AS (
                SELECT 
                    child.id as child_id,
                    parent.id as parent_id
                FROM 
                    res_partner child
                    LEFT JOIN res_partner parent ON child.sap_parent_card = parent.sap_card_code
                WHERE 
                    child.sap_parent_card IS NOT NULL
                    AND parent.sap_card_code IS NOT NULL
                    AND child.id != parent.id  -- Prevent self-referencing
            )

            -- Now update the parent_id field
            UPDATE res_partner rp
            SET parent_id = pm.parent_id
            FROM parent_matches pm
            WHERE 
                rp.id = pm.child_id
                AND (rp.parent_id IS NULL OR rp.parent_id != pm.parent_id);  -- Only update if different
            """
        )
        self.env.cr.commit()

    def _import_ocpr(self, cr):
        """Import contacts"""
        sap_contacts = self._get_sap_partners_ocpr(cr)
        _logger.info(f"Importing {len(sap_contacts)} contacts.")
        partner_vals = self._get_ocpr_partner_vals(sap_contacts)
        return self.env["res.partner"].create(partner_vals)

    def _get_sap_partners_ocpr(self, cr):
        self.env.cr.execute(
            "SELECT sap_cntct_code FROM res_partner WHERE sap_cntct_code is not null"
        )
        existing_cntctcodes = tuple([row[0] for row in self.env.cr.fetchall()])
        if existing_cntctcodes:
            sql = SQL(
                "SELECT * from OCPR WHERE cntctcode is not null "
                "AND cntctcode not in %s",
                existing_cntctcodes,
            )
        else:
            sql = SQL("SELECT * from OCPR WHERE cntctcode is not null")
        cr.execute(sql)
        sap_contacts = cr.dictfetchall()
        return sap_contacts

    def _get_ocpr_partner_vals(self, sap_contacts):
        partner_vals = []
        for sap_contact in sap_contacts:
            partner_vals.append(
                {
                    "name": fix_quotes(sap_contact["name"]),
                    "sap_cntct_code": sap_contact["cntctcode"],
                    "sap_parent_card": sap_contact["cardcode"],
                    "is_company": False,
                    "email": sap_contact["e_maill"],
                    "phone": sap_contact["tel1"] or sap_contact["tel2"],
                    "mobile": sap_contact["cellolar"],
                    "active": sap_contact["active"] == "Y",
                    "function": sap_contact["position"] or sap_contact["title"],
                    "company_id": self.env.company.id,
                    "comment": sap_contact["notes1"] or sap_contact["notes2"] or "",
                    "type": "contact",
                }
            )
        return partner_vals

    def _delete_all(self):
        self.env.cr.execute(
            """
            DELETE FROM res_partner WHERE sap_card_code is not null 
            or sap_cntct_code is not null 
            or sap_parent_card is not null
            """
        )
