# -*- coding: utf-8 -*-
import logging
from odoo import api, models, Command
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.tax",
    importer_name="account.tax.importer",
    sap_source="ostc",
    depends_on=[
        "res.company.importer",
        "account.account.importer",  # Need CoA for tax accounts
    ],
    allow_multiprocessing=False,
)
class AccountTaxImporter(models.AbstractModel):
    _name = "account.tax.importer"
    _description = "SAP Tax Code Importer (OSTC)"

    @ETL.extract("ostc")
    def extract_taxes(self, ctx: ETLContext):
        """Extract tax codes from SAP OSTC.

        Extracts all tax codes with their rates and validity flags.
        """
        ctx.cr.execute(
            """
            SELECT 
                code,
                name,
                rate,
                validforar,
                validforap,
                vatexempt,
                freight,
                lock
            FROM ostc
            ORDER BY code
        """
        )
        taxes = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(taxes)} tax codes from SAP OSTC")
        return {"taxes": taxes}

    @ETL.transform()
    def transform_taxes(self, ctx: ETLContext, extracted):
        """Transform SAP tax codes to Odoo account.tax vals.

        Maps SAP tax codes to Odoo taxes:
        - validforar='Y' → type_tax_use='sale'
        - validforap='Y' → type_tax_use='purchase'
        - Both 'Y' → create two taxes (one for sale, one for purchase)
        - rate → amount (convert to percentage)
        """
        data = extracted.get("extract_taxes") or {}
        sap_taxes = data.get("taxes", [])

        if not sap_taxes:
            _logger.info("No SAP taxes to transform")
            return []

        # Get existing taxes to avoid duplicates
        existing_taxes = ctx.env["account.tax"].search(
            [("company_id", "=", ctx.env.company.id)]
        )
        existing_codes = {(t.name, t.type_tax_use) for t in existing_taxes}

        tax_vals = []
        for sap_tax in sap_taxes:
            code = (sap_tax.get("code") or "").strip()
            name = (sap_tax.get("name") or code).strip()
            rate = float(sap_tax.get("rate") or 0.0)
            validforar = (sap_tax.get("validforar") or "N").strip().upper()
            validforap = (sap_tax.get("validforap") or "N").strip().upper()
            vatexempt = (sap_tax.get("vatexempt") or "N").strip().upper()
            locked = (sap_tax.get("lock") or "N").strip().upper()

            if not code:
                _logger.warning(f"Skipping tax with no code: {sap_tax}")
                continue

            # Determine if this is a sale tax, purchase tax, or both
            is_sale = validforar == "Y"
            is_purchase = validforap == "Y"

            if not is_sale and not is_purchase:
                _logger.debug(f"Skipping tax {code} - not valid for AR or AP")
                continue

            # Create sale tax
            if is_sale:
                sale_name = f"{name} (Sale)" if is_purchase else name
                if (sale_name, "sale") not in existing_codes:
                    vals = self._create_tax_vals(
                        ctx,
                        code,
                        sale_name,
                        rate,
                        "sale",
                        vatexempt == "Y",
                        locked == "Y",
                    )
                    tax_vals.append(vals)
                    existing_codes.add((sale_name, "sale"))  # Track in this batch

            # Create purchase tax
            if is_purchase:
                purchase_name = f"{name} (Purchase)" if is_sale else name
                if (purchase_name, "purchase") not in existing_codes:
                    vals = self._create_tax_vals(
                        ctx,
                        code,
                        purchase_name,
                        rate,
                        "purchase",
                        vatexempt == "Y",
                        locked == "Y",
                    )
                    tax_vals.append(vals)
                    existing_codes.add(
                        (purchase_name, "purchase")
                    )  # Track in this batch

        _logger.info(
            f"Transformed {len(tax_vals)} taxes from {len(sap_taxes)} SAP tax codes "
            f"(skipped {len(sap_taxes) - len(tax_vals)} existing/invalid)"
        )
        return tax_vals

    def _create_tax_vals(
        self, ctx, code, name, rate, type_tax_use, is_exempt, is_locked
    ):
        """Create tax vals dict for Odoo.

        Args:
            code: SAP tax code
            name: Tax name/description
            rate: Tax rate (percentage)
            type_tax_use: 'sale' or 'purchase'
            is_exempt: Whether this is an exempt tax
            is_locked: Whether tax is locked in SAP
        """
        # Determine tax group based on code prefix (as-is from SAP)
        # E.g., "CO" → "CO", "WY 01" → "WY", "NE 123" → "NE"
        code_parts = code.split()
        group_name = code_parts[0] if code_parts else "Other"

        # Get or create tax group using SAP prefix as-is
        tax_group = ctx.env["account.tax.group"].search(
            [
                ("name", "=", group_name),
                ("company_id", "=", ctx.env.company.id),
            ],
            limit=1,
        )

        if not tax_group:
            tax_group = ctx.env["account.tax.group"].create(
                {
                    "name": group_name,
                    "company_id": ctx.env.company.id,
                }
            )
            _logger.debug(f"Created tax group '{group_name}' (ID: {tax_group.id})")

        vals = {
            "name": name,
            "amount": rate,
            "amount_type": "percent",
            "type_tax_use": type_tax_use,
            "company_id": ctx.env.company.id,
            "tax_group_id": tax_group.id,  # Required in Odoo 19
            "active": not is_locked,  # Inactive if locked in SAP
            "sap_tax_code": code,  # Store SAP code for linking invoice lines
            "description": code,  # Also in description for visibility
        }

        # For exempt taxes, set amount to 0
        if is_exempt:
            vals["amount"] = 0.0

        return vals

    @ETL.load()
    def load_taxes(self, ctx: ETLContext, transformed):
        """Create account.tax records from transformed data."""
        tax_vals = transformed.get("transform_taxes", [])

        if not tax_vals:
            _logger.info("No new taxes to create")
            return

        taxes = ctx.env["account.tax"].create(tax_vals)
        _logger.info(
            f"Created {len(taxes)} account.tax records: "
            f"{len([t for t in taxes if t.type_tax_use == 'sale'])} sale, "
            f"{len([t for t in taxes if t.type_tax_use == 'purchase'])} purchase"
        )
