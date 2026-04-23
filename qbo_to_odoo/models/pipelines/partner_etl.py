"""QuickBooks Online Partner ETL Pipelines

This module handles the migration of Customers and Vendors from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.qbo_to_odoo.models.pipelines.utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="res.partner",
    importer_name="qbo.customer.importer",
    sap_source="Customer",
    depends_on=["qbo.term.importer"],
)
class QboCustomerImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Customers."""

    _name = "qbo.customer.importer"
    _description = "QBO Customer Importer"

    @ETL.extract("Customer")
    def extract_customers(self, ctx: ETLContext) -> List[Dict]:
        """Extract customers from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO customer IDs
        ctx.env.cr.execute(
            "SELECT qbo_customer_id FROM res_partner WHERE qbo_customer_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing customers in Odoo")

        # Get existing partner names for deduplication (cross-system matching)
        ctx.env.cr.execute(
            "SELECT LOWER(name) FROM res_partner WHERE name IS NOT NULL AND name != ''"
        )
        existing_names = {row[0] for row in ctx.env.cr.fetchall()}
        _logger.info(
            f"Found {len(existing_names)} existing partners by name for deduplication"
        )

        # Fetch all customers from QBO
        customers = api_client.query_all(
            entity="Customer", where="Active IN (true, false)", order_by="Id"
        )

        # Filter out already imported by QBO ID
        new_customers = [c for c in customers if str(c.get("Id")) not in existing_ids]

        # Filter out customers that match existing partners by name (deduplication)
        def get_customer_name(c):
            return (c.get("DisplayName") or c.get("CompanyName") or "").lower()

        deduped_customers = [
            c for c in new_customers if get_customer_name(c) not in existing_names
        ]

        _logger.info(
            f"Extracted {len(customers)} customers from QBO, {len(new_customers)} new by ID, "
            f"{len(deduped_customers)} after deduplication by name"
        )
        return deduped_customers

    @ETL.transform()
    def transform_customers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO customers into Odoo partner values."""
        customers = extracted.get("extract_customers", [])

        # Build payment term lookup
        ctx.env.cr.execute(
            "SELECT qbo_term_id, id FROM account_payment_term "
            "WHERE qbo_term_id IS NOT NULL"
        )
        term_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        partner_vals = []

        for customer in customers:
            # Build address
            bill_addr = customer.get("BillAddr", {}) or {}

            # Get primary email
            email = customer.get("PrimaryEmailAddr", {})
            email = email.get("Address") if email else None

            # Get primary phone
            phone = customer.get("PrimaryPhone", {})
            phone = phone.get("FreeFormNumber") if phone else None

            # Get mobile - only use if phone is not set
            mobile = None
            if not phone:
                mobile_data = customer.get("Mobile", {})
                mobile = mobile_data.get("FreeFormNumber") if mobile_data else None

            # Get website
            website = customer.get("WebAddr", {})
            website = website.get("URI") if website else None

            vals = {
                "name": customer.get("DisplayName")
                or customer.get("CompanyName")
                or "Unknown",
                "company_type": (
                    "company" if customer.get("CompanyName") else "person"
                ),
                "is_company": bool(customer.get("CompanyName")),
                "customer_rank": 1,
                "supplier_rank": 0,
                "email": email,
                "phone": phone or mobile,  # Use mobile as phone if no phone
                "website": website,
                "comment": customer.get("Notes"),
                "street": bill_addr.get("Line1"),
                "street2": bill_addr.get("Line2"),
                "city": bill_addr.get("City"),
                "zip": bill_addr.get("PostalCode"),
                "country_id": self._get_country_id(ctx, bill_addr.get("Country")),
                "state_id": self._get_state_id(
                    ctx,
                    bill_addr.get("CountrySubDivisionCode"),
                    bill_addr.get("Country"),
                ),
                "active": customer.get("Active", True),
                "qbo_customer_id": int(customer.get("Id")),
                "ref": customer.get("Id"),
            }

            # Payment terms
            sales_term_ref = customer.get("SalesTermRef", {})
            if sales_term_ref:
                term_id = term_map.get(str(sales_term_ref.get("value")))
                if term_id:
                    vals["property_payment_term_id"] = term_id

            partner_vals.append(vals)

        _logger.info(f"Transformed {len(partner_vals)} customer records")
        return partner_vals

    def _get_country_id(self, ctx: ETLContext, country_code: str) -> int:
        """Get Odoo country ID from country code.

        Applies an explicit alias map for common 3-letter codes (e.g. "USA"→"US",
        "CAN"→"CA") before falling back to a name lookup.  The name fallback uses
        ``=ilike`` (case-insensitive exact match) and only applies to strings longer
        than 3 characters to prevent short ISO-alpha-3 codes from fuzzy-matching
        unrelated country names (e.g. "USA" matching "United States Minor Outlying
        Islands").
        """
        if not country_code:
            return False

        # Explicit alias map: alpha-3 codes and other common variants
        _COUNTRY_ALIAS = {
            "USA": "US",
            "CAN": "CA",
            "MEX": "MX",
            "GBR": "GB",
            "FRA": "FR",
            "DEU": "DE",
            "ITA": "IT",
            "ESP": "ES",
            "CHN": "CN",
            "JPN": "JP",
            "AUS": "AU",
            "BRA": "BR",
            "IND": "IN",
            "RUS": "RU",
            "ZAF": "ZA",
        }
        resolved = _COUNTRY_ALIAS.get(country_code.upper(), country_code)

        # Try exact ISO-2 code match first
        country = ctx.env["res.country"].search(
            [("code", "=", resolved)], limit=1
        )
        if country:
            return country.id

        # Name fallback: only for strings longer than 3 chars (avoids alpha-3 collisions)
        if len(resolved) > 3:
            country = ctx.env["res.country"].search(
                [("name", "=ilike", resolved)], limit=1
            )
            return country.id if country else False

        return False

    def _get_state_id(self, ctx: ETLContext, state_code: str, country_code: str) -> int:
        """Get Odoo state ID from state and country codes."""
        if not state_code:
            return False

        country_id = self._get_country_id(ctx, country_code)

        domain = [("code", "=", state_code)]
        if country_id:
            domain.append(("country_id", "=", country_id))

        state = ctx.env["res.country.state"].search(domain, limit=1)
        return state.id if state else False

    @ETL.load()
    def load_customers(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load customers into Odoo."""
        partner_vals = transformed.get("transform_customers", [])

        if not partner_vals:
            _logger.info("No new customers to create")
            return

        partners = ctx.env["res.partner"].create(partner_vals)
        _logger.info(f"Created {len(partners)} customers")



@ETL.pipeline(
    target_model="res.partner",
    importer_name="qbo.vendor.importer",
    sap_source="Vendor",
    depends_on=["qbo.term.importer"],
)
class QboVendorImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Vendors."""

    _name = "qbo.vendor.importer"
    _description = "QBO Vendor Importer"

    @ETL.extract("Vendor")
    def extract_vendors(self, ctx: ETLContext) -> List[Dict]:
        """Extract vendors from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO vendor IDs
        ctx.env.cr.execute(
            "SELECT qbo_vendor_id FROM res_partner WHERE qbo_vendor_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing vendors in Odoo")

        # Get existing partner names for deduplication (cross-system matching)
        ctx.env.cr.execute(
            "SELECT LOWER(name) FROM res_partner WHERE name IS NOT NULL AND name != ''"
        )
        existing_names = {row[0] for row in ctx.env.cr.fetchall()}
        _logger.info(
            f"Found {len(existing_names)} existing partners by name for deduplication"
        )

        # Fetch all vendors from QBO
        vendors = api_client.query_all(
            entity="Vendor", where="Active IN (true, false)", order_by="Id"
        )

        # Filter out already imported by QBO ID
        new_vendors = [v for v in vendors if str(v.get("Id")) not in existing_ids]

        # Filter out vendors that match existing partners by name (deduplication)
        def get_vendor_name(v):
            return (v.get("DisplayName") or v.get("CompanyName") or "").lower()

        deduped_vendors = [
            v for v in new_vendors if get_vendor_name(v) not in existing_names
        ]

        _logger.info(
            f"Extracted {len(vendors)} vendors from QBO, {len(new_vendors)} new by ID, "
            f"{len(deduped_vendors)} after deduplication by name"
        )
        return deduped_vendors

    @ETL.transform()
    def transform_vendors(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO vendors into Odoo partner values."""
        vendors = extracted.get("extract_vendors", [])

        # Build payment term lookup
        ctx.env.cr.execute(
            "SELECT qbo_term_id, id FROM account_payment_term "
            "WHERE qbo_term_id IS NOT NULL"
        )
        term_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        partner_vals = []

        for vendor in vendors:
            # Build address
            bill_addr = vendor.get("BillAddr", {}) or {}

            # Get primary email
            email = vendor.get("PrimaryEmailAddr", {})
            email = email.get("Address") if email else None

            # Get primary phone
            phone = vendor.get("PrimaryPhone", {})
            phone = phone.get("FreeFormNumber") if phone else None

            # Get mobile - only use if phone is not set
            mobile = None
            if not phone:
                mobile_data = vendor.get("Mobile", {})
                mobile = mobile_data.get("FreeFormNumber") if mobile_data else None

            # Get website
            website = vendor.get("WebAddr", {})
            website = website.get("URI") if website else None

            vals = {
                "name": vendor.get("DisplayName")
                or vendor.get("CompanyName")
                or "Unknown",
                "company_type": ("company" if vendor.get("CompanyName") else "person"),
                "is_company": bool(vendor.get("CompanyName")),
                "customer_rank": 0,
                "supplier_rank": 1,
                "email": email,
                "phone": phone or mobile,  # Use mobile as phone if no phone
                "website": website,
                "comment": vendor.get("Notes"),
                "street": bill_addr.get("Line1"),
                "street2": bill_addr.get("Line2"),
                "city": bill_addr.get("City"),
                "zip": bill_addr.get("PostalCode"),
                "country_id": self._get_country_id(ctx, bill_addr.get("Country")),
                "state_id": self._get_state_id(
                    ctx,
                    bill_addr.get("CountrySubDivisionCode"),
                    bill_addr.get("Country"),
                ),
                "active": vendor.get("Active", True),
                "qbo_vendor_id": int(vendor.get("Id")),
                "ref": vendor.get("Id"),
            }

            # Payment terms
            term_ref = vendor.get("TermRef", {})
            if term_ref:
                term_id = term_map.get(str(term_ref.get("value")))
                if term_id:
                    vals["property_supplier_payment_term_id"] = term_id

            # Currency
            currency_ref = vendor.get("CurrencyRef", {})
            if currency_ref:
                currency_code = currency_ref.get("value")
                if currency_code:
                    currency = ctx.env["res.currency"].search(
                        [("name", "=", currency_code)], limit=1
                    )
                    if currency:
                        vals["property_purchase_currency_id"] = currency.id

            partner_vals.append(vals)

        _logger.info(f"Transformed {len(partner_vals)} vendor records")
        return partner_vals

    def _get_country_id(self, ctx: ETLContext, country_code: str) -> int:
        """Get Odoo country ID from country code.

        Applies an explicit alias map for common 3-letter codes (e.g. "USA"→"US",
        "CAN"→"CA") before falling back to a name lookup.  The name fallback uses
        ``=ilike`` (case-insensitive exact match) and only applies to strings longer
        than 3 characters to prevent short ISO-alpha-3 codes from fuzzy-matching
        unrelated country names (e.g. "USA" matching "United States Minor Outlying
        Islands").
        """
        if not country_code:
            return False

        # Explicit alias map: alpha-3 codes and other common variants
        _COUNTRY_ALIAS = {
            "USA": "US",
            "CAN": "CA",
            "MEX": "MX",
            "GBR": "GB",
            "FRA": "FR",
            "DEU": "DE",
            "ITA": "IT",
            "ESP": "ES",
            "CHN": "CN",
            "JPN": "JP",
            "AUS": "AU",
            "BRA": "BR",
            "IND": "IN",
            "RUS": "RU",
            "ZAF": "ZA",
        }
        resolved = _COUNTRY_ALIAS.get(country_code.upper(), country_code)

        # Try exact ISO-2 code match first
        country = ctx.env["res.country"].search(
            [("code", "=", resolved)], limit=1
        )
        if country:
            return country.id

        # Name fallback: only for strings longer than 3 chars (avoids alpha-3 collisions)
        if len(resolved) > 3:
            country = ctx.env["res.country"].search(
                [("name", "=ilike", resolved)], limit=1
            )
            return country.id if country else False

        return False

    def _get_state_id(self, ctx: ETLContext, state_code: str, country_code: str) -> int:
        """Get Odoo state ID from state and country codes."""
        if not state_code:
            return False

        country_id = self._get_country_id(ctx, country_code)

        domain = [("code", "=", state_code)]
        if country_id:
            domain.append(("country_id", "=", country_id))

        state = ctx.env["res.country.state"].search(domain, limit=1)
        return state.id if state else False

    @ETL.load()
    def load_vendors(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load vendors into Odoo."""
        partner_vals = transformed.get("transform_vendors", [])

        if not partner_vals:
            _logger.info("No new vendors to create")
            return

        partners = ctx.env["res.partner"].create(partner_vals)
        _logger.info(f"Created {len(partners)} vendors")



@ETL.pipeline(
    target_model="res.partner",
    importer_name="qbo.customer.linker",
    sap_source="Customer",
    depends_on=["qbo.customer.importer"],
)
class QboCustomerLinker(models.AbstractModel):
    """ETL Pipeline for linking existing partners to QBO Customers by name."""

    _name = "qbo.customer.linker"
    _description = "QBO Customer Linker"

    @ETL.extract("Customer")
    def extract_customers_for_linking(self, ctx: ETLContext) -> List[Dict]:
        """Extract customers from QBO API that need linking."""
        api_client = get_api_client(ctx)

        # Fetch all customers from QBO
        customers = api_client.query_all(
            entity="Customer", where="Active IN (true, false)", order_by="Id"
        )

        _logger.info(f"Extracted {len(customers)} customers for linking")
        return customers

    @ETL.transform()
    def transform_customers_for_linking(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Find existing partners by name and prepare link updates."""
        customers = extracted.get("extract_customers_for_linking", [])

        # Build lookup of existing partners by name that don't have qbo_customer_id
        ctx.env.cr.execute(
            """
            SELECT id, LOWER(name) FROM res_partner
            WHERE name IS NOT NULL AND name != ''
            AND qbo_customer_id IS NULL
            """
        )
        partner_by_name = {row[1]: row[0] for row in ctx.env.cr.fetchall()}

        # Build payment term lookup
        ctx.env.cr.execute(
            "SELECT qbo_term_id, id FROM account_payment_term "
            "WHERE qbo_term_id IS NOT NULL"
        )
        term_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        link_updates = []
        for customer in customers:
            name = customer.get("DisplayName") or customer.get("CompanyName") or ""
            if name and name.lower() in partner_by_name:
                bill_addr = customer.get("BillAddr", {}) or {}
                update = {
                    "partner_id": partner_by_name[name.lower()],
                    "qbo_customer_id": int(customer.get("Id")),
                    "name": name,
                    # Address fields for backfill (only written when target is empty)
                    "addr_street": bill_addr.get("Line1"),
                    "addr_street2": bill_addr.get("Line2"),
                    "addr_city": bill_addr.get("City"),
                    "addr_zip": bill_addr.get("PostalCode"),
                    "addr_country_code": bill_addr.get("Country"),
                    "addr_state_code": bill_addr.get("CountrySubDivisionCode"),
                }

                # Payment terms
                sales_term_ref = customer.get("SalesTermRef") or {}
                if sales_term_ref:
                    term_id = term_map.get(str(sales_term_ref.get("value")))
                    if term_id:
                        update["property_payment_term_id"] = term_id

                link_updates.append(update)

        _logger.info(f"Found {len(link_updates)} partners to link as customers by name")
        return link_updates

    @ETL.load()
    def load_customer_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing partners with QBO customer IDs, payment terms, and address (backfill only)."""
        link_updates = transformed.get("transform_customers_for_linking", [])

        if not link_updates:
            _logger.info("No partners to link as customers")
            return

        Partner = ctx.env["res.partner"]
        for update in link_updates:
            partner = Partner.browse(update["partner_id"])
            vals = {
                "qbo_customer_id": update["qbo_customer_id"],
                "customer_rank": max(partner.customer_rank, 1),
            }
            if "property_payment_term_id" in update:
                vals["property_payment_term_id"] = update["property_payment_term_id"]

            # Backfill address fields that are currently empty (per-field guard)
            if not partner.street and update.get("addr_street"):
                vals["street"] = update["addr_street"]
            if not partner.street2 and update.get("addr_street2"):
                vals["street2"] = update["addr_street2"]
            if not partner.city and update.get("addr_city"):
                vals["city"] = update["addr_city"]
            if not partner.zip and update.get("addr_zip"):
                vals["zip"] = update["addr_zip"]
            if not partner.country_id and update.get("addr_country_code"):
                country_id = self._get_country_id(ctx, update["addr_country_code"])
                if country_id:
                    vals["country_id"] = country_id
            if not partner.state_id and update.get("addr_state_code"):
                state_id = self._get_state_id(
                    ctx, update["addr_state_code"], update.get("addr_country_code")
                )
                if state_id:
                    vals["state_id"] = state_id

            partner.write(vals)

            _logger.debug(
                f"Linked partner {update['partner_id']} (name={update['name']}) "
                f"to QBO customer {update['qbo_customer_id']}"
            )

        _logger.info(f"Linked {len(link_updates)} existing partners to QBO customers")

    def _get_country_id(self, ctx: ETLContext, country_code: str) -> int:
        """Get Odoo country ID from country code with alias map and tightened name fallback."""
        if not country_code:
            return False

        _COUNTRY_ALIAS = {
            "USA": "US",
            "CAN": "CA",
            "MEX": "MX",
            "GBR": "GB",
            "FRA": "FR",
            "DEU": "DE",
            "ITA": "IT",
            "ESP": "ES",
            "CHN": "CN",
            "JPN": "JP",
            "AUS": "AU",
            "BRA": "BR",
            "IND": "IN",
            "RUS": "RU",
            "ZAF": "ZA",
        }
        resolved = _COUNTRY_ALIAS.get(country_code.upper(), country_code)
        country = ctx.env["res.country"].search([("code", "=", resolved)], limit=1)
        if country:
            return country.id
        if len(resolved) > 3:
            country = ctx.env["res.country"].search(
                [("name", "=ilike", resolved)], limit=1
            )
            return country.id if country else False
        return False

    def _get_state_id(self, ctx: ETLContext, state_code: str, country_code: str) -> int:
        """Get Odoo state ID from state and country codes."""
        if not state_code:
            return False
        country_id = self._get_country_id(ctx, country_code)
        domain = [("code", "=", state_code)]
        if country_id:
            domain.append(("country_id", "=", country_id))
        state = ctx.env["res.country.state"].search(domain, limit=1)
        return state.id if state else False


@ETL.pipeline(
    target_model="res.partner",
    importer_name="qbo.vendor.linker",
    sap_source="Vendor",
    depends_on=["qbo.vendor.importer"],
)
class QboVendorLinker(models.AbstractModel):
    """ETL Pipeline for linking existing partners to QBO Vendors by name."""

    _name = "qbo.vendor.linker"
    _description = "QBO Vendor Linker"

    @ETL.extract("Vendor")
    def extract_vendors_for_linking(self, ctx: ETLContext) -> List[Dict]:
        """Extract vendors from QBO API that need linking."""
        api_client = get_api_client(ctx)

        # Fetch all vendors from QBO
        vendors = api_client.query_all(
            entity="Vendor", where="Active IN (true, false)", order_by="Id"
        )

        _logger.info(f"Extracted {len(vendors)} vendors for linking")
        return vendors

    @ETL.transform()
    def transform_vendors_for_linking(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Find existing partners by name and prepare link updates."""
        vendors = extracted.get("extract_vendors_for_linking", [])

        # Build lookup of existing partners by name that don't have qbo_vendor_id
        ctx.env.cr.execute(
            """
            SELECT id, LOWER(name) FROM res_partner
            WHERE name IS NOT NULL AND name != ''
            AND qbo_vendor_id IS NULL
            """
        )
        partner_by_name = {row[1]: row[0] for row in ctx.env.cr.fetchall()}

        # Build payment term lookup
        ctx.env.cr.execute(
            "SELECT qbo_term_id, id FROM account_payment_term "
            "WHERE qbo_term_id IS NOT NULL"
        )
        term_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        link_updates = []
        for vendor in vendors:
            name = vendor.get("DisplayName") or vendor.get("CompanyName") or ""
            if name and name.lower() in partner_by_name:
                bill_addr = vendor.get("BillAddr", {}) or {}
                update = {
                    "partner_id": partner_by_name[name.lower()],
                    "qbo_vendor_id": int(vendor.get("Id")),
                    "name": name,
                    # Address fields for backfill (only written when target is empty)
                    "addr_street": bill_addr.get("Line1"),
                    "addr_street2": bill_addr.get("Line2"),
                    "addr_city": bill_addr.get("City"),
                    "addr_zip": bill_addr.get("PostalCode"),
                    "addr_country_code": bill_addr.get("Country"),
                    "addr_state_code": bill_addr.get("CountrySubDivisionCode"),
                }

                # Payment terms
                term_ref = vendor.get("TermRef") or {}
                if term_ref:
                    term_id = term_map.get(str(term_ref.get("value")))
                    if term_id:
                        update["property_supplier_payment_term_id"] = term_id

                # Vendor currency
                currency_ref = vendor.get("CurrencyRef") or {}
                if currency_ref:
                    currency_code = currency_ref.get("value")
                    if currency_code:
                        currency = ctx.env["res.currency"].search(
                            [("name", "=", currency_code)], limit=1
                        )
                        if currency:
                            update["property_purchase_currency_id"] = currency.id

                link_updates.append(update)

        _logger.info(f"Found {len(link_updates)} partners to link as vendors by name")
        return link_updates

    @ETL.load()
    def load_vendor_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing partners with QBO vendor IDs, payment terms, currency, and address (backfill only)."""
        link_updates = transformed.get("transform_vendors_for_linking", [])

        if not link_updates:
            _logger.info("No partners to link as vendors")
            return

        Partner = ctx.env["res.partner"]
        for update in link_updates:
            partner = Partner.browse(update["partner_id"])
            vals = {
                "qbo_vendor_id": update["qbo_vendor_id"],
                "supplier_rank": max(partner.supplier_rank, 1),
            }
            if "property_supplier_payment_term_id" in update:
                vals["property_supplier_payment_term_id"] = update["property_supplier_payment_term_id"]
            if "property_purchase_currency_id" in update:
                vals["property_purchase_currency_id"] = update["property_purchase_currency_id"]

            # Backfill address fields that are currently empty (per-field guard)
            if not partner.street and update.get("addr_street"):
                vals["street"] = update["addr_street"]
            if not partner.street2 and update.get("addr_street2"):
                vals["street2"] = update["addr_street2"]
            if not partner.city and update.get("addr_city"):
                vals["city"] = update["addr_city"]
            if not partner.zip and update.get("addr_zip"):
                vals["zip"] = update["addr_zip"]
            if not partner.country_id and update.get("addr_country_code"):
                country_id = self._get_country_id(ctx, update["addr_country_code"])
                if country_id:
                    vals["country_id"] = country_id
            if not partner.state_id and update.get("addr_state_code"):
                state_id = self._get_state_id(
                    ctx, update["addr_state_code"], update.get("addr_country_code")
                )
                if state_id:
                    vals["state_id"] = state_id

            partner.write(vals)

            _logger.debug(
                f"Linked partner {update['partner_id']} (name={update['name']}) "
                f"to QBO vendor {update['qbo_vendor_id']}"
            )

        _logger.info(f"Linked {len(link_updates)} existing partners to QBO vendors")

    def _get_country_id(self, ctx: ETLContext, country_code: str) -> int:
        """Get Odoo country ID from country code with alias map and tightened name fallback."""
        if not country_code:
            return False

        _COUNTRY_ALIAS = {
            "USA": "US",
            "CAN": "CA",
            "MEX": "MX",
            "GBR": "GB",
            "FRA": "FR",
            "DEU": "DE",
            "ITA": "IT",
            "ESP": "ES",
            "CHN": "CN",
            "JPN": "JP",
            "AUS": "AU",
            "BRA": "BR",
            "IND": "IN",
            "RUS": "RU",
            "ZAF": "ZA",
        }
        resolved = _COUNTRY_ALIAS.get(country_code.upper(), country_code)
        country = ctx.env["res.country"].search([("code", "=", resolved)], limit=1)
        if country:
            return country.id
        if len(resolved) > 3:
            country = ctx.env["res.country"].search(
                [("name", "=ilike", resolved)], limit=1
            )
            return country.id if country else False
        return False

    def _get_state_id(self, ctx: ETLContext, state_code: str, country_code: str) -> int:
        """Get Odoo state ID from state and country codes."""
        if not state_code:
            return False
        country_id = self._get_country_id(ctx, country_code)
        domain = [("code", "=", state_code)]
        if country_id:
            domain.append(("country_id", "=", country_id))
        state = ctx.env["res.country.state"].search(domain, limit=1)
        return state.id if state else False
