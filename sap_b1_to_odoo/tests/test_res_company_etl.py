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
"""Tests for sap_b1_to_odoo res_company_etl.

Acceptance criteria:
===================

TestParseOadmAddress (pure-function, no DB):
1. (test_rwi_sample_blob) RWI sample blob round-trip: correct street/city/state/zip/country.
2. (test_malformed_penultimate) Malformed penultimate line: street set, city/state/zip None.
3. (test_empty_input) Empty/whitespace/None input: all six keys are None.
4. (test_mixed_newline_separators) Mixed \\n/\\r separators parse correctly with street2.
5. (test_multi_space_city_state_zip) Multi-space city/state/zip separator (req §7).

TestResCompanyEtlIntegration (TransactionCase with stubbed SAP cursor):
6.  (test_obpl_happy_path) OBPL fully populated → all address fields set; INFO log names bplid.
7.  (test_state_by_code_us_colorado) State lookup by code 'co' (lowercase) → Colorado.
8.  (test_state_by_code_canada_qc) State lookup by code 'QC' → Quebec province.
9.  (test_state_by_name_canada_quebec) State lookup by name 'Quebec' → Quebec province.
10. (test_country_not_found) Country code 'ZZ' → country_id False, state_id False, WARNING.
11. (test_state_not_found_in_country) Country 'US', state 'ZZ' → country_id set, state_id False, WARNING.
12. (test_obpl_empty_fallback_to_parser) OBPL None → OADM-structured + parser; INFO names fallback.
13. (test_parser_fallback_malformed_penultimate) OBPL empty, malformed blob → WARNING, street set, city/state_id/zip False.
14. (test_non_address_fields_preserved) name/phone/email/currency_id/vat/cost_method/inventory_valuation all present.
15. (test_reimport_overwrites_stale_street) Re-import overwrites a stale street value.
"""

from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase, TransactionCase
from odoo.tests import tagged

from odoo.addons.sap_b1_to_odoo.models.pipelines.res_company_etl import (
    parse_oadm_address,
    _resolve_country,
    _resolve_state,
)
from odoo.addons.etl_framework import ETLContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_cursor(oadm_row, obpl_row=None, has_mainbplid=False):
    """Build a mock SAP cursor that returns canned dictfetchone results.

    Call order in extract_company:
    1. SELECT … FROM oadm  → oadm_row
    2. SELECT 1 FROM information_schema.columns … (mainbplid probe) → row or None
    3. SELECT … FROM obpl  → obpl_row

    fetchone() on step 2 returns (1,) if has_mainbplid else None.
    dictfetchone() on step 3 returns obpl_row.
    """
    cursor = MagicMock()

    _call_count = [0]  # mutable closure counter

    def _dictfetchone():
        _call_count[0] += 1
        if _call_count[0] == 1:
            return oadm_row
        # 3rd call is OBPL dictfetchone
        if _call_count[0] == 3:
            return obpl_row
        return None  # shouldn't be reached

    def _fetchone():
        # Called after the information_schema query (call #2)
        return (1,) if has_mainbplid else None

    cursor.dictfetchone.side_effect = _dictfetchone
    cursor.fetchone.side_effect = _fetchone
    return cursor


def _make_ctx(env, oadm_row, obpl_row=None, has_mainbplid=False):
    """Build an ETLContext with the given Odoo env and a stubbed SAP cursor."""
    cursor = _make_fake_cursor(oadm_row, obpl_row=obpl_row, has_mainbplid=has_mainbplid)
    return ETLContext(cr=cursor, env=env)


_FULL_OADM = {
    "compnyname": "Test Co",
    "compnyaddr": "1 Main St\rPueblo CO 81001\rUSA",
    "country": "US",
    "state": "CO",
    "btwcity": "",
    "btwzip": "",
    "phone1": "555-1234",
    "phone2": "",
    "e_mail": "test@test.com",
    "maincurncy": "USD",
    "taxidnum": "12-3456789",
    "invntsystm": "F",
    "continvnt": "Y",
}

_RWI_OADM = {
    "compnyname": "Refractories West Inc",
    "compnyaddr": "33163 Braniff Avenue\r\rPueblo CO  81001-4832\rUSA",
    "country": "US",
    "state": "CO",
    "btwcity": "",
    "btwzip": "",
    "phone1": "",
    "phone2": "",
    "e_mail": "",
    "maincurncy": "USD",
    "taxidnum": "",
    "invntsystm": "F",
    "continvnt": "N",
}


# ---------------------------------------------------------------------------
# Pure-function tests — no DB required
# ---------------------------------------------------------------------------

# These are pure-function tests (no DB).  Use BaseCase so Odoo 19's tag
# selector picks them up as "standard" tests.  No DB access occurs here.


@tagged("res_company_etl")
class TestParseOadmAddress(BaseCase):
    """Pure-function tests for parse_oadm_address.  No Odoo DB required."""

    def test_rwi_sample_blob(self):
        """§13.5 — RWI sample blob round-trip."""
        result = parse_oadm_address("33163 Braniff Avenue\r\rPueblo CO  81001-4832\rUSA")
        self.assertEqual(result["street"], "33163 Braniff Avenue")
        self.assertIsNone(result["street2"])
        self.assertEqual(result["city"], "Pueblo")
        self.assertEqual(result["state"], "CO")
        self.assertEqual(result["zip"], "81001-4832")
        self.assertEqual(result["country"], "USA")

    def test_malformed_penultimate(self):
        """§13.6 — Malformed penultimate line leaves city/state/zip None."""
        result = parse_oadm_address("123 Main St\rsomething random\rUSA")
        self.assertEqual(result["street"], "123 Main St")
        self.assertIsNone(result["city"])
        self.assertIsNone(result["state"])
        self.assertIsNone(result["zip"])
        self.assertEqual(result["country"], "USA")

    def test_empty_input(self):
        """Empty / whitespace / blank-lines input → all None."""
        for raw in ("", "   ", "\r\r\r", "\n\n\n", "\r\n"):
            with self.subTest(raw=repr(raw)):
                result = parse_oadm_address(raw)
                for key in ("street", "street2", "city", "state", "zip", "country"):
                    self.assertIsNone(result[key], f"{key} must be None for {raw!r}")

    def test_mixed_newline_separators(self):
        """Mixed \\n / \\r separators; street2 from middle line."""
        result = parse_oadm_address("10 A St\nApt 5\nPueblo CO 81001\nUSA")
        self.assertEqual(result["street"], "10 A St")
        self.assertEqual(result["street2"], "Apt 5")
        self.assertEqual(result["city"], "Pueblo")
        self.assertEqual(result["state"], "CO")
        self.assertEqual(result["zip"], "81001")
        self.assertEqual(result["country"], "USA")

    def test_multi_space_city_state_zip(self):
        """§7 — Multi-space separator between city/state/zip tokens."""
        result = parse_oadm_address("1 Main\rPueblo   CO     81001-4832\rUSA")
        self.assertEqual(result["city"], "Pueblo")
        self.assertEqual(result["state"], "CO")
        self.assertEqual(result["zip"], "81001-4832")


# ---------------------------------------------------------------------------
# Integration tests — TransactionCase with stubbed SAP cursor
# ---------------------------------------------------------------------------


@tagged("-at_install", "post_install", "res_company_etl")
class TestResCompanyEtlIntegration(TransactionCase):
    """Integration tests for ResCompanyImporter.transform_company.

    Uses a real Odoo env (so res.country / res.country.state look-ups work)
    and a fake SAP cursor returning canned dictfetchone results.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # We need a company for load_company; use env.company directly.
        cls.company = cls.env.company

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_transform(self, oadm_row, obpl_row=None, has_mainbplid=False):
        """Run extract + transform; return (importer, extracted, vals)."""
        importer = self.env["res.company.importer"]
        ctx = _make_ctx(self.env, oadm_row, obpl_row=obpl_row, has_mainbplid=has_mainbplid)

        # Simulate extract by rebuilding the extracted dict manually
        # (we can't call extract_company because it needs a real SAP cursor;
        # instead we call transform_company directly with the canned data).
        extracted = {
            "extract_company": {
                "oadm": oadm_row,
                "obpl": obpl_row,
            }
        }
        vals = importer.transform_company(ctx, extracted)
        return vals

    def _us_country_id(self):
        return self.env.ref("base.us").id

    def _co_state_id(self):
        """Colorado state ID."""
        us = self.env.ref("base.us")
        return self.env["res.country.state"].search(
            [("country_id", "=", us.id), ("code", "=", "CO")], limit=1
        ).id

    def _ca_country_id(self):
        return self.env.ref("base.ca").id

    def _qc_state_id(self):
        """Quebec province ID."""
        ca = self.env.ref("base.ca")
        return self.env["res.country.state"].search(
            [("country_id", "=", ca.id), ("code", "=", "QC")], limit=1
        ).id

    # ------------------------------------------------------------------
    # Test 6: Happy path — OBPL fully populated
    # ------------------------------------------------------------------

    def test_obpl_happy_path(self):
        """§13.1 — OBPL row with all fields → partner has full address."""
        obpl = {
            "bplid": 1,
            "street": "1 Main",
            "streetno": "",
            "block": "Suite 2",
            "city": "Pueblo",
            "state": "CO",
            "zipcode": "81001",
            "country": "US",
        }
        oadm = dict(_FULL_OADM, country="US", state="", btwcity="", btwzip="")
        log_calls = []
        with patch(
            "odoo.addons.sap_b1_to_odoo.models.pipelines.res_company_etl._logger"
        ) as mock_logger:
            mock_logger.info.side_effect = lambda msg, *args, **kw: log_calls.append(
                msg % args if args else msg
            )
            vals = self._run_transform(oadm, obpl_row=obpl)

        self.assertEqual(vals["street"], "1 Main")
        self.assertEqual(vals["street2"], "Suite 2")
        self.assertEqual(vals["city"], "Pueblo")
        self.assertEqual(vals["zip"], "81001")
        self.assertEqual(vals["country_id"], self._us_country_id())
        self.assertEqual(vals["state_id"], self._co_state_id())
        # INFO log must name the bplid
        self.assertTrue(
            any("bplid=1" in msg for msg in log_calls),
            "INFO log must mention OBPL bplid=1",
        )

    # ------------------------------------------------------------------
    # Tests 7-9: State lookup variants
    # ------------------------------------------------------------------

    def test_state_by_code_us_colorado(self):
        """§13.2a — State 'co' (lowercase) resolves to Colorado."""
        obpl = {
            "bplid": 2,
            "street": "1 Test",
            "streetno": "",
            "block": "",
            "city": "Denver",
            "state": "co",
            "zipcode": "80203",
            "country": "US",
        }
        oadm = dict(_FULL_OADM, country="US", state="", btwcity="", btwzip="")
        vals = self._run_transform(oadm, obpl_row=obpl)
        self.assertEqual(vals["state_id"], self._co_state_id())

    def test_state_by_code_canada_qc(self):
        """§13.2b — State 'QC' resolves to Quebec province (Canada)."""
        obpl = {
            "bplid": 3,
            "street": "100 Rue Test",
            "streetno": "",
            "block": "",
            "city": "Montreal",
            "state": "QC",
            "zipcode": "H3A 1A1",
            "country": "CA",
        }
        oadm = dict(_FULL_OADM, country="CA", state="", btwcity="", btwzip="")
        vals = self._run_transform(oadm, obpl_row=obpl)
        self.assertEqual(vals["country_id"], self._ca_country_id())
        self.assertEqual(vals["state_id"], self._qc_state_id())

    def test_state_by_name_canada_quebec(self):
        """§13.2c — State 'Quebec' (name) resolves to Quebec province."""
        obpl = {
            "bplid": 4,
            "street": "100 Rue Test",
            "streetno": "",
            "block": "",
            "city": "Montreal",
            "state": "Quebec",
            "zipcode": "H3A 1A1",
            "country": "CA",
        }
        oadm = dict(_FULL_OADM, country="CA", state="", btwcity="", btwzip="")
        vals = self._run_transform(oadm, obpl_row=obpl)
        self.assertEqual(vals["country_id"], self._ca_country_id())
        self.assertEqual(vals["state_id"], self._qc_state_id())

    # ------------------------------------------------------------------
    # Test 10: Country code not found
    # ------------------------------------------------------------------

    def test_country_not_found(self):
        """§13.3 — Unknown country 'ZZ' → country_id False, state_id False, WARNING."""
        obpl = {
            "bplid": 5,
            "street": "1 Test",
            "streetno": "",
            "block": "",
            "city": "Nowhere",
            "state": "XX",
            "zipcode": "00000",
            "country": "ZZ",
        }
        oadm = dict(_FULL_OADM, country="ZZ", state="", btwcity="", btwzip="")
        with self.assertLogs("odoo.addons.sap_b1_to_odoo.models.pipelines.res_company_etl", level="WARNING") as cm:
            vals = self._run_transform(oadm, obpl_row=obpl)
        self.assertFalse(vals.get("country_id"))
        self.assertFalse(vals.get("state_id"))
        self.assertTrue(
            any("ZZ" in msg for msg in cm.output),
            "WARNING must mention the bad country code ZZ",
        )

    # ------------------------------------------------------------------
    # Test 11: State not found within resolved country
    # ------------------------------------------------------------------

    def test_state_not_found_in_country(self):
        """§13.4 — Country 'US' found, state 'ZZ' not found → WARNING, state_id False."""
        obpl = {
            "bplid": 6,
            "street": "1 Test",
            "streetno": "",
            "block": "",
            "city": "Anytown",
            "state": "ZZ",
            "zipcode": "12345",
            "country": "US",
        }
        oadm = dict(_FULL_OADM, country="US", state="", btwcity="", btwzip="")
        with self.assertLogs("odoo.addons.sap_b1_to_odoo.models.pipelines.res_company_etl", level="WARNING") as cm:
            vals = self._run_transform(oadm, obpl_row=obpl)
        self.assertEqual(vals["country_id"], self._us_country_id())
        self.assertFalse(vals.get("state_id"))
        self.assertTrue(
            any("ZZ" in msg for msg in cm.output),
            "WARNING must mention the bad state code ZZ",
        )

    # ------------------------------------------------------------------
    # Test 12: OBPL empty → fallback to OADM-structured + parser
    # ------------------------------------------------------------------

    def test_obpl_empty_fallback_to_parser(self):
        """§13.5 — No OBPL row → OADM state/country + parser for street/city/zip."""
        oadm = dict(_RWI_OADM)
        log_calls = []
        with patch(
            "odoo.addons.sap_b1_to_odoo.models.pipelines.res_company_etl._logger"
        ) as mock_logger:
            mock_logger.info.side_effect = lambda msg, *args, **kw: log_calls.append(
                msg % args if args else msg
            )
            vals = self._run_transform(oadm, obpl_row=None)

        self.assertEqual(vals["street"], "33163 Braniff Avenue")
        self.assertEqual(vals["city"], "Pueblo")
        self.assertEqual(vals["zip"], "81001-4832")
        self.assertEqual(vals["country_id"], self._us_country_id())
        self.assertEqual(vals["state_id"], self._co_state_id())
        # INFO log must name the fallback path
        self.assertTrue(
            any("fallback: parsed CompnyAddr" in msg for msg in log_calls),
            "INFO log must mention 'fallback: parsed CompnyAddr'",
        )

    # ------------------------------------------------------------------
    # Test 13: Parser fallback with malformed penultimate
    # ------------------------------------------------------------------

    def test_parser_fallback_malformed_penultimate(self):
        """§13.6 — Malformed penultimate line → WARNING; street set, city/state_id/zip False."""
        oadm = dict(
            _FULL_OADM,
            compnyaddr="123 Main\rgarbage\rUSA",
            country="",
            state="",
            btwcity="",
            btwzip="",
        )
        with self.assertLogs("odoo.addons.sap_b1_to_odoo.models.pipelines.res_company_etl", level="WARNING") as cm:
            vals = self._run_transform(oadm, obpl_row=None)

        self.assertEqual(vals["street"], "123 Main")
        self.assertFalse(vals.get("city"))
        self.assertFalse(vals.get("state_id"))
        self.assertFalse(vals.get("zip"))
        # country resolved from parser "USA" token — "USA" is not a standard ISO code,
        # so country_id should be False and a warning logged.
        self.assertTrue(
            any("WARNING" in msg for msg in cm.output),
            "At least one WARNING must be logged",
        )

    # ------------------------------------------------------------------
    # Test 14: Non-address fields preserved
    # ------------------------------------------------------------------

    def test_non_address_fields_preserved(self):
        """§13/§14 — Non-address fields all land in vals unchanged."""
        obpl = {
            "bplid": 7,
            "street": "1 Main",
            "streetno": "",
            "block": "",
            "city": "Denver",
            "state": "CO",
            "zipcode": "80203",
            "country": "US",
        }
        oadm = {
            "compnyname": "My Company",
            "compnyaddr": "",
            "country": "US",
            "state": "",
            "btwcity": "",
            "btwzip": "",
            "phone1": "555-9999",
            "phone2": "",
            "e_mail": "info@myco.com",
            "maincurncy": "USD",
            "taxidnum": "99-9999999",
            "invntsystm": "A",
            "continvnt": "Y",
        }
        vals = self._run_transform(oadm, obpl_row=obpl)

        self.assertEqual(vals.get("name"), "My Company")
        self.assertEqual(vals.get("phone"), "555-9999")
        self.assertEqual(vals.get("email"), "info@myco.com")
        self.assertEqual(vals.get("vat"), "99-9999999")
        self.assertEqual(vals.get("cost_method"), "average")
        self.assertEqual(vals.get("inventory_valuation"), "real_time")
        # currency_id must be set (USD is the company currency)
        self.assertTrue(vals.get("currency_id"), "currency_id must be set for USD")

    # ------------------------------------------------------------------
    # Test 15: Re-import overwrites stale street
    # ------------------------------------------------------------------

    def test_reimport_overwrites_stale_street(self):
        """§9 — Re-importing must overwrite a stale street value."""
        # Pre-condition: company already has a bad street
        company = self.company
        original_name = company.name
        company.write({"street": "STALE BROKEN BLOB"})
        self.assertEqual(company.street, "STALE BROKEN BLOB")

        obpl = {
            "bplid": 8,
            "street": "33163 Braniff Avenue",
            "streetno": "",
            "block": "",
            "city": "Pueblo",
            "state": "CO",
            "zipcode": "81001",
            "country": "US",
        }
        oadm = dict(_FULL_OADM, compnyname=original_name)

        # Build ctx with real SAP cursor stub and call load_company
        importer = self.env["res.company.importer"]
        extracted = {"extract_company": {"oadm": oadm, "obpl": obpl}}
        ctx_transform = ETLContext(cr=MagicMock(), env=self.env)
        vals = importer.transform_company(ctx_transform, extracted)

        ctx_load = ETLContext(cr=MagicMock(), env=self.env)
        importer.load_company(ctx_load, {"transform_company": vals})

        company.invalidate_recordset()
        self.assertEqual(
            company.street,
            "33163 Braniff Avenue",
            "Re-import must overwrite the stale street value",
        )
