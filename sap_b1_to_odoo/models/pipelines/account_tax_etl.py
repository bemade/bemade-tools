# -*- coding: utf-8 -*-
import logging
from collections import defaultdict

from odoo import models, Command
from odoo.addons.etl_framework import ETL, ETLContext

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
        """Extract tax codes from SAP OSTC with per-jurisdiction components from TAX1."""
        ctx.cr.execute(
            """
            SELECT code, name, rate, validforar, validforap, vatexempt, lock
            FROM ostc
            ORDER BY code
            """
        )
        taxes = ctx.cr.dictfetchall()

        # Most-recent vatpercent + taxacct per (taxcode, stacode) from TAX1.
        # crditdebit='C' = tax collected (sale); crditdebit='D' = tax paid (purchase).
        ctx.cr.execute(
            """
            SELECT DISTINCT ON (t.taxcode, t.stacode, t.crditdebit)
                t.taxcode,
                t.stacode,
                t.crditdebit,
                t.taxacct,
                a.formatcode AS acct_formatcode,
                t.vatpercent
            FROM tax1 t
            JOIN oact a ON t.taxacct = a.acctcode
            WHERE t.vatsum <> 0
            ORDER BY t.taxcode, t.stacode, t.crditdebit, t.absentry DESC
            """
        )
        components_raw = ctx.cr.dictfetchall()

        # Group components by (taxcode, crditdebit)
        components = defaultdict(list)
        for row in components_raw:
            key = (row["taxcode"], row["crditdebit"])
            components[key].append({
                "acct_formatcode": row["acct_formatcode"].strip(),
                "vatpercent": float(row["vatpercent"] or 0),
            })

        _logger.info(
            "Extracted %d tax codes, %d tax components from SAP",
            len(taxes), len(components_raw),
        )
        return {"taxes": taxes, "components": components}

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
        components = data.get("components", {})

        if not sap_taxes:
            _logger.info("No SAP taxes to transform")
            return []

        # Build account lookup: formatcode (stripped) → account_id
        accounts = ctx.env["account.account"].search_read(
            [("sap_acct_code", "!=", False)],
            ["id", "sap_acct_code"],
        )
        acct_by_code = {a["sap_acct_code"].strip(): a["id"] for a in accounts}

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

            is_sale = validforar == "Y"
            is_purchase = validforap == "Y"

            if not is_sale and not is_purchase:
                _logger.debug(f"Skipping tax {code} - not valid for AR or AP")
                continue

            if is_sale:
                sale_name = f"{name} (Sale)" if is_purchase else name
                if (sale_name, "sale") not in existing_codes:
                    sale_components = components.get((code, "C"), [])
                    vals = self._create_tax_vals(
                        ctx, code, sale_name, rate, "sale",
                        vatexempt == "Y", locked == "Y",
                        sale_components, acct_by_code,
                    )
                    tax_vals.append(vals)
                    existing_codes.add((sale_name, "sale"))

            if is_purchase:
                purchase_name = f"{name} (Purchase)" if is_sale else name
                if (purchase_name, "purchase") not in existing_codes:
                    purchase_components = components.get((code, "D"), [])
                    vals = self._create_tax_vals(
                        ctx, code, purchase_name, rate, "purchase",
                        vatexempt == "Y", locked == "Y",
                        purchase_components, acct_by_code,
                    )
                    tax_vals.append(vals)
                    existing_codes.add((purchase_name, "purchase"))

        _logger.info(
            f"Transformed {len(tax_vals)} taxes from {len(sap_taxes)} SAP tax codes "
            f"(skipped {len(sap_taxes) - len(tax_vals)} existing/invalid)"
        )
        return tax_vals

    def _create_tax_vals(
        self, ctx, code, name, rate, type_tax_use, is_exempt, is_locked,
        components, acct_by_code,
    ):
        """Create tax vals dict for Odoo including per-jurisdiction repartition lines."""
        code_parts = code.split()
        group_name = code_parts[0] if code_parts else "Other"

        tax_group = ctx.env["account.tax.group"].search(
            [("name", "=", group_name), ("company_id", "=", ctx.env.company.id)],
            limit=1,
        )
        if not tax_group:
            tax_group = ctx.env["account.tax.group"].create(
                {"name": group_name, "company_id": ctx.env.company.id}
            )

        effective_rate = 0.0 if is_exempt else rate

        vals = {
            "name": name,
            "amount": effective_rate,
            "amount_type": "percent",
            "type_tax_use": type_tax_use,
            "company_id": ctx.env.company.id,
            "tax_group_id": tax_group.id,
            "active": not is_locked,
            "sap_tax_code": code,
            "description": code,
        }

        # Build repartition lines from TAX1 component data.
        # factor_percent = component_rate / total_rate * 100 (split of total tax amount).
        # Fall back to a single unallocated tax line if no component data.
        repline_vals = self._build_repartition_line_vals(
            components, effective_rate, acct_by_code,
        )
        vals["invoice_repartition_line_ids"] = repline_vals
        vals["refund_repartition_line_ids"] = repline_vals

        return vals

    def _build_repartition_line_vals(self, components, total_rate, acct_by_code):
        """Build invoice_repartition_line_ids vals from TAX1 component list."""
        lines = [Command.create({"repartition_type": "base", "factor_percent": 100})]

        if not components or total_rate == 0:
            # No component data — single tax line with no account (will be fixed later)
            lines.append(Command.create({
                "repartition_type": "tax",
                "factor_percent": 100,
            }))
            return lines

        # Deduplicate components by account (same account can appear via different
        # stacode entries; sum their rates).
        by_account = defaultdict(float)
        for comp in components:
            fmtcode = comp["acct_formatcode"]
            by_account[fmtcode] += comp["vatpercent"]

        total_component_rate = sum(by_account.values())
        if total_component_rate == 0:
            lines.append(Command.create({
                "repartition_type": "tax",
                "factor_percent": 100,
            }))
            return lines

        for fmtcode, comp_rate in by_account.items():
            account_id = acct_by_code.get(fmtcode)
            factor = round(comp_rate / total_component_rate * 100, 6)
            repline = {
                "repartition_type": "tax",
                "factor_percent": factor,
            }
            if account_id:
                repline["account_id"] = account_id
            else:
                _logger.warning(
                    "No Odoo account found for tax component '%s'", fmtcode,
                )
            lines.append(Command.create(repline))

        return lines

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
