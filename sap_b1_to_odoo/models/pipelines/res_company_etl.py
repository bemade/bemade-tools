#
#    Bemade Inc.
#
#    Copyright (C) 2024-2025 Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""SAP B1 Company (OADM/OBPL) → Odoo res.company ETL pipeline."""
import re
import logging

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure-function address parser  (no Odoo env, fully unit-testable)
# ---------------------------------------------------------------------------

_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\S.*)$"
)


def parse_oadm_address(raw):
    """Parse SAP OADM.CompnyAddr blob into discrete address components.

    The blob is carriage-return / line-feed delimited.  Example::

        '33163 Braniff Avenue\\r\\rPueblo CO  81001-4832\\rUSA'

    Returns a dict with keys: street, street2, city, state, zip, country.
    All values are ``str`` or ``None``.  No Odoo environment is used; the
    caller is responsible for resolving state/country codes to IDs.

    Parsing rules:

    * Split on ``[\\r\\n]+``; strip and drop blank lines.
    * 0 lines → all None.
    * 1 line  → street only.
    * 2 lines → street = line[0], country = line[1].
    * 3+ lines:
      - street        = line[0]
      - country       = last line
      - penultimate   = parsed via ``City ST  zip`` regex (1+ spaces between
                        tokens; state token must match ``[A-Z]{2}``).  On
                        regex miss all three tokens remain None and the caller
                        should log a WARNING.
      - middle lines  = lines[1 : -2] (all between street and penultimate)
                        joined with ``', '`` → street2.  Empty when none.
    """
    result = {
        "street": None,
        "street2": None,
        "city": None,
        "state": None,
        "zip": None,
        "country": None,
    }
    if not raw:
        return result

    lines = [l.strip() for l in re.split(r"[\r\n]+", raw) if l.strip()]
    if not lines:
        return result

    if len(lines) == 1:
        result["street"] = lines[0]
        return result

    if len(lines) == 2:
        result["street"] = lines[0]
        result["country"] = lines[1]
        return result

    # 3+ lines
    result["street"] = lines[0]
    result["country"] = lines[-1]
    penultimate = lines[-2]
    middle = lines[1:-2]  # between street and penultimate (may be empty)

    m = _CITY_STATE_ZIP_RE.match(penultimate)
    if m:
        result["city"] = m.group("city")
        result["state"] = m.group("state")
        result["zip"] = m.group("zip")
    # On regex miss: city/state/zip stay None; caller logs WARNING.

    if middle:
        result["street2"] = ", ".join(middle)

    return result


# ---------------------------------------------------------------------------
# Country / state resolution helpers  (scoped lookups, not bulk-cached)
# ---------------------------------------------------------------------------


def _resolve_country(env, code):
    """Resolve a SAP country code to a res.country ID.

    Case-insensitive match on ``res.country.code``.

    :param env: Odoo environment.
    :param code: SAP country code string (e.g. ``'US'``, ``'CA'``).
    :returns: integer ID or ``False`` on miss.
    """
    if not code:
        return False
    country = env["res.country"].search(
        [("code", "=ilike", code.strip())],
        limit=1,
    )
    if country:
        return country.id
    _logger.warning(
        "res.company ETL: no res.country matches SAP code %r; country_id left unset.",
        code,
    )
    return False


def _resolve_state(env, country_id, value):
    """Resolve a SAP state value to a res.country.state ID.

    Tries case-insensitive code match first, then case-insensitive name match,
    both scoped to ``country_id`` to prevent cross-country collisions.

    :param env: Odoo environment.
    :param country_id: integer res.country ID (must be set; if 0/False the
        function returns False immediately without logging).
    :param value: SAP state code or name string (e.g. ``'CO'``, ``'Quebec'``).
    :returns: integer ID or ``False`` on miss.
    """
    if not country_id or not value:
        return False
    value = value.strip()
    domain_base = [("country_id", "=", country_id)]
    # Try code (case-insensitive)
    state = env["res.country.state"].search(
        domain_base + [("code", "=ilike", value)], limit=1
    )
    if state:
        return state.id
    # Try name (case-insensitive)
    state = env["res.country.state"].search(
        domain_base + [("name", "=ilike", value)], limit=1
    )
    if state:
        return state.id
    _logger.warning(
        "res.company ETL: no res.country.state matches SAP value %r "
        "within country_id=%s; state_id left unset.",
        value,
        country_id,
    )
    return False


# ---------------------------------------------------------------------------
# ETL pipeline
# ---------------------------------------------------------------------------


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
        """Extract basic company configuration from SAP OADM + optional OBPL.

        Fetches the single OADM row (there is always exactly one) plus the
        best-matching OBPL branch-place row (preferred structured address
        source).  OBPL selection:

        1. If ``OADM.MainBPLId`` exists in this SAP schema variant, use it.
        2. Else fall back to ``MIN(bplid)`` filtered by
           ``COALESCE(disabled,'N') <> 'Y'``.

        The OBPL row may be absent (e.g. RWI's staging DB has 0 OBPL rows);
        in that case ``obpl`` is ``None`` and the transform falls back to the
        OADM-structured columns + ``compnyaddr`` parser.
        """
        ctx.cr.execute(
            "SELECT compnyname, compnyaddr, country, state, btwcity, btwzip, "
            "phone1, phone2, e_mail, maincurncy, taxidnum, invntsystm, continvnt "
            "FROM oadm LIMIT 1"
        )
        row = ctx.cr.dictfetchone()
        if not row:
            _logger.info("No OADM row found in SAP; skipping company import.")
            return {}

        # --- OBPL selection (defensive against schema variants) ---
        obpl_row = None
        try:
            # Introspect for MainBPLId presence (SAP B1 schema variant guard)
            ctx.cr.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'oadm' "
                "AND lower(column_name) = 'mainbplid' LIMIT 1"
            )
            has_mainbplid = ctx.cr.fetchone() is not None

            if has_mainbplid:
                ctx.cr.execute(
                    "SELECT o.bplid, o.street, o.streetno, o.block, o.city, "
                    "o.state, o.zipcode, o.country "
                    "FROM obpl o "
                    "INNER JOIN oadm a ON o.bplid = a.mainbplid "
                    "WHERE COALESCE(o.disabled, 'N') <> 'Y' "
                    "LIMIT 1"
                )
            else:
                ctx.cr.execute(
                    "SELECT bplid, street, streetno, block, city, "
                    "state, zipcode, country "
                    "FROM obpl "
                    "WHERE COALESCE(disabled, 'N') <> 'Y' "
                    "ORDER BY bplid "
                    "LIMIT 1"
                )
            obpl_row = ctx.cr.dictfetchone()
        except Exception:
            _logger.warning(
                "res.company ETL: could not query OBPL; "
                "falling back to OADM-structured + parser.",
                exc_info=True,
            )

        return {"oadm": row, "obpl": obpl_row}

    @ETL.transform()
    def transform_company(self, ctx: ETLContext, extracted):
        """Map SAP company data to Odoo res.company fields.

        Address resolution order (first non-empty per field wins):

        1. OBPL row (structured, preferred) — when the row is usable (at least
           one of street/city/zipcode is populated).
        2. OADM structured columns: ``country``, ``state``, ``btwcity``,
           ``btwzip``.
        3. Parser fallback on ``OADM.compnyaddr`` blob.

        Non-address fields (name, phone, email, currency, vat, cost_method,
        inventory_valuation) are mapped from OADM as before.
        """
        data = extracted.get("extract_company") or {}
        oadm = data.get("oadm")
        if not oadm:
            return {}

        vals = {}

        # ------------------------------------------------------------------
        # Non-address identity / contact fields (unchanged from prior code)
        # ------------------------------------------------------------------

        compny_name = (oadm.get("compnyname") or "").strip()
        if compny_name:
            vals["name"] = compny_name

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
                    "res.company ETL: no res.currency matching SAP MainCurncy %r; "
                    "leaving company.currency_id unchanged.",
                    main_currency,
                )

        # Tax ID
        tax_id = (oadm.get("taxidnum") or "").strip()
        if tax_id:
            vals["vat"] = tax_id

        # Inventory costing method (A=AVCO, S=Standard, F=FIFO)
        invnt_system = (oadm.get("invntsystm") or "").strip().upper()
        cost_method_map = {"A": "average", "S": "standard", "F": "fifo"}
        vals["cost_method"] = cost_method_map.get(invnt_system, "fifo")

        # Perpetual vs Periodic inventory
        cont_invnt = (oadm.get("continvnt") or "").strip().upper()
        vals["inventory_valuation"] = "real_time" if cont_invnt == "Y" else "periodic"

        # ------------------------------------------------------------------
        # Address resolution: OBPL → OADM-structured → parser
        # ------------------------------------------------------------------

        # addr dict accumulates raw string tokens; IDs resolved at the end.
        addr = {
            "street": None,
            "street2": None,
            "city": None,
            "state_code": None,
            "zip": None,
            "country_code": None,
        }
        addr_source = "fallback: parsed CompnyAddr"

        # 1. Try OBPL (preferred structured source)
        obpl = data.get("obpl")
        obpl_usable = False
        if obpl:
            obpl_street = (obpl.get("street") or "").strip()
            obpl_city = (obpl.get("city") or "").strip()
            obpl_zip = (obpl.get("zipcode") or "").strip()
            obpl_usable = bool(obpl_street or obpl_city or obpl_zip)

        if obpl_usable:
            bplid = obpl.get("bplid", "?")
            addr["street"] = (obpl.get("street") or "").strip() or None
            obpl_block = (obpl.get("block") or "").strip()
            # street2 = block when non-empty (OBPL.Block is the suite / unit)
            addr["street2"] = obpl_block or None
            addr["city"] = obpl_city or None
            addr["state_code"] = (obpl.get("state") or "").strip() or None
            addr["zip"] = obpl_zip or None
            addr["country_code"] = (obpl.get("country") or "").strip() or None
            addr_source = f"OBPL bplid={bplid}"

        # 2. Promote OADM structured columns into any still-empty slot
        oadm_country = (oadm.get("country") or "").strip() or None
        oadm_state = (oadm.get("state") or "").strip() or None
        oadm_btwcity = (oadm.get("btwcity") or "").strip() or None
        oadm_btwzip = (oadm.get("btwzip") or "").strip() or None

        if addr["country_code"] is None and oadm_country:
            addr["country_code"] = oadm_country
        if addr["state_code"] is None and oadm_state:
            addr["state_code"] = oadm_state
        if addr["city"] is None and oadm_btwcity:
            addr["city"] = oadm_btwcity
        if addr["zip"] is None and oadm_btwzip:
            addr["zip"] = oadm_btwzip

        # 3. Parser fallback on compnyaddr blob (fills any remaining gaps)
        compnyaddr = oadm.get("compnyaddr") or ""
        parsed = parse_oadm_address(compnyaddr)

        if addr["street"] is None and parsed["street"]:
            addr["street"] = parsed["street"]
        if addr["street2"] is None and parsed["street2"]:
            addr["street2"] = parsed["street2"]
        if addr["city"] is None and parsed["city"]:
            addr["city"] = parsed["city"]
        if addr["zip"] is None and parsed["zip"]:
            addr["zip"] = parsed["zip"]
        if addr["country_code"] is None and parsed["country"]:
            addr["country_code"] = parsed["country"]
        if addr["state_code"] is None and parsed["state"]:
            addr["state_code"] = parsed["state"]

        # Warn when the penultimate line of the blob was unparsable (3+ line
        # blob but city/state/zip still unset after all three steps).
        blob_lines = [ln.strip() for ln in re.split(r"[\r\n]+", compnyaddr) if ln.strip()]
        if (
            len(blob_lines) >= 3
            and not obpl_usable
            and addr["city"] is None
            and addr["state_code"] is None
            and addr["zip"] is None
        ):
            _logger.warning(
                "res.company ETL: could not parse city/state/zip from "
                "CompnyAddr penultimate line %r; those fields left unset.",
                blob_lines[-2] if len(blob_lines) >= 2 else compnyaddr,
            )

        # 4. Resolve country and state IDs
        country_id = _resolve_country(ctx.env, addr["country_code"])
        state_id = _resolve_state(ctx.env, country_id, addr["state_code"])

        # 5. Write address fields into vals (empty/whitespace → False)
        vals["street"] = addr["street"] or False
        vals["street2"] = addr["street2"] or False
        vals["city"] = addr["city"] or False
        vals["zip"] = addr["zip"] or False
        vals["country_id"] = country_id or False
        vals["state_id"] = state_id or False

        _logger.info(
            "res.company ETL: address resolved via %s — "
            "street=%r city=%r state_id=%s zip=%r country_id=%s",
            addr_source,
            vals["street"],
            vals["city"],
            vals["state_id"],
            vals["zip"],
            vals["country_id"],
        )

        return vals

    @ETL.load()
    def load_company(self, ctx: ETLContext, transformed):
        """Apply company configuration to env.company."""
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
