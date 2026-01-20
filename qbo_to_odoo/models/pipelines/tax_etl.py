"""QuickBooks Online Tax ETL Pipeline

This module handles the migration of TaxCodes and TaxRates from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.tax",
    importer_name="qbo.tax.importer",
    sap_source="TaxCode",
    depends_on=["qbo.account.importer"],
)
class QboTaxImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Tax Codes and Rates."""

    _name = "qbo.tax.importer"
    _description = "QBO Tax Importer"

    # Class-level cache for tax rates lookup (needed in transform)
    _tax_rates_cache: Dict = {}

    @ETL.extract("TaxCode")
    def extract_taxes(self, ctx: ETLContext) -> List[Dict]:
        """Extract tax codes and rates from QBO API."""
        api_client = ctx.get_config("api_client")
        if not api_client:
            raise ValueError("API client not found in ETL context")

        # Get existing QBO tax IDs
        ctx.env.cr.execute(
            "SELECT qbo_tax_id FROM account_tax WHERE qbo_tax_id IS NOT NULL"
        )
        existing_tax_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_rate_id FROM account_tax WHERE qbo_tax_rate_id IS NOT NULL"
        )
        existing_rate_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}

        _logger.info(f"Found {len(existing_tax_ids)} existing tax codes in Odoo")
        _logger.info(f"Found {len(existing_rate_ids)} existing tax rates in Odoo")

        # Fetch all tax codes from QBO
        tax_codes = api_client.query_all(entity="TaxCode", order_by="Id")

        # Fetch all tax rates from QBO
        tax_rates = api_client.query_all(entity="TaxRate", order_by="Id")

        # Cache all tax rates for transform phase
        QboTaxImporter._tax_rates_cache = {str(tr.get("Id")): tr for tr in tax_rates}

        # Filter out already imported
        new_tax_codes = [
            {"type": "tax_code", "data": tc}
            for tc in tax_codes
            if str(tc.get("Id")) not in existing_tax_ids
            and tc.get("Active", True)
            and tc.get("Taxable", False)
        ]

        new_tax_rates = [
            {"type": "tax_rate", "data": tr}
            for tr in tax_rates
            if str(tr.get("Id")) not in existing_rate_ids and tr.get("Active", True)
        ]

        _logger.info(
            f"Extracted {len(tax_codes)} tax codes, {len(new_tax_codes)} are new/active"
        )
        _logger.info(
            f"Extracted {len(tax_rates)} tax rates, {len(new_tax_rates)} are new/active"
        )

        # Combine into single list - rates first, then codes (codes may reference rates)
        all_taxes = new_tax_rates + new_tax_codes
        return all_taxes

    @ETL.transform()
    def transform_taxes(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO taxes into Odoo account.tax values."""
        all_taxes = extracted.get("extract_taxes", [])

        # Split back into tax codes and rates
        tax_codes = [t["data"] for t in all_taxes if t.get("type") == "tax_code"]
        tax_rates = [t["data"] for t in all_taxes if t.get("type") == "tax_rate"]
        all_tax_rates = QboTaxImporter._tax_rates_cache

        company = ctx.env.company

        # Find a tax group for this company/country
        tax_group = ctx.env["account.tax.group"].search(
            [
                ("company_id", "=", company.id),
            ],
            limit=1,
        )
        if not tax_group:
            tax_group = ctx.env["account.tax.group"].search([], limit=1)

        tax_vals = []

        # First, create individual tax rates - duplicated for both sale and purchase
        for rate in tax_rates:
            rate_value = 0.0
            if rate.get("RateValue"):
                rate_value = float(rate.get("RateValue", 0) or 0)
            elif rate.get("EffectiveTaxRate"):
                # Get current effective rate
                for eff in rate.get("EffectiveTaxRate", []):
                    if not eff.get("EndDate"):
                        rate_value = float(eff.get("RateValue", 0) or 0)
                        break

            # Create tax rate for sales
            tax_vals.append(
                {
                    "name": rate.get("Name", "") + " %",
                    "description": rate.get("Description", ""),
                    "qbo_tax_rate_id": str(rate.get("Id")),
                    "amount_type": "percent",
                    "amount": rate_value,
                    "type_tax_use": "sale",
                    "company_id": company.id,
                    "country_id": company.country_id.id,
                    "tax_group_id": tax_group.id if tax_group else False,
                }
            )

            # Create tax rate for purchases
            tax_vals.append(
                {
                    "name": rate.get("Name", "") + " %",
                    "description": rate.get("Description", ""),
                    "qbo_tax_rate_id": str(rate.get("Id")),
                    "amount_type": "percent",
                    "amount": rate_value,
                    "type_tax_use": "purchase",
                    "company_id": company.id,
                    "country_id": company.country_id.id,
                    "tax_group_id": tax_group.id if tax_group else False,
                }
            )

        # Then create tax codes (group taxes that reference rates)
        for tc in tax_codes:
            # Create sale tax if SalesTaxRateList exists
            sales_rates = tc.get("SalesTaxRateList", {}).get("TaxRateDetail", [])
            if sales_rates:
                vals = {
                    "name": tc.get("Name", ""),
                    "description": tc.get("Description", ""),
                    "qbo_tax_id": str(tc.get("Id")),
                    "amount_type": "group",
                    "amount": 0,
                    "type_tax_use": "sale",
                    "company_id": company.id,
                    "country_id": company.country_id.id,
                    "tax_group_id": tax_group.id if tax_group else False,
                }
                tax_vals.append(vals)

            # Create purchase tax if PurchaseTaxRateList exists
            purchase_rates = tc.get("PurchaseTaxRateList", {}).get("TaxRateDetail", [])
            if purchase_rates:
                vals = {
                    "name": tc.get("Name", ""),
                    "description": tc.get("Description", ""),
                    "qbo_tax_id": str(tc.get("Id")),
                    "amount_type": "group",
                    "amount": 0,
                    "type_tax_use": "purchase",
                    "company_id": company.id,
                    "country_id": company.country_id.id,
                    "tax_group_id": tax_group.id if tax_group else False,
                }
                tax_vals.append(vals)

        _logger.info(f"Transformed {len(tax_vals)} tax records")
        return tax_vals

    @ETL.load()
    def load_taxes(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load taxes into Odoo."""
        tax_vals = transformed.get("transform_taxes", [])

        if not tax_vals:
            _logger.info("No new taxes to create")
            return

        # Batch create taxes
        taxes = ctx.env["account.tax"].create(tax_vals)
        _logger.info(f"Created {len(taxes)} taxes")
