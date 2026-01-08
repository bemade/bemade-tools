import logging

from odoo import api, models

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="res.company",
    importer_name="res.company.importer",
    sap_source="oadm",
    depends_on=[],
    allow_multiprocessing=False,
)
class ResCompanyImporter(models.AbstractModel):
    _name = "res.company.importer"
    _description = "SAP Company Configuration Importer (OADM)"

    @ETL.extract("oadm")
    def extract_company(self, ctx: ETLContext):
        """Extract basic company configuration from SAP OADM.

        We currently only care about the country code for setting
        env.company.country_id.
        """
        ctx.cr.execute("SELECT * FROM oadm LIMIT 1")
        row = ctx.cr.dictfetchone()
        if not row:
            _logger.info("No OADM row found in SAP; skipping company import.")
            return {}
        return {"oadm": row}

    @ETL.transform()
    def transform_company(self, ctx: ETLContext, extracted):
        """Map SAP company data to Odoo res.company fields.

        Currently handles:
        - Country (OADM.Country -> company.country_id)
        - Name (OADM.CompnyName -> company.name)
        - Address (OADM.CompnyAddr -> company.street)
        - Phone (OADM.Phone1 -> company.phone)
        - Email (OADM.e_mail -> company.email)
        - Main currency (OADM.MainCurncy -> company.currency_id)
        - Tax ID (OADM.TaxIdNum -> company.vat)
        """
        data = extracted.get("extract_company") or {}
        oadm = data.get("oadm")
        if not oadm:
            return {}

        vals = {}

        # Country
        sap_country = (oadm.get("country") or "").strip()
        if sap_country:
            country = ctx.env["res.country"].search(
                [
                    ("code", "=", sap_country),
                ],
                limit=1,
            )
            if country:
                vals["country_id"] = country.id
            else:
                _logger.warning(
                    "No res.country found matching SAP country code %s; leaving company.country_id unchanged.",
                    sap_country,
                )
        else:
            _logger.info(
                "SAP OADM has no country set; leaving company.country_id unchanged."
            )

        # Basic identity/contact fields
        compny_name = (oadm.get("compnyname") or "").strip()
        if compny_name:
            vals["name"] = compny_name

        compny_addr = (oadm.get("compnyaddr") or "").strip()
        if compny_addr:
            vals["street"] = compny_addr

        phone1 = (oadm.get("phone1") or oadm.get("phone2") or "").strip()
        if phone1:
            vals["phone"] = phone1

        email = (oadm.get("e_mail") or "").strip()
        if email:
            vals["email"] = email

        # Main currency
        main_currency = (oadm.get("maincurncy") or "").strip()
        if main_currency:
            currency = ctx.env["res.currency"].search(
                [
                    ("name", "=", main_currency),
                    ("active", "in", [False, True]),
                ],
                limit=1,
            )
            if currency:
                vals["currency_id"] = currency.id
            else:
                _logger.warning(
                    "No res.currency found matching SAP MainCurncy %s; leaving company.currency_id unchanged.",
                    main_currency,
                )

        # Tax ID
        tax_id = (oadm.get("taxidnum") or "").strip()
        if tax_id:
            vals["vat"] = tax_id

        # Inventory costing method from SAP OADM.InvntSystm
        # A = Moving Average, S = Standard, F = FIFO
        invnt_system = (oadm.get("invntsystm") or "").strip().upper()
        cost_method_map = {
            "A": "average",  # Moving Average -> AVCO
            "S": "standard",  # Standard Price
            "F": "fifo",  # FIFO
        }
        vals["cost_method"] = cost_method_map.get(invnt_system, "fifo")

        # Perpetual vs Periodic inventory from SAP OADM.ContInvnt
        # Y = Perpetual (real_time), N = Periodic
        cont_invnt = (oadm.get("continvnt") or "").strip().upper()
        if cont_invnt == "Y":
            vals["inventory_valuation"] = "real_time"  # Perpetual
        else:
            vals["inventory_valuation"] = "periodic"  # Non-perpetual

        return vals

    @ETL.load()
    def load_company(self, ctx: ETLContext, transformed):
        """Apply company configuration to env.company and install localization."""
        vals = transformed.get("transform_company") or {}
        if not vals:
            _logger.info("No company values to apply from SAP; skipping.")
            return

        company = ctx.env.company
        _logger.info(
            "Updating company %s (ID %s) with values from SAP OADM: %s",
            company.name,
            company.id,
            vals,
        )
        company.write(vals)
