"""QuickBooks Online Tax ETL Pipeline

This module handles the migration of TaxCodes and TaxRates from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

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
    # Class-level cache for all tax codes (needed to link group children in load)
    _tax_codes_cache: List = []

    @ETL.extract("TaxCode")
    def extract_taxes(self, ctx: ETLContext) -> List[Dict]:
        """Extract tax codes and rates from QBO API."""
        api_client = get_api_client(ctx)

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

        # Cache all tax codes for linking group children in load phase
        QboTaxImporter._tax_codes_cache = tax_codes

        # Filter out already imported.  Include inactive/hidden codes and
        # rates so that historical transactions referencing them can still
        # resolve via resolve_tax() during the GL-first import.
        new_tax_codes = [
            {"type": "tax_code", "data": tc}
            for tc in tax_codes
            if str(tc.get("Id")) not in existing_tax_ids
            and tc.get("Taxable", False)
        ]

        new_tax_rates = [
            {"type": "tax_rate", "data": tr}
            for tr in tax_rates
            if str(tr.get("Id")) not in existing_rate_ids
        ]

        _logger.info(
            f"Extracted {len(tax_codes)} tax codes, {len(new_tax_codes)} new"
        )
        _logger.info(
            f"Extracted {len(tax_rates)} tax rates, {len(new_tax_rates)} new"
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

        # Create a default tax group if none exists
        if not tax_group:
            tax_group = ctx.env["account.tax.group"].create(
                {
                    "name": f"{company.name} Taxes",
                    "company_id": company.id,
                }
            )

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
                    "tax_group_id": tax_group.id,
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
                    "tax_group_id": tax_group.id,
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
                    "tax_group_id": tax_group.id,
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
                    "tax_group_id": tax_group.id,
                }
                tax_vals.append(vals)

        _logger.info(f"Transformed {len(tax_vals)} tax records")
        return tax_vals

    @ETL.load()
    def load_taxes(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load taxes into Odoo."""
        tax_vals = transformed.get("transform_taxes", [])

        if tax_vals:
            # Batch create taxes
            taxes = ctx.env["account.tax"].create(tax_vals)
            _logger.info(f"Created {len(taxes)} taxes")
        else:
            _logger.info("No new taxes to create")

        # Link children to group taxes (fixes both new and existing)
        self._link_group_tax_children(ctx)

        # Set tax accounts on repartition lines
        self._set_repartition_accounts(ctx)

    def _link_group_tax_children(self, ctx: ETLContext) -> None:
        """Link child tax rates to their parent group taxes.

        Uses the cached QBO TaxCode data to find which tax rates belong
        to each tax code, then sets children_tax_ids on the corresponding
        Odoo group taxes. Applies to all group taxes (new and existing).
        """
        all_tax_codes = QboTaxImporter._tax_codes_cache
        if not all_tax_codes:
            _logger.info("No cached tax codes — skipping group children linking")
            return

        # Build lookup maps: {qbo_tax_rate_id: odoo_tax_id} per type_tax_use
        ctx.env.cr.execute(
            "SELECT qbo_tax_rate_id, id, type_tax_use FROM account_tax "
            "WHERE qbo_tax_rate_id IS NOT NULL"
        )
        rate_id_map = {"sale": {}, "purchase": {}}
        for qbo_rate_id, tax_id, type_tax_use in ctx.env.cr.fetchall():
            rate_id_map.get(type_tax_use, {})[str(qbo_rate_id)] = tax_id

        linked_count = 0
        Tax = ctx.env["account.tax"]

        for tc in all_tax_codes:
            qbo_tax_id = str(tc.get("Id"))

            for tax_use, rate_list_key in [
                ("sale", "SalesTaxRateList"),
                ("purchase", "PurchaseTaxRateList"),
            ]:
                details = tc.get(rate_list_key, {}).get("TaxRateDetail", [])
                if not details:
                    continue

                child_ids = []
                for detail in details:
                    rate_ref = str(
                        detail.get("TaxRateRef", {}).get("value", "")
                    )
                    odoo_id = rate_id_map[tax_use].get(rate_ref)
                    if odoo_id:
                        child_ids.append(odoo_id)

                if not child_ids:
                    continue

                group_tax = Tax.search(
                    [
                        ("qbo_tax_id", "=", qbo_tax_id),
                        ("type_tax_use", "=", tax_use),
                        ("amount_type", "=", "group"),
                    ],
                    limit=1,
                )
                if group_tax:
                    group_tax.children_tax_ids = [
                        (6, 0, child_ids),
                    ]
                    linked_count += 1

        _logger.info(
            f"Linked children for {linked_count} group taxes"
        )

    @staticmethod
    def _set_repartition_accounts(ctx: ETLContext) -> None:
        """Set tax accounts on repartition lines for QBO-imported taxes.

        QBO uses a single tax payable account (GlobalTaxPayable subtype)
        for all sales tax repartition.  We find that account and set it
        on every repartition line of type 'tax' that has no account.
        """
        # Find the QBO tax payable account (GlobalTaxPayable subtype)
        tax_payable = ctx.env["account.account"].search(
            [("qbo_id", "!=", False), ("name", "ilike", "GST/HST - QST Payable")],
            limit=1,
        )
        if not tax_payable:
            # Fallback: any account with GlobalTaxPayable subtype
            ctx.env.cr.execute("""
                SELECT id FROM account_account
                WHERE qbo_id IS NOT NULL AND qbo_id != 0
                AND code = '2615'
            """)
            row = ctx.env.cr.fetchone()
            if row:
                tax_payable = ctx.env["account.account"].browse(row[0])

        if not tax_payable:
            _logger.warning(
                "No GST/HST - QST Payable account found — "
                "tax repartition accounts not set"
            )
            return

        # Find all QBO-imported taxes (non-group, non-zero rate)
        qbo_taxes = ctx.env["account.tax"].search([
            "|",
            ("qbo_tax_id", "!=", False),
            ("qbo_tax_rate_id", "!=", False),
            ("amount_type", "!=", "group"),
        ])

        updated = 0
        for tax in qbo_taxes:
            for rep_line in tax.invoice_repartition_line_ids.filtered(
                lambda l: l.repartition_type == "tax" and not l.account_id
            ):
                rep_line.account_id = tax_payable.id
                updated += 1
            for rep_line in tax.refund_repartition_line_ids.filtered(
                lambda l: l.repartition_type == "tax" and not l.account_id
            ):
                rep_line.account_id = tax_payable.id
                updated += 1

        _logger.info(
            f"Set tax account {tax_payable.code} ({tax_payable.name}) "
            f"on {updated} repartition lines"
        )
