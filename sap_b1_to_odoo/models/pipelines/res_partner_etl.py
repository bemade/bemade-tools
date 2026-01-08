# TODO: add a fix_quotes here for contact names
import logging
from typing import Dict, List

from odoo import api, models
from odoo.tools import email_normalize
from odoo.tools.sql import SQL

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext
from odoo.addons.sap_b1_to_odoo.tools import fix_quotes

_logger = logging.getLogger(__name__)


##################################################################
# ETL Framework Helper Functions
##################################################################


def get_countries_dict(env):
    """Get a dictionary of country IDs by SAP code."""
    countries = env["res.country"].search([])
    return {country.code: country.id for country in countries}


def get_states_dict(env):
    """Get a dictionary of state IDs by SAP code."""
    states = env["res.country.state"].search([])
    return {state.code: state.id for state in states}


def get_users_dict(env):
    """Get a dictionary of Odoo user IDs with their SAP salesperson codes as keys."""
    return {
        user.sap_slpcode: user.id
        for user in env["res.users"].search(
            [
                ("sap_slpcode", "!=", False),
                ("active", "in", [False, True]),
            ]
        )
    }


def get_payment_terms_dict(env):
    """Get a dictionary of payment terms by SAP groupnum."""
    terms = env["account.payment.term"].search([])
    return {term.sap_groupnum: term.id for term in terms}


def extract_sap_state_country(country_code, state_code, countries_dict, states_dict):
    """Extract country and state IDs from SAP codes.

    Returns tuple of (country_id, state_id).
    """
    country_id = countries_dict.get(country_code, False)
    state_id = states_dict.get(state_code, False)
    return country_id, state_id


def extract_sap_street_street2(address, block):
    """Extract street and street2 from SAP address fields."""
    street = address or ""
    street2 = block or ""
    return street, street2


def get_payment_terms(terms_dict, sap_partner):
    """Get payment terms for a partner.

    Returns tuple of (payment_term_id, supplier_payment_term_id) based on partner type.
    Only one value is set depending if cardtype matches a customer or vendor entry in SAP.
    """
    groupnum = sap_partner.get("groupnum")

    # Customers (C, L) get payment term, vendors get supplier payment term
    if sap_partner.get("cardtype") in ["C", "L"]:
        return terms_dict.get(groupnum, False), False
    else:
        return False, terms_dict.get(groupnum, False)


##################################################################
# ETL Framework Pipelines
##################################################################


@ETL.pipeline(
    target_model="res.partner",
    importer_name="res.partner.company.importer",
    sap_source="ocrd",
    depends_on=[],
    allow_multiprocessing=False,  # Single-process for now due to write contention
)
class ResPartnerCompanyImporter(models.AbstractModel):
    _name = "res.partner.company.importer"
    _description = "SAP Partner Companies Importer (OCRD)"

    # Class-level cache for lookup dictionaries (shared across instances)
    _lookup_cache = {}

    @ETL.extract("ocrd")
    def extract_companies(self, ctx: ETLContext) -> List[Dict]:
        """Extract business partner companies from SAP OCRD table.

        Also pre-computes lookup dictionaries for use in transform phase.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of company dictionaries from SAP.
        """
        # Get existing partners to avoid duplicates
        ctx.env.cr.execute(
            "SELECT distinct sap_card_code FROM res_partner WHERE sap_card_code is not null"
        )
        existing_cardcodes = tuple(row[0] for row in ctx.env.cr.fetchall())

        # Query SAP
        sql = "SELECT * FROM ocrd"
        if existing_cardcodes:
            sql += " WHERE cardcode NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_cardcodes))
        else:
            ctx.cr.execute(sql)

        sap_partners = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(sap_partners)} companies from SAP OCRD.")

        # Pre-compute lookup dictionaries in main process (before multiprocessing)
        # Store in class-level cache so they're available to worker processes
        # IMPORTANT: Store IDs only, not recordsets (recordsets can't be pickled)
        _logger.info("Pre-computing lookup dictionaries for transform phase...")

        # Get countries as {code: id}
        countries = ctx.env["res.country"].search([])
        countries_dict = {country.code: country.id for country in countries}

        # Get states as {code: id}
        states = ctx.env["res.country.state"].search([])
        states_dict = {state.code: state.id for state in states}

        # Get users as {sap_code: id}
        users_dict = get_users_dict(ctx.env)

        # Get payment terms as {sap_groupnum: id}
        terms_dict = get_payment_terms_dict(ctx.env)

        # Get currencies as {name: id}
        currencies = ctx.env["res.currency"].search([])
        currencies_dict = {curr.name: curr.id for curr in currencies}
        company_currency_id = ctx.env.company.currency_id.id

        # Get company ID
        company_id = ctx.env.company.id

        ResPartnerCompanyImporter._lookup_cache = {
            "countries_dict": countries_dict,
            "states_dict": states_dict,
            "users_dict": users_dict,
            "terms_dict": terms_dict,
            "currencies_dict": currencies_dict,
            "company_currency_id": company_currency_id,
            "company_id": company_id,
        }
        _logger.info("Lookup dictionaries ready.")

        return sap_partners

    @ETL.transform()
    def transform_companies(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP companies into Odoo partner values.

        Uses pre-computed lookup dictionaries from extract phase.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of partner value dictionaries ready for creation.
        """
        sap_partners = extracted["extract_companies"]

        # Use pre-computed lookup dictionaries from class cache (no database queries in workers)
        cache = ResPartnerCompanyImporter._lookup_cache
        if not cache:
            raise RuntimeError("Cache is empty in transform! This should never happen.")

        countries_dict = cache["countries_dict"]
        states_dict = cache["states_dict"]
        users_dict = cache["users_dict"]
        terms_dict = cache["terms_dict"]
        company_id = cache["company_id"]

        partner_vals = []
        for i, sap_partner in enumerate(sap_partners):
            # Get name and skip if empty
            name = fix_quotes(sap_partner["cardname"])
            if not name or not name.strip():
                _logger.warning(
                    f"Skipping company with empty name: cardcode={sap_partner['cardcode']}"
                )
                continue

            # Extract location data (returns IDs)
            country_id, state_id = extract_sap_state_country(
                sap_partner["country"],
                sap_partner["state1"],
                countries_dict,
                states_dict,
            )
            street, street2 = extract_sap_street_street2(
                sap_partner["address"],
                sap_partner["block"],
            )

            # Get user ID and currency ID
            user_id = users_dict.get(sap_partner["slpcode"], False)

            # Get currency ID from cache
            currencies_dict = cache.get("currencies_dict", {})
            currency_id = currencies_dict.get(sap_partner["currency"])
            if not currency_id:
                currency_id = cache.get("company_currency_id")

            # Get payment terms
            property_payment_term_id, property_supplier_payment_term_id = (
                get_payment_terms(terms_dict, sap_partner)
            )

            # Other fields
            picking_policy = "one" if sap_partner["partdelivr"] == "Y" else "direct"
            email = fix_quotes(sap_partner["e_mail"])
            email = email_normalize(email)

            partner_vals.append(
                {
                    "sap_card_code": sap_partner["cardcode"],
                    "sap_atcentry": sap_partner["atcentry"],
                    "name": name,
                    "street": street,
                    "street2": street2,
                    "city": sap_partner["city"] or "",
                    "country_id": country_id or False,
                    "state_id": state_id or False,
                    "zip": sap_partner["zipcode"],
                    "sap_parent_card": sap_partner["fathercard"] or False,
                    "sap_partner_type": sap_partner["cardtype"],
                    "phone": sap_partner["phone1"] or sap_partner["phone2"],
                    "email": email,
                    "is_company": True,
                    "company_id": company_id,
                    "comment": sap_partner["notes"],
                    "user_id": user_id,
                    "property_purchase_currency_id": currency_id,
                    "property_payment_term_id": property_payment_term_id,
                    "property_supplier_payment_term_id": property_supplier_payment_term_id,
                    "picking_policy": picking_policy,
                }
            )

        _logger.info(f"Transformed {len(partner_vals)} company records.")
        return partner_vals

    @ETL.load()
    def load_companies(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load companies into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        partner_vals = transformed["transform_companies"]

        if partner_vals:
            partners = ctx.env["res.partner"].create(partner_vals)
            _logger.info(f"Created {len(partners)} company partners.")
        else:
            _logger.info("No new companies to create.")


@ETL.pipeline(
    target_model="res.partner",
    importer_name="res.partner.address.importer",
    sap_source="crd1",
    depends_on=["res.partner.company.importer"],
    allow_multiprocessing=False,  # Single-process for now due to write contention
)
class ResPartnerAddressImporter(models.AbstractModel):
    _name = "res.partner.address.importer"
    _description = "SAP Partner Addresses Importer (CRD1)"

    # Class-level cache for lookup dictionaries (shared across instances)
    _lookup_cache = {}

    @ETL.extract("crd1")
    def extract_addresses(self, ctx: ETLContext) -> List[Dict]:
        """Extract partner addresses from SAP CRD1 table.

        Also pre-computes lookup dictionaries for use in transform phase.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of address dictionaries from SAP.
        """
        # Get existing addresses to avoid duplicates
        # Addresses are uniquely identified by parent cardcode + linenum
        ctx.env.cr.execute(
            """
            SELECT DISTINCT sap_parent_card, sap_address_linenum
            FROM res_partner 
            WHERE sap_parent_card IS NOT NULL 
            AND sap_address_linenum IS NOT NULL
            """
        )
        existing_addresses = set((row[0], row[1]) for row in ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_addresses)} existing addresses.")

        # Query SAP
        ctx.cr.execute("SELECT * FROM crd1")
        all_addresses = ctx.cr.dictfetchall()

        # Filter out existing addresses based on cardcode + linenum
        sap_addresses = [
            addr
            for addr in all_addresses
            if (addr["cardcode"], addr["linenum"]) not in existing_addresses
        ]

        _logger.info(
            f"Extracted {len(sap_addresses)} new addresses from SAP CRD1 (filtered from {len(all_addresses)} total)."
        )

        # Pre-compute lookup dictionaries in main process (before multiprocessing)
        # Store in class-level cache so they're available to worker processes
        _logger.info("Pre-computing lookup dictionaries for transform phase...")
        ResPartnerAddressImporter._lookup_cache = {
            "countries_dict": get_countries_dict(ctx.env),
            "states_dict": get_states_dict(ctx.env),
        }
        _logger.info("Lookup dictionaries ready.")

        return sap_addresses

    @ETL.transform()
    def transform_addresses(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP addresses into Odoo partner values.

        Uses pre-computed lookup dictionaries from extract phase.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of partner value dictionaries ready for creation.
        """
        sap_addresses = extracted["extract_addresses"]

        # Use pre-computed lookup dictionaries from class cache (no database queries in workers)
        cache = ResPartnerAddressImporter._lookup_cache
        if not cache:
            _logger.error(
                "Cache is empty in transform! This will cause database queries in workers."
            )
            # Fallback to querying (will cause contention but better than crashing)
            cache = {
                "countries_dict": get_countries_dict(ctx.env),
                "states_dict": get_states_dict(ctx.env),
            }

        countries_dict = cache["countries_dict"]
        states_dict = cache["states_dict"]

        def extract_name_street_street2(address, address2, address3, street, block):
            """Intelligently concatenate address lines."""
            address_parts = [
                part for part in [address, street, address2, address3, block] if part
            ]
            if len(address_parts) > 3:
                return address_parts[0], address_parts[1], ", ".join(address_parts[2:])
            else:
                address_parts += ["" for _ in range(3 - len(address_parts))]
                return tuple(address_parts)

        partner_vals = []
        for sap_address in sap_addresses:
            name, street, street2 = extract_name_street_street2(
                sap_address["address"],
                sap_address["address2"],
                sap_address["address3"],
                sap_address["street"],
                sap_address["block"],
            )

            # Determine address type first
            address_type = "delivery" if sap_address["adrestype"] == "S" else "invoice"

            # Validate and fix name - use fallback if empty
            name = fix_quotes(name)
            if not name or not name.strip():
                # Use address type as fallback name
                name = f"{address_type.title()} Address"
                _logger.debug(
                    f"Using fallback name '{name}' for address: cardcode={sap_address['cardcode']}"
                )

            country_id, state_id = extract_sap_state_country(
                sap_address["country"],
                sap_address["state"],
                countries_dict,
                states_dict,
            )

            partner_vals.append(
                {
                    "name": name,
                    "street": street,
                    "street2": street2,
                    "city": sap_address["city"],
                    "country_id": country_id or False,
                    "state_id": state_id or False,
                    "sap_parent_card": sap_address["cardcode"],
                    "sap_address_linenum": sap_address["linenum"],
                    "type": address_type,
                    "is_company": False,
                    "user_id": False,
                    "zip": sap_address["zipcode"],
                }
            )

        _logger.info(f"Transformed {len(partner_vals)} address records.")
        return partner_vals

    @ETL.load()
    def load_addresses(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load addresses into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        partner_vals = transformed["transform_addresses"]

        if partner_vals:
            partners = ctx.env["res.partner"].create(partner_vals)
            _logger.info(f"Created {len(partners)} address partners.")
        else:
            _logger.info("No new addresses to create.")


@ETL.pipeline(
    target_model="res.partner",
    importer_name="res.partner.contact.importer",
    sap_source="ocpr",
    depends_on=["res.partner.company.importer"],
    allow_multiprocessing=False,  # Single-process for now due to write contention
)
class ResPartnerContactImporter(models.AbstractModel):
    _name = "res.partner.contact.importer"
    _description = "SAP Partner Contacts Importer (OCPR)"

    @ETL.extract("ocpr")
    def extract_contacts(self, ctx: ETLContext) -> List[Dict]:
        """Extract partner contacts from SAP OCPR table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of contact dictionaries from SAP.
        """
        # Get existing contacts to avoid duplicates
        ctx.env.cr.execute(
            "SELECT sap_cntct_code FROM res_partner WHERE sap_cntct_code is not null"
        )
        existing_cntct_codes = tuple(row[0] for row in ctx.env.cr.fetchall())

        # Query SAP
        sql = "SELECT * FROM ocpr"
        if existing_cntct_codes:
            sql += " WHERE cntctcode NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_cntct_codes))
        else:
            ctx.cr.execute(sql)

        sap_contacts = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(sap_contacts)} contacts from SAP OCPR.")
        return sap_contacts

    @ETL.transform()
    def transform_contacts(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP contacts into Odoo partner values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of partner value dictionaries ready for creation.
        """
        sap_contacts = extracted["extract_contacts"]

        partner_vals = []
        for sap_contact in sap_contacts:
            # Get contact details first
            email = fix_quotes(sap_contact["e_maill"])
            email = email_normalize(email) if email else False

            # In Odoo 19.0, mobile field was removed. Use phone field for mobile if no landline.
            phone = (
                sap_contact["tel1"] or sap_contact["tel2"] or sap_contact["cellolar"]
            )

            # Get name - use email or phone as fallback if empty
            name = fix_quotes(sap_contact["name"])
            if not name or not name.strip():
                if email:
                    name = email
                    _logger.debug(
                        f"Using email as name for contact: cntctcode={sap_contact['cntctcode']}"
                    )
                elif phone:
                    name = f"Contact {phone}"
                    _logger.debug(
                        f"Using phone as name for contact: cntctcode={sap_contact['cntctcode']}"
                    )
                else:
                    # No identifying information at all - skip this one
                    _logger.warning(
                        f"Skipping contact with no name, email, or phone: cntctcode={sap_contact['cntctcode']}"
                    )
                    continue

            partner_vals.append(
                {
                    "name": name,
                    "sap_cntct_code": sap_contact["cntctcode"],
                    "sap_parent_card": sap_contact["cardcode"],
                    "email": email,
                    "phone": phone,
                    "is_company": False,
                    "type": "contact",
                    "company_id": ctx.env.company.id,
                }
            )

        _logger.info(
            f"Transformed {len(partner_vals)} contact records (skipped contacts with empty names)."
        )
        return partner_vals

    @ETL.load()
    def load_contacts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load contacts into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        partner_vals = transformed["transform_contacts"]

        if partner_vals:
            partners = ctx.env["res.partner"].create(partner_vals)
            _logger.info(f"Created {len(partners)} contact partners.")
        else:
            _logger.info("No new contacts to create.")


@ETL.pipeline(
    target_model="res.partner",
    importer_name="res.partner.postprocess.importer",
    depends_on=[
        "res.partner.company.importer",
        "res.partner.address.importer",
        "res.partner.contact.importer",
    ],  # Runs after all partner imports
    allow_multiprocessing=False,
)
class ResPartnerPostProcessImporter(models.AbstractModel):
    _name = "res.partner.postprocess.importer"
    _description = "SAP Partner Post-Processing (Link Children to Parents)"

    @ETL.extract()
    def extract_nothing(self, ctx: ETLContext) -> List:
        """No extraction needed for post-processing.

        Args:
            ctx: ETL context.

        Returns:
            Empty list.
        """
        return []

    @ETL.transform()
    def transform_nothing(self, ctx: ETLContext, extracted: Dict) -> None:
        """No transformation needed for post-processing.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data (empty).

        Returns:
            None.
        """
        return None

    @ETL.load()
    def load_link_children_to_parents(self, ctx: ETLContext, transformed: Dict) -> None:
        """Link children (addresses/contacts) to their parent companies.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data (None).
        """
        _logger.info("Linking children to parents.")
        ctx.env.flush_all()

        # Link children to parents based on SAP codes
        ctx.env.cr.execute(
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
            SET parent_id = pm.parent_id, commercial_partner_id = pm.parent_id
            FROM parent_matches pm
            WHERE rp.id = pm.child_id
                AND (rp.parent_id IS NULL OR rp.parent_id != pm.parent_id OR rp.commercial_partner_id != pm.parent_id);  -- Only update if different
            """
        )

        # Fill in a partner's address if there isn't already information in the fields
        sql = """
        WITH parent_matches AS (
                SELECT
                    child.id as child_id,
                    parent.id as parent_id,
                    parent.street as street,
                    parent.street2 as street2,
                    parent.city as city,
                    parent.country_id as country_id,
                    parent.state_id as state_id,
                    parent.zip as zip
                FROM
                    res_partner child
                    INNER JOIN res_partner parent ON child.parent_id = parent.id
                WHERE
                    parent.sap_card_code IS NOT NULL
            )
            UPDATE res_partner rp
            SET %(col)s = pm.%(col)s
            FROM parent_matches pm
            WHERE rp.id = pm.child_id
                AND rp.parent_id IS NOT NULL
                AND (rp.%(col)s IS NULL)
                AND (rp.street IS NULL OR rp.street = pm.street)
                AND (rp.street2 IS NULL OR rp.street2 = pm.street2)
                AND (rp.city IS NULL OR rp.city = pm.city)
                AND (rp.country_id IS NULL OR rp.country_id = pm.country_id)
                AND (rp.state_id IS NULL OR rp.state_id = pm.state_id)
                AND (rp.zip IS NULL OR rp.zip = pm.zip)
        """
        for col in ["street", "street2", "city", "country_id", "state_id", "zip"]:
            ctx.env.cr.execute(sql % {"col": col})

        _logger.info("Completed linking children to parents.")
