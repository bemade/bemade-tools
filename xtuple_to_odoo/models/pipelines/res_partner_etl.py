"""xTuple Partner ETL Pipelines

This module handles the migration of partner data (customers, vendors, contacts,
and ship-to addresses) from xTuple to Odoo using the ETL framework.

Pipeline execution order:
1. xtuple.partner.customer.importer - Import customers as companies
2. xtuple.partner.vendor.importer - Import vendors as companies
3. xtuple.partner.standalone.importer - Import standalone CRM accounts
4. xtuple.partner.contact.importer - Import contacts
5. xtuple.partner.shipto.importer - Import ship-to addresses
6. xtuple.partner.postprocessor - Link parents and set ranks
"""

import logging
from typing import Any, Dict, List

from odoo import api, models
from odoo.tools import email_normalize
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.xtuple_to_odoo.tools import normalize_country_code

_logger = logging.getLogger(__name__)

# Common SQL query parts
CONTACT_ADDRESS_SELECT = """
    cntct_first_name,
    cntct_last_name,
    cntct_honorific,
    cntct_initials,
    cntct_phone,
    cntct_phone2,
    cntct_fax,
    cntct_email,
    cntct_webaddr,
    cntct_notes,
    cntct_active,
    addr_line1,
    addr_line2,
    addr_line3,
    addr_city,
    addr_state,
    addr_postalcode,
    addr_country,
    addr_notes
"""

CUSTOMER_SELECT = f"""
    cust_id,
    cust_number,
    cust_name,
    cust_active,
    cntct_id as cust_cntct_id,
    crmacct_id,
    {CONTACT_ADDRESS_SELECT}
"""

VENDOR_SELECT = f"""
    vend_id,
    vend_number,
    vend_name,
    vend_active,
    cntct_id as vend_cntct_id,
    crmacct_id,
    {CONTACT_ADDRESS_SELECT}
"""

CONTACT_SELECT = f"""
    cntct_id,
    cntct_crmacct_id,
    {CONTACT_ADDRESS_SELECT}
"""

SHIPTO_SELECT = f"""
    shipto_id,
    shipto_name,
    shipto_cust_id,
    shipto_active,
    cntct_id as shipto_cntct_id,
    {CONTACT_ADDRESS_SELECT}
"""


# =============================================================================
# Helper Mixin for Partner Import
# =============================================================================


class XtuplePartnerImportMixin(models.AbstractModel):
    """Mixin providing common partner import utilities."""

    _name = "xtuple.partner.import.mixin"
    _description = "xTuple Partner Import Mixin"

    @api.model
    def _get_countries_dict(self):
        return {country.code: country for country in self.env["res.country"].search([])}

    @api.model
    def _get_states_dict(self):
        states = self.env["res.country.state"].search([])
        result = {}
        for state in states:
            if state.country_id.code not in result:
                result[state.country_id.code] = {}
            result[state.country_id.code][state.code] = state
        return result

    @api.model
    def _extract_state_country(
        self, country_code, state_code, country_dict, states_dict
    ):
        """Extract country and state from xTuple country and state codes."""
        odoo_country = country_dict.get(country_code) if country_code else False

        if not odoo_country and country_code:
            _logger.warning(
                f"Country code '{country_code}' not found in Odoo. Using US as fallback."
            )
            odoo_country = country_dict.get("US")

        odoo_state = None
        if state_code and odoo_country:
            odoo_state = states_dict.get(odoo_country.code, {}).get(state_code)
            if not odoo_state:
                _logger.warning(
                    f"State code '{state_code}' not found for country '{odoo_country.code}'"
                )

        return odoo_country, odoo_state

    @api.model
    def _extract_street_street2(self, address1, address2, address3):
        """Extract street and street2 from xTuple address fields."""
        street = address1 or ""
        street2_parts = [p for p in [address2, address3] if p]
        street2 = ", ".join(street2_parts) if street2_parts else ""
        return street, street2

    @api.model
    def _extract_address_info(self, partner_data, countries_dict, states_dict) -> dict:
        """Extract address information from xTuple partner data."""
        country_code = normalize_country_code(partner_data.get("addr_country"))
        state_code = partner_data.get("addr_state")
        country, state = self._extract_state_country(
            country_code, state_code, countries_dict, states_dict
        )

        street, street2 = self._extract_street_street2(
            partner_data.get("addr_line1"),
            partner_data.get("addr_line2"),
            partner_data.get("addr_line3"),
        )

        return {
            "country": country,
            "state": state,
            "street": street,
            "street2": street2,
            "city": partner_data.get("addr_city"),
            "zip": partner_data.get("addr_postalcode"),
        }

    @api.model
    def _build_partner_vals(
        self,
        partner_data: dict,
        address_info: dict,
        company_id: int,
        is_company: bool = True,
        active_field: str = "",
    ) -> dict:
        """Build common partner values dictionary.

        Args:
            partner_data: Raw xTuple partner data dict
            address_info: Address info from _extract_address_info()
            company_id: Odoo company ID
            is_company: Whether this is a company partner
            active_field: Field name for active status (e.g., 'cust_active', 'vend_active')

        Returns:
            Dictionary with common partner field values
        """
        phone = partner_data.get("cntct_phone") or partner_data.get("cntct_phone2")
        email = email_normalize(partner_data.get("cntct_email", ""))

        vals = {
            "street": address_info["street"],
            "street2": address_info["street2"],
            "city": address_info["city"],
            "state_id": address_info["state"].id if address_info["state"] else False,
            "country_id": (
                address_info["country"].id if address_info["country"] else False
            ),
            "zip": address_info["zip"],
            "phone": phone,
            "email": email,
            "is_company": is_company,
            "company_id": company_id,
        }

        if active_field:
            vals["active"] = partner_data.get(active_field)

        return vals

    @api.model
    def _get_contact_name(self, partner_data: dict) -> str:
        """Build contact name from first/last name fields."""
        first_name = partner_data.get("cntct_first_name", "") or ""
        last_name = partner_data.get("cntct_last_name", "") or ""
        return f"{first_name} {last_name}".strip()


# =============================================================================
# Customer Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="res.partner",
    importer_name="xtuple.partner.customer.importer",
    sap_source="custinfo",
    depends_on=[],
    allow_multiprocessing=True,
    multiprocessing_threshold=500,
)
class XtuplePartnerCustomerImporter(models.AbstractModel):
    _name = "xtuple.partner.customer.importer"
    _description = "xTuple Customer Importer"
    _inherit = "xtuple.partner.import.mixin"

    @ETL.extract("custinfo")
    def extract_customers(self, ctx: ETLContext) -> List[Dict]:
        """Extract customers from xTuple custinfo table."""
        ctx.env.cr.execute(
            "SELECT xtuple_cust_id FROM res_partner WHERE xtuple_cust_id IS NOT NULL"
        )
        existing_cust_ids = tuple([row[0] for row in ctx.env.cr.fetchall()])
        _logger.info(f"Found {len(existing_cust_ids)} existing customers in Odoo")

        # Get existing partner names for deduplication (cross-system matching)
        ctx.env.cr.execute(
            "SELECT LOWER(name) FROM res_partner WHERE name IS NOT NULL AND name != ''"
        )
        existing_names = {row[0] for row in ctx.env.cr.fetchall()}
        _logger.info(
            f"Found {len(existing_names)} existing partners by name for deduplication"
        )

        select_clause = f"""
        SELECT
            {CUSTOMER_SELECT},
            crmacct_parent_id,
            crmacct.crmacct_id
        FROM custinfo
        LEFT JOIN cntct ON (cust_cntct_id = cntct_id)
        LEFT JOIN addr ON (cntct_addr_id = addr_id)
        LEFT JOIN crmacct ON (crmacct_cust_id = cust_id)
        """

        if existing_cust_ids:
            sql = SQL(select_clause + "WHERE cust_id NOT IN %s", existing_cust_ids)
        else:
            sql = SQL(select_clause)

        ctx.cr.execute(sql)
        customers = ctx.cr.dictfetchall()

        # Filter out customers that match existing partners by name (deduplication)
        deduped_customers = [
            c
            for c in customers
            if not c.get("cust_name")
            or c.get("cust_name", "").lower() not in existing_names
        ]

        _logger.info(
            f"Extracted {len(customers)} customers from xTuple, "
            f"{len(deduped_customers)} after deduplication by name"
        )
        return deduped_customers

    @ETL.transform()
    def transform_customers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple customers into Odoo partner values."""
        customers = extracted.get("extract_customers", [])
        countries_dict = self._get_countries_dict()
        states_dict = self._get_states_dict()
        company_id = ctx.env.company.id

        partner_vals = []
        for customer in customers:
            try:
                address_info = self._extract_address_info(
                    customer, countries_dict, states_dict
                )

                vals = self._build_partner_vals(
                    customer,
                    address_info,
                    company_id,
                    is_company=True,
                    active_field="cust_active",
                )
                vals.update(
                    {
                        "ref": customer.get("cust_number"),
                        "name": customer.get("cust_name", ""),
                        "xtuple_cust_id": customer.get("cust_id"),
                        "xtuple_crmacct_id": customer.get("crmacct_id"),
                        "xtuple_partner_type": "customer",
                        "customer_rank": 1,
                    }
                )
                partner_vals.append(vals)
            except Exception as e:
                _logger.error(
                    f"Error transforming customer {customer.get('cust_id')}: {str(e)}"
                )

        _logger.info(f"Transformed {len(partner_vals)} customer records")
        return partner_vals

    @ETL.load()
    def load_customers(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load customers into Odoo."""
        partner_vals = transformed.get("transform_customers", [])
        if partner_vals:
            partners = ctx.env["res.partner"].create(partner_vals)
            _logger.info(f"Created {len(partners)} customer partners")
        else:
            _logger.info("No new customers to create")


# =============================================================================
# Vendor Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="res.partner",
    importer_name="xtuple.partner.vendor.importer",
    sap_source="vendinfo",
    depends_on=["xtuple.partner.customer.importer"],
    allow_multiprocessing=True,
    multiprocessing_threshold=500,
)
class XtuplePartnerVendorImporter(models.AbstractModel):
    _name = "xtuple.partner.vendor.importer"
    _description = "xTuple Vendor Importer"
    _inherit = "xtuple.partner.import.mixin"

    @ETL.extract("vendinfo")
    def extract_vendors(self, ctx: ETLContext) -> List[Dict]:
        """Extract vendors from xTuple vendinfo table."""
        ctx.env.cr.execute(
            "SELECT xtuple_vend_id FROM res_partner WHERE xtuple_vend_id IS NOT NULL"
        )
        existing_vend_ids = tuple([row[0] for row in ctx.env.cr.fetchall()])
        _logger.info(f"Found {len(existing_vend_ids)} existing vendors in Odoo")

        # Get existing partner names for deduplication (cross-system matching)
        ctx.env.cr.execute(
            "SELECT LOWER(name) FROM res_partner WHERE name IS NOT NULL AND name != ''"
        )
        existing_names = {row[0] for row in ctx.env.cr.fetchall()}
        _logger.info(
            f"Found {len(existing_names)} existing partners by name for deduplication"
        )

        select_clause = f"""
        SELECT
            {VENDOR_SELECT},
            crmacct_parent_id,
            crmacct.crmacct_id as vend_crmacct_id
        FROM vendinfo
        LEFT JOIN cntct ON (vend_cntct1_id = cntct_id)
        LEFT JOIN addr ON (vend_addr_id = addr_id)
        LEFT JOIN crmacct ON (crmacct_vend_id = vend_id)
        """

        if existing_vend_ids:
            sql = SQL(select_clause + "WHERE vend_id NOT IN %s", existing_vend_ids)
        else:
            sql = SQL(select_clause)

        ctx.cr.execute(sql)
        vendors = ctx.cr.dictfetchall()

        # Filter out vendors that match existing partners by name (deduplication)
        deduped_vendors = [
            v
            for v in vendors
            if not v.get("vend_name")
            or v.get("vend_name", "").lower() not in existing_names
        ]

        _logger.info(
            f"Extracted {len(vendors)} vendors from xTuple, "
            f"{len(deduped_vendors)} after deduplication by name"
        )
        return deduped_vendors

    @ETL.transform()
    def transform_vendors(self, ctx: ETLContext, extracted: Dict) -> Dict[str, Any]:
        """Transform xTuple vendors into Odoo partner values."""
        vendors = extracted.get("extract_vendors", [])
        countries_dict = self._get_countries_dict()
        states_dict = self._get_states_dict()
        company_id = ctx.env.company.id

        vendor_ids_to_update = []
        partner_vals = []

        for vendor in vendors:
            # Check if this vendor is also a customer
            ctx.env.cr.execute(
                "SELECT id FROM res_partner WHERE xtuple_cust_id = %s",
                (vendor.get("vend_id"),),
            )
            existing_customer = ctx.env.cr.fetchone()

            if existing_customer:
                vendor_ids_to_update.append(
                    (existing_customer[0], int(vendor.get("vend_id")))
                )
                continue

            address_info = self._extract_address_info(
                vendor, countries_dict, states_dict
            )

            name = vendor.get("vend_name", "")
            if not name:
                name = self._get_contact_name(vendor)

            vals = self._build_partner_vals(
                vendor,
                address_info,
                company_id,
                is_company=True,
                active_field="vend_active",
            )
            vals.update(
                {
                    "ref": vendor.get("vend_number"),
                    "name": name,
                    "xtuple_vend_id": int(vendor.get("vend_id")),
                    "xtuple_crmacct_id": vendor.get("vend_crmacct_id"),
                    "xtuple_partner_type": "vendor",
                    "supplier_rank": 1,
                }
            )
            partner_vals.append(vals)

        _logger.info(f"Transformed {len(partner_vals)} vendor records")
        return {
            "partner_vals": partner_vals,
            "vendor_ids_to_update": vendor_ids_to_update,
        }

    @ETL.load()
    def load_vendors(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load vendors into Odoo."""
        data = transformed.get("transform_vendors", {})
        partner_vals = data.get("partner_vals", [])
        vendor_ids_to_update = data.get("vendor_ids_to_update", [])

        # Update existing customers to be both customer and vendor
        for partner_id, vend_id in vendor_ids_to_update:
            ctx.env.cr.execute(
                """
                UPDATE res_partner
                SET xtuple_vend_id = %s, xtuple_partner_type = 'both', supplier_rank = 1
                WHERE id = %s
                """,
                (vend_id, partner_id),
            )
        if vendor_ids_to_update:
            _logger.info(
                f"Updated {len(vendor_ids_to_update)} existing customers to be both customer and vendor"
            )

        # Create new vendor partners
        if partner_vals:
            partners = ctx.env["res.partner"].create(partner_vals)
            _logger.info(f"Created {len(partners)} vendor partners")
        else:
            _logger.info("No new vendors to create")


# =============================================================================
# Standalone CRM Account Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="res.partner",
    importer_name="xtuple.partner.standalone.importer",
    sap_source="crmacct",
    depends_on=["xtuple.partner.vendor.importer"],
)
class XtuplePartnerStandaloneImporter(models.AbstractModel):
    _name = "xtuple.partner.standalone.importer"
    _description = "xTuple Standalone CRM Account Importer"

    @ETL.extract("crmacct")
    def extract_standalone_accounts(self, ctx: ETLContext) -> List[Dict]:
        """Extract standalone crmacct records not linked to customers or vendors."""
        ctx.env.cr.execute(
            "SELECT xtuple_crmacct_id FROM res_partner WHERE xtuple_crmacct_id IS NOT NULL"
        )
        existing_crmacct_ids = tuple([row[0] for row in ctx.env.cr.fetchall()])
        _logger.info(
            f"Found {len(existing_crmacct_ids)} existing crmacct records in Odoo"
        )

        select_clause = """
        SELECT
            crmacct_id,
            crmacct_number,
            crmacct_name,
            crmacct_active,
            crmacct_type,
            crmacct_notes,
            crmacct_parent_id
        FROM crmacct
        WHERE crmacct_active
        AND crmacct_cust_id IS NULL
        AND crmacct_vend_id IS NULL
        AND crmacct_type = 'O'
        """

        if existing_crmacct_ids:
            sql = SQL(select_clause + "AND crmacct_id NOT IN %s", existing_crmacct_ids)
        else:
            sql = SQL(select_clause)

        ctx.cr.execute(sql)
        accounts = ctx.cr.dictfetchall()

        _logger.info(f"Extracted {len(accounts)} standalone CRM accounts from xTuple")
        return accounts

    @ETL.transform()
    def transform_standalone_accounts(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform standalone CRM accounts into Odoo partner values."""
        accounts = extracted.get("extract_standalone_accounts", [])
        company = ctx.env.company

        partner_vals = []
        for crmacct in accounts:
            name = crmacct.get("crmacct_name", "")
            if not name:
                name = crmacct.get("crmacct_number", "Unknown CRM Account")

            crmacct_id = crmacct.get("crmacct_id")

            partner_vals.append(
                {
                    "xtuple_crmacct_id": crmacct_id,
                    "ref": crmacct.get("crmacct_number"),
                    "name": name,
                    "is_company": True,
                    "company_id": company.id,
                    "active": crmacct.get("crmacct_active"),
                    "comment": crmacct.get("crmacct_notes"),
                }
            )

        _logger.info(f"Transformed {len(partner_vals)} standalone CRM account records")
        return partner_vals

    @ETL.load()
    def load_standalone_accounts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load standalone CRM accounts into Odoo."""
        partner_vals = transformed.get("transform_standalone_accounts", [])
        if partner_vals:
            partners = ctx.env["res.partner"].create(partner_vals)
            _logger.info(f"Created {len(partners)} standalone CRM account partners")
        else:
            _logger.info("No new standalone CRM accounts to create")


# =============================================================================
# Contact Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="res.partner",
    importer_name="xtuple.partner.contact.importer",
    sap_source="cntct",
    depends_on=["xtuple.partner.standalone.importer"],
    allow_multiprocessing=True,
    multiprocessing_threshold=500,
)
class XtuplePartnerContactImporter(models.AbstractModel):
    _name = "xtuple.partner.contact.importer"
    _description = "xTuple Contact Importer"
    _inherit = "xtuple.partner.import.mixin"

    @ETL.extract("cntct")
    def extract_contacts(self, ctx: ETLContext) -> List[Dict]:
        """Extract contacts from xTuple cntct table."""
        ctx.env.cr.execute(
            "SELECT xtuple_cntct_id FROM res_partner WHERE xtuple_cntct_id IS NOT NULL"
        )
        existing_cntct_ids = tuple([row[0] for row in ctx.env.cr.fetchall()])
        _logger.info(f"Found {len(existing_cntct_ids)} existing contacts in Odoo")

        select_clause = f"""
        SELECT
            {CONTACT_SELECT},
            cntct_crmacct_id
        FROM cntct
        LEFT JOIN addr ON (cntct_addr_id = addr_id)
        WHERE (cntct_first_name IS NOT NULL OR cntct_last_name IS NOT NULL)
        """

        if existing_cntct_ids:
            sql = SQL(select_clause + "AND cntct_id NOT IN %s", existing_cntct_ids)
        else:
            sql = SQL(select_clause)

        ctx.cr.execute(sql)
        contacts = ctx.cr.dictfetchall()

        _logger.info(f"Extracted {len(contacts)} new contacts from xTuple")
        return contacts

    @ETL.transform()
    def transform_contacts(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple contacts into Odoo partner values."""
        contacts = extracted.get("extract_contacts", [])
        countries_dict = self._get_countries_dict()
        states_dict = self._get_states_dict()
        company_id = ctx.env.company.id

        partner_vals = []
        for contact in contacts:
            address_info = self._extract_address_info(
                contact, countries_dict, states_dict
            )

            vals = self._build_partner_vals(
                contact,
                address_info,
                company_id,
                is_company=False,
                active_field="cntct_active",
            )
            vals.update(
                {
                    "name": self._get_contact_name(contact),
                    "xtuple_cntct_id": int(contact.get("cntct_id")),
                    "xtuple_parent_id": contact.get("cntct_crmacct_id"),
                    "function": contact.get("cntct_title"),
                    "comment": contact.get("cntct_notes"),
                    "type": "contact",
                }
            )
            partner_vals.append(vals)

        _logger.info(f"Transformed {len(partner_vals)} contact records")
        return partner_vals

    @ETL.load()
    def load_contacts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load contacts into Odoo."""
        partner_vals = transformed.get("transform_contacts", [])
        if partner_vals:
            partners = ctx.env["res.partner"].create(partner_vals)
            _logger.info(f"Created {len(partners)} contact partners")
        else:
            _logger.info("No new contacts to create")


# =============================================================================
# Ship-To Address Importer Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="res.partner",
    importer_name="xtuple.partner.shipto.importer",
    sap_source="shiptoinfo",
    depends_on=["xtuple.partner.contact.importer"],
    allow_multiprocessing=True,
    multiprocessing_threshold=500,
)
class XtuplePartnerShiptoImporter(models.AbstractModel):
    _name = "xtuple.partner.shipto.importer"
    _description = "xTuple Ship-To Address Importer"
    _inherit = "xtuple.partner.import.mixin"

    @ETL.extract("shiptoinfo")
    def extract_shiptos(self, ctx: ETLContext) -> Dict[str, Any]:
        """Extract ship-to addresses from xTuple shiptoinfo table."""
        ctx.env.cr.execute(
            "SELECT xtuple_shipto_id FROM res_partner WHERE xtuple_shipto_id IS NOT NULL"
        )
        existing_shipto_ids = tuple([row[0] for row in ctx.env.cr.fetchall()])
        _logger.info(
            f"Found {len(existing_shipto_ids)} existing ship-to addresses in Odoo"
        )

        select_clause = f"""
        SELECT
            {SHIPTO_SELECT}
        FROM shiptoinfo
        LEFT JOIN cntct ON (shipto_cntct_id = cntct_id)
        LEFT JOIN addr ON (shipto_addr_id = addr_id)
        """

        if existing_shipto_ids:
            sql = SQL(select_clause + "WHERE shipto_id NOT IN %s", existing_shipto_ids)
        else:
            sql = SQL(select_clause)

        ctx.cr.execute(sql)
        shiptos = ctx.cr.dictfetchall()

        # Get customer mapping for parent lookup
        ctx.env.cr.execute(
            "SELECT xtuple_cust_id, id FROM res_partner WHERE xtuple_cust_id IS NOT NULL"
        )
        customer_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        _logger.info(f"Extracted {len(shiptos)} new ship-to addresses from xTuple")
        return {"shiptos": shiptos, "customer_map": customer_map}

    @ETL.transform()
    def transform_shiptos(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform xTuple ship-to addresses into Odoo partner values."""
        data = extracted.get("extract_shiptos", {})
        shiptos = data.get("shiptos", [])
        customer_map = data.get("customer_map", {})
        countries_dict = self._get_countries_dict()
        states_dict = self._get_states_dict()
        company_id = ctx.env.company.id

        partner_vals = []
        for address in shiptos:
            parent_id = customer_map.get(address.get("shipto_cust_id"))
            if not parent_id:
                _logger.warning(
                    f"Parent customer not found for ship-to {address.get('shipto_id')}"
                )
                continue

            address_info = self._extract_address_info(
                address, countries_dict, states_dict
            )

            name = address.get("shipto_name", "")
            if not name:
                name = self._get_contact_name(address)
            if not name:
                ctx.env.cr.execute(
                    "SELECT name FROM res_partner WHERE id = %s", (parent_id,)
                )
                parent_name = ctx.env.cr.fetchone()[0]
                name = f"{parent_name} - Shipping Address"

            vals = self._build_partner_vals(
                address,
                address_info,
                company_id,
                is_company=False,
                active_field="shipto_active",
            )
            vals.update(
                {
                    "parent_id": parent_id,
                    "name": name,
                    "type": "delivery",
                    "xtuple_shipto_id": address.get("shipto_id"),
                }
            )
            partner_vals.append(vals)

        _logger.info(f"Transformed {len(partner_vals)} ship-to address records")
        return partner_vals

    @ETL.load()
    def load_shiptos(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load ship-to addresses into Odoo."""
        partner_vals = transformed.get("transform_shiptos", [])
        if partner_vals:
            partners = ctx.env["res.partner"].create(partner_vals)
            _logger.info(f"Created {len(partners)} ship-to address partners")
        else:
            _logger.info("No new ship-to addresses to create")


# =============================================================================
# Partner Postprocessor Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="res.partner",
    importer_name="xtuple.partner.postprocessor",
    sap_source="",
    depends_on=["xtuple.partner.shipto.importer"],
)
class XtuplePartnerPostprocessor(models.AbstractModel):
    _name = "xtuple.partner.postprocessor"
    _description = "xTuple Partner Postprocessor"

    @ETL.extract("")
    def extract_nothing(self, ctx: ETLContext) -> Dict:
        """No extraction needed for postprocessing."""
        return {}

    @ETL.transform()
    def transform_nothing(self, ctx: ETLContext, extracted: Dict) -> Dict:
        """No transformation needed for postprocessing."""
        return {}

    @ETL.load()
    def postprocess_partners(self, ctx: ETLContext, transformed: Dict) -> None:
        """Link children to parents and set partner ranks."""
        _logger.info("Running partner postprocessing...")

        self._link_children_parents(ctx)
        self._set_partner_ranks(ctx)

        ctx.env["res.partner"].flush_model()
        _logger.info("Partner postprocessing complete")

    def _link_children_parents(self, ctx: ETLContext) -> None:
        """Link contacts to their parent companies based on crmacct relationships."""
        _logger.info("Linking children to parents based on crmacct relationships.")
        ctx.env.flush_all()

        try:
            ctx.env.cr.execute(
                """
                UPDATE res_partner contact
                SET
                    parent_id = company.id,
                    commercial_partner_id = company.id
                FROM res_partner company
                WHERE
                    contact.xtuple_cntct_id IS NOT NULL
                    AND contact.xtuple_parent_id IS NOT NULL
                    AND company.xtuple_crmacct_id IS NOT NULL
                    AND company.is_company = TRUE
                    AND contact.xtuple_parent_id = company.xtuple_crmacct_id
                    AND contact.id != company.id
                    AND (contact.parent_id IS NULL OR contact.parent_id != company.id)
                RETURNING contact.id, contact.name, company.id
                """
            )

            linked_contacts = ctx.env.cr.fetchall()
            _logger.info(
                f"Linked {len(linked_contacts)} contacts to their parent companies."
            )

            # Fill in child address from parent if empty
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
                        (parent.xtuple_cust_id IS NOT NULL OR parent.xtuple_vend_id IS NOT NULL)
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

            _logger.info("Successfully linked children to parents")
        except Exception as e:
            _logger.error(f"Error in _link_children_parents: {str(e)}")
            raise

    def _set_partner_ranks(self, ctx: ETLContext) -> None:
        """Set customer and supplier ranks based on partner type."""
        ctx.env.cr.execute(
            """
            UPDATE res_partner
            SET customer_rank = 1
            WHERE xtuple_partner_type IN ('customer', 'both')
            """
        )

        ctx.env.cr.execute(
            """
            UPDATE res_partner
            SET supplier_rank = 1
            WHERE xtuple_partner_type IN ('vendor', 'both')
            """
        )
        _logger.info("Set partner ranks based on xTuple partner type")


# =============================================================================
# Partner Linker Pipelines (for deduplication)
# =============================================================================


@ETL.pipeline(
    target_model="res.partner",
    importer_name="xtuple.partner.customer.linker",
    sap_source="custinfo",
    depends_on=["xtuple.partner.customer.importer"],
)
class XtuplePartnerCustomerLinker(models.AbstractModel):
    """ETL Pipeline for linking existing partners to xTuple customers by name."""

    _name = "xtuple.partner.customer.linker"
    _description = "xTuple Customer Linker"

    @ETL.extract("custinfo")
    def extract_customers_for_linking(self, ctx: ETLContext) -> List[Dict]:
        """Extract customers from xTuple that need linking."""
        select_clause = """
        SELECT
            cust_id,
            cust_name
        FROM custinfo
        WHERE cust_name IS NOT NULL AND cust_name != ''
        """
        ctx.cr.execute(select_clause)
        customers = ctx.cr.dictfetchall()

        _logger.info(f"Extracted {len(customers)} customers for linking")
        return customers

    @ETL.transform()
    def transform_customers_for_linking(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Find existing partners by name and prepare link updates."""
        customers = extracted.get("extract_customers_for_linking", [])

        # Build lookup of existing partners by name that don't have xtuple_cust_id
        ctx.env.cr.execute(
            """
            SELECT id, LOWER(name) FROM res_partner
            WHERE name IS NOT NULL AND name != ''
            AND xtuple_cust_id IS NULL
            """
        )
        partner_by_name = {row[1]: row[0] for row in ctx.env.cr.fetchall()}

        # Get customer IDs already assigned to partners (to avoid duplicates)
        ctx.env.cr.execute(
            "SELECT xtuple_cust_id FROM res_partner WHERE xtuple_cust_id IS NOT NULL"
        )
        existing_cust_ids = {row[0] for row in ctx.env.cr.fetchall()}

        link_updates = []
        for customer in customers:
            cust_id = customer.get("cust_id")
            # Skip if this customer ID is already assigned to another partner
            if cust_id in existing_cust_ids:
                continue
            name = customer.get("cust_name", "")
            if name and name.lower() in partner_by_name:
                link_updates.append(
                    {
                        "partner_id": partner_by_name[name.lower()],
                        "xtuple_cust_id": cust_id,
                        "name": name,
                    }
                )

        _logger.info(f"Found {len(link_updates)} partners to link as customers by name")
        return link_updates

    @ETL.load()
    def load_customer_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing partners with xTuple customer IDs."""
        link_updates = transformed.get("transform_customers_for_linking", [])

        if not link_updates:
            _logger.info("No partners to link as customers")
            return

        for update in link_updates:
            ctx.env.cr.execute(
                """
                UPDATE res_partner
                SET xtuple_cust_id = %s, xtuple_partner_type = COALESCE(
                    CASE
                        WHEN xtuple_partner_type = 'vendor' THEN 'both'
                        ELSE 'customer'
                    END,
                    'customer'
                ), customer_rank = GREATEST(customer_rank, 1)
                WHERE id = %s
                """,
                (update["xtuple_cust_id"], update["partner_id"]),
            )
            _logger.debug(
                f"Linked partner {update['partner_id']} (name={update['name']}) "
                f"to xTuple customer {update['xtuple_cust_id']}"
            )

        _logger.info(
            f"Linked {len(link_updates)} existing partners to xTuple customers"
        )


@ETL.pipeline(
    target_model="res.partner",
    importer_name="xtuple.partner.vendor.linker",
    sap_source="vendinfo",
    depends_on=["xtuple.partner.vendor.importer"],
)
class XtuplePartnerVendorLinker(models.AbstractModel):
    """ETL Pipeline for linking existing partners to xTuple vendors by name."""

    _name = "xtuple.partner.vendor.linker"
    _description = "xTuple Vendor Linker"

    @ETL.extract("vendinfo")
    def extract_vendors_for_linking(self, ctx: ETLContext) -> List[Dict]:
        """Extract vendors from xTuple that need linking."""
        select_clause = """
        SELECT
            vend_id,
            vend_name
        FROM vendinfo
        WHERE vend_name IS NOT NULL AND vend_name != ''
        """
        ctx.cr.execute(select_clause)
        vendors = ctx.cr.dictfetchall()

        _logger.info(f"Extracted {len(vendors)} vendors for linking")
        return vendors

    @ETL.transform()
    def transform_vendors_for_linking(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Find existing partners by name and prepare link updates."""
        vendors = extracted.get("extract_vendors_for_linking", [])

        # Build lookup of existing partners by name that don't have xtuple_vend_id
        ctx.env.cr.execute(
            """
            SELECT id, LOWER(name) FROM res_partner
            WHERE name IS NOT NULL AND name != ''
            AND xtuple_vend_id IS NULL
            """
        )
        partner_by_name = {row[1]: row[0] for row in ctx.env.cr.fetchall()}

        # Get vendor IDs already assigned to partners (to avoid duplicates)
        ctx.env.cr.execute(
            "SELECT xtuple_vend_id FROM res_partner WHERE xtuple_vend_id IS NOT NULL"
        )
        existing_vend_ids = {row[0] for row in ctx.env.cr.fetchall()}

        link_updates = []
        for vendor in vendors:
            vend_id = vendor.get("vend_id")
            # Skip if this vendor ID is already assigned to another partner
            if vend_id in existing_vend_ids:
                continue
            name = vendor.get("vend_name", "")
            if name and name.lower() in partner_by_name:
                link_updates.append(
                    {
                        "partner_id": partner_by_name[name.lower()],
                        "xtuple_vend_id": vend_id,
                        "name": name,
                    }
                )

        _logger.info(f"Found {len(link_updates)} partners to link as vendors by name")
        return link_updates

    @ETL.load()
    def load_vendor_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing partners with xTuple vendor IDs."""
        link_updates = transformed.get("transform_vendors_for_linking", [])

        if not link_updates:
            _logger.info("No partners to link as vendors")
            return

        for update in link_updates:
            ctx.env.cr.execute(
                """
                UPDATE res_partner
                SET xtuple_vend_id = %s, xtuple_partner_type = COALESCE(
                    CASE
                        WHEN xtuple_partner_type = 'customer' THEN 'both'
                        ELSE 'vendor'
                    END,
                    'vendor'
                ), supplier_rank = GREATEST(supplier_rank, 1)
                WHERE id = %s
                """,
                (update["xtuple_vend_id"], update["partner_id"]),
            )
            _logger.debug(
                f"Linked partner {update['partner_id']} (name={update['name']}) "
                f"to xTuple vendor {update['xtuple_vend_id']}"
            )

        _logger.info(f"Linked {len(link_updates)} existing partners to xTuple vendors")
