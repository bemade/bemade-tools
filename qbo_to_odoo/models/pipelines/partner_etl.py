"""QuickBooks Online Partner ETL Pipelines

This module handles the migration of Customers and Vendors from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Any, Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

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
        api_client = ctx.get_config("api_client")
        if not api_client:
            raise ValueError("API client not found in ETL context")

        # Get existing QBO customer IDs
        ctx.env.cr.execute(
            "SELECT qbo_customer_id FROM res_partner WHERE qbo_customer_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing customers in Odoo")

        # Fetch all customers from QBO
        customers = api_client.query_all(
            entity="Customer", where="Active IN (true, false)", order_by="Id"
        )

        # Filter out already imported
        new_customers = [c for c in customers if str(c.get("Id")) not in existing_ids]

        _logger.info(
            f"Extracted {len(customers)} customers from QBO, "
            f"{len(new_customers)} are new"
        )
        return new_customers

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
        api_client = ctx.get_config("api_client")
        if not api_client:
            raise ValueError("API client not found in ETL context")

        # Get existing QBO vendor IDs
        ctx.env.cr.execute(
            "SELECT qbo_vendor_id FROM res_partner WHERE qbo_vendor_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing vendors in Odoo")

        # Fetch all vendors from QBO
        vendors = api_client.query_all(
            entity="Vendor", where="Active IN (true, false)", order_by="Id"
        )

        # Filter out already imported
        new_vendors = [v for v in vendors if str(v.get("Id")) not in existing_ids]

        _logger.info(
            f"Extracted {len(vendors)} vendors from QBO, " f"{len(new_vendors)} are new"
        )
        return new_vendors

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
