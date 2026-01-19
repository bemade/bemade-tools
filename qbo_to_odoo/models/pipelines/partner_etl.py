"""QuickBooks Online Partner ETL Pipelines

This module handles the migration of Customers and Vendors from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Any, Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.qbo_to_odoo.models.pipelines.utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="res.partner",
    importer_name="qbo.customer.importer",
    sap_source="Customer",
    depends_on=[],
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

            partner_vals.append(vals)

        _logger.info(f"Transformed {len(partner_vals)} customer records")
        return partner_vals

    def _get_country_id(self, ctx: ETLContext, country_code: str) -> int:
        """Get Odoo country ID from country code."""
        if not country_code:
            return False

        country = ctx.env["res.country"].search(
            [
                "|",
                ("code", "=", country_code),
                ("name", "ilike", country_code),
            ],
            limit=1,
        )

        return country.id if country else False

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

        # Update last sync timestamp
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection.last_customer_sync = ctx.env.cr.now()


@ETL.pipeline(
    target_model="res.partner",
    importer_name="qbo.vendor.importer",
    sap_source="Vendor",
    depends_on=[],
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

            partner_vals.append(vals)

        _logger.info(f"Transformed {len(partner_vals)} vendor records")
        return partner_vals

    def _get_country_id(self, ctx: ETLContext, country_code: str) -> int:
        """Get Odoo country ID from country code."""
        if not country_code:
            return False

        country = ctx.env["res.country"].search(
            [
                "|",
                ("code", "=", country_code),
                ("name", "ilike", country_code),
            ],
            limit=1,
        )

        return country.id if country else False

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

        # Update last sync timestamp
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection.last_vendor_sync = ctx.env.cr.now()


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

        link_updates = []
        for customer in customers:
            name = customer.get("DisplayName") or customer.get("CompanyName") or ""
            if name and name.lower() in partner_by_name:
                link_updates.append(
                    {
                        "partner_id": partner_by_name[name.lower()],
                        "qbo_customer_id": int(customer.get("Id")),
                        "name": name,
                    }
                )

        _logger.info(f"Found {len(link_updates)} partners to link as customers by name")
        return link_updates

    @ETL.load()
    def load_customer_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing partners with QBO customer IDs."""
        link_updates = transformed.get("transform_customers_for_linking", [])

        if not link_updates:
            _logger.info("No partners to link as customers")
            return

        for update in link_updates:
            ctx.env.cr.execute(
                """
                UPDATE res_partner
                SET qbo_customer_id = %s, customer_rank = GREATEST(customer_rank, 1)
                WHERE id = %s
                """,
                (update["qbo_customer_id"], update["partner_id"]),
            )
            _logger.debug(
                f"Linked partner {update['partner_id']} (name={update['name']}) "
                f"to QBO customer {update['qbo_customer_id']}"
            )

        _logger.info(f"Linked {len(link_updates)} existing partners to QBO customers")


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

        link_updates = []
        for vendor in vendors:
            name = vendor.get("DisplayName") or vendor.get("CompanyName") or ""
            if name and name.lower() in partner_by_name:
                link_updates.append(
                    {
                        "partner_id": partner_by_name[name.lower()],
                        "qbo_vendor_id": int(vendor.get("Id")),
                        "name": name,
                    }
                )

        _logger.info(f"Found {len(link_updates)} partners to link as vendors by name")
        return link_updates

    @ETL.load()
    def load_vendor_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing partners with QBO vendor IDs."""
        link_updates = transformed.get("transform_vendors_for_linking", [])

        if not link_updates:
            _logger.info("No partners to link as vendors")
            return

        for update in link_updates:
            ctx.env.cr.execute(
                """
                UPDATE res_partner
                SET qbo_vendor_id = %s, supplier_rank = GREATEST(supplier_rank, 1)
                WHERE id = %s
                """,
                (update["qbo_vendor_id"], update["partner_id"]),
            )
            _logger.debug(
                f"Linked partner {update['partner_id']} (name={update['name']}) "
                f"to QBO vendor {update['qbo_vendor_id']}"
            )

        _logger.info(f"Linked {len(link_updates)} existing partners to QBO vendors")
