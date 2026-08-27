"""Sage 50 `tcustomr` + `tvendor` -> `res.partner`.

Sage keeps customers and vendors in two unrelated tables with independent id
sequences, and a company that is both appears in each. Odoo wants one
partner, so the two are merged on a hard-folded name and every merge is
reported rather than trusted.

Three clean-ups happen here rather than in Odoo, because they are properties
of the Sage data and not of the target: phone numbers typed into the customer
*name* field, provinces and countries written free-hand, and postal codes
typed into the country column. All three are near-universal in Sage files —
none of these fields is validated on entry.
"""

import logging
import re
import unicodedata

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)

#: Province spellings, folded to lower case and stripped of accents, mapped
#: to the `res.country.state` code.
PROVINCE_MAP = {
    "qc": "QC", "quebec": "QC", "pq": "QC", "que": "QC", "quebed": "QC",
    "on": "ON", "ontario": "ON",
    "ab": "AB", "alberta": "AB",
    "bc": "BC", "britishcolumbia": "BC",
    "mb": "MB", "manitoba": "MB",
    "nb": "NB", "nouveaubrunswick": "NB", "newbrunswick": "NB",
    "ns": "NS", "novascotia": "NS", "nouvelleecosse": "NS",
    "pe": "PE", "pei": "PE",
    "nl": "NL", "newfoundland": "NL",
    "sk": "SK", "saskatchewan": "SK",
    "nt": "NT", "nu": "NU", "yt": "YT",
}

#: A trailing phone number in the name field. Deliberately strict: it must be
#: at the end, and it must look like a North American number, so that an
#: account number ("Compass Canada #40965") and a Québec company number
#: ("2955-3039 Québec inc.") are left alone.
PHONE_IN_NAME = re.compile(
    r"""
    \s{1,}                          # separated from the name
    (?:t[ée]l\.?\s*:?\s*)?          # optional "Tel:" / "Tél."
    (?:1[-\s.]?)?                   # optional long-distance 1
    (?:\(\d{3}\)|\d{3})             # area code, bracketed or not
    [-\s.]?\d{3}[-\s.]?\d{4}        # exchange and line
    # Extension, with or without a keyword. A bare trailing group is safe
    # here because it can only match after a full phone number: the Québec
    # company numbers that also live in these names are 4+4 digits and never
    # match the 3+3+4 shape above.
    (?:\s*(?:p\.?|poste|ext\.?)?\s*\d{2,5})?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

POSTAL_CODE = re.compile(r"[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d")

#: Sage writes a placeholder rather than leaving the credit limit blank.
NO_CREDIT_LIMIT = -1.0


def normalise_key(name: str) -> str:
    """Fold a company name hard enough to match across the two tables."""
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.lower()
    folded = re.sub(
        r"\b(inc|ltd|ltee|ltée|enr|senc|cie|co|corp)\b\.?", "", folded
    )
    return re.sub(r"[^a-z0-9]+", "", folded)


@ETL.pipeline(
    target_model="res.partner",
    importer_name="sage.partner.importer",
    sap_source="tcustomr",
    depends_on=["sage.account.importer", "sage.pricelist.importer"],
    allow_multiprocessing=False,
)
class SagePartnerImporter(models.AbstractModel):
    _name = "sage.partner.importer"
    _description = "Sage 50 Partner Importer"

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------
    def _province_map(self) -> dict:
        return PROVINCE_MAP

    def _split_phone_from_name(self, name: str) -> tuple:
        match = PHONE_IN_NAME.search(name or "")
        if not match:
            return (name or "").strip(), None
        phone = match.group(0).strip()
        phone = re.sub(
            r"^(?:t[ée]l\.?\s*:?\s*)", "", phone, flags=re.IGNORECASE
        )
        return name[: match.start()].strip(), re.sub(r"\s+", " ", phone).strip()

    def _clean_country(self, value: str):
        value = (value or "").strip()
        if not value or POSTAL_CODE.fullmatch(value):
            return None
        return "CA" if value.lower().startswith("canada") else value

    def _clean_province(self, value: str):
        raw = (value or "").strip()
        if not raw:
            return None
        folded = unicodedata.normalize("NFKD", raw)
        folded = "".join(c for c in folded if not unicodedata.combining(c))
        folded = re.sub(r"[^a-z]", "", folded.lower())
        code = self._province_map().get(folded)
        # An unmapped spelling is surfaced, not silently dropped: it is
        # usually a province nobody thought of rather than junk.
        return code or ("?" + raw)

    def _default_country_code(self) -> str:
        return "CA"

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    @ETL.extract("tcustomr")
    def extract_customers(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr,
            """select lId, sName, sCntcName, sStreet1, sStreet2, sCity,
                      sProvState, sCountry, sPostalZip, sPhone1, sPhone2,
                      sEmail, sWebSite, bInactive, nNetDay, dCrLimit,
                      lPrcListId, lAcDefRev
                 from tcustomr order by lId""",
        )

    @ETL.extract("tvendor")
    def extract_vendors(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr,
            """select lId, sName, sCntcName, sStreet1, sStreet2, sCity,
                      sProvState, sCountry, sPostalZip, sPhone1, sPhone2,
                      sEmail, sWebSite, bInactive, nNetDay, lTaxCode,
                      lAcDefExp, sTaxId
                 from tvendor order by lId""",
        )

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------
    def _base_record(self, row: dict) -> dict:
        name, phone_from_name = self._split_phone_from_name(row["sName"])
        return {
            "name": name,
            "match_key": normalise_key(name),
            "contact_name": (row["sCntcName"] or "").strip() or None,
            "street": (row["sStreet1"] or "").strip() or None,
            "street2": (row["sStreet2"] or "").strip() or None,
            "city": (row["sCity"] or "").strip() or None,
            "state_code": self._clean_province(row["sProvState"]),
            "country_code": self._clean_country(row["sCountry"]),
            "zip": (row["sPostalZip"] or "").strip() or None,
            # Odoo 19 dropped `mobile` from res.partner, so Sage's second
            # number is only kept when the first is blank rather than
            # silently discarded.
            "phone": ((row["sPhone1"] or "").strip() or phone_from_name
                      or (row["sPhone2"] or "").strip() or None),
            "email": (row["sEmail"] or "").strip() or None,
            "website": (row["sWebSite"] or "").strip() or None,
            "active": not row["bInactive"],
            "payment_term_days": row["nNetDay"] or None,
            "phone_recovered_from_name": bool(phone_from_name),
        }

    @ETL.transform()
    def transform_partners(self, ctx: ETLContext, extracted: dict) -> list:
        partners, order = {}, []
        for row in extracted["extract_customers"]:
            record = self._base_record(row)
            record.update({
                "sage_customer_id": row["lId"],
                "sage_vendor_id": 0,
                "customer_rank": 1,
                "supplier_rank": 0,
                "credit_limit": (
                    0.0 if row["dCrLimit"] == NO_CREDIT_LIMIT
                    else row["dCrLimit"]
                ),
                "sage_pricelist_id": row["lPrcListId"] or None,
            })
            key = f"c{row['lId']}"
            partners[key] = record
            order.append(key)

        by_key = {
            record["match_key"]: key
            for key, record in partners.items() if record["match_key"]
        }
        merged = 0
        for row in extracted["extract_vendors"]:
            record = self._base_record(row)
            existing_key = by_key.get(record["match_key"])
            if existing_key:
                # The same company on both sides: one partner, both ranks.
                # The customer record wins on the address, because that is the
                # one a human maintains.
                target = partners[existing_key]
                target["supplier_rank"] = 1
                target["sage_vendor_id"] = row["lId"]
                merged += 1
                _logger.info(
                    "Merging Sage vendor %r into customer %r",
                    record["name"], target["name"],
                )
                continue
            record.update({
                "sage_customer_id": 0,
                "sage_vendor_id": row["lId"],
                "customer_rank": 0,
                "supplier_rank": 1,
                "vat": (row["sTaxId"] or "").strip() or None,
            })
            key = f"v{row['lId']}"
            partners[key] = record
            order.append(key)

        _logger.info(
            "Sage partners: %s records, %s companies present on both sides.",
            len(order), merged,
        )
        return [partners[key] for key in order]

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @ETL.load()
    def load_partners(self, ctx: ETLContext, transformed: dict) -> None:
        Partner = ctx.env["res.partner"]
        company = ctx.env["res.company"].browse(ctx.get_config("company_id"))
        states = {
            (state.code, state.country_id.code): state.id
            for state in ctx.env["res.country.state"].search([])
        }
        countries = {
            country.code: country.id
            for country in ctx.env["res.country"].search([])
        }
        default_country = countries.get(self._default_country_code())
        terms = self._payment_terms_by_days(ctx)
        pricelists = ctx.env["sage.pricelist.importer"].sage_pricelist_map(ctx)

        created = updated = pinned = 0
        for record in transformed["transform_partners"]:
            record = dict(record)
            record.pop("match_key", None)
            record.pop("phone_recovered_from_name", None)
            record.pop("contact_name", None)
            sage_pricelist = record.pop("sage_pricelist_id", None)
            days = record.pop("payment_term_days", None)
            state_code = record.pop("state_code", None)
            country_code = record.pop("country_code", None)

            if state_code and state_code.startswith("?"):
                ctx.report.warning(
                    f"Unmapped province {state_code[1:]!r}",
                    source_ref=record["name"],
                )
                state_code = None
            country_id = countries.get(country_code, default_country)
            record["country_id"] = country_id
            record["state_id"] = states.get((state_code, country_code or "CA"))
            record["company_type"] = "company"
            if days and days in terms:
                record["property_payment_term_id"] = terms[days]
            elif days:
                ctx.report.warning(
                    f"No payment term of {days} days to assign",
                    source_ref=record["name"],
                )

            domain = (
                [("sage_customer_id", "=", record["sage_customer_id"])]
                if record["sage_customer_id"]
                else [("sage_vendor_id", "=", record["sage_vendor_id"])]
            )
            partner = Partner.with_context(active_test=False).search(
                domain, limit=1
            )
            if partner:
                partner.write(record)
                updated += 1
            else:
                partner = Partner.create(record)
                created += 1
            ctx.report.success()

            pricelist_id = pricelists.get(sage_pricelist)
            if pricelist_id:
                # Write the *stored* company-dependent field, not the computed
                # `property_product_pricelist`. Its inverse stores nothing when
                # the value happens to equal the country fallback, so the
                # partner reads back correct and yet has no pricelist pinned —
                # and silently follows the fallback the day a new pricelist is
                # created.
                partner.with_company(
                    company
                ).specific_property_product_pricelist = pricelist_id
                pinned += 1

        _logger.info(
            "Sage partners: %s created, %s updated, %s pinned to a pricelist.",
            created, updated, pinned,
        )

    def _payment_terms_by_days(self, ctx: ETLContext) -> dict:
        """Net-days -> `account.payment.term` id, for the simple terms.

        Only terms made of a single balance line are considered, because only
        those mean "net N days" unambiguously. Nothing is created: inventing a
        payment term to match a number in Sage would put a term in front of
        the client that they never agreed to.
        """
        mapping = {}
        for term in ctx.env["account.payment.term"].search([
            ("company_id", "in", (ctx.get_config("company_id"), False)),
        ]):
            if len(term.line_ids) != 1:
                continue
            mapping.setdefault(term.line_ids.nb_days, term.id)
        return mapping
