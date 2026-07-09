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
"""Tests for country-scoped state resolution in res_partner_etl.

Acceptance criteria:
====================

TestGetStatesDict (unit — no pipeline call):
1. (test_dict_keys_are_composite_tuples) get_states_dict returns keys of the
   form (country_id, value.upper()), not bare string codes.
2. (test_us_co_and_uy_co_are_distinct) US "CO" (Colorado) and UY "CO" (Colonia)
   are present as distinct keys with distinct IDs.

TestExtractSapStateCountry (unit — calls helper directly):
3. (test_us_co_resolves_to_colorado) (US, "CO") → Colorado id; NOT Colonia.
4. (test_uy_co_resolves_to_colonia) (UY, "CO") → Colonia id; NOT Colorado.
5. (test_lowercase_code) (US, "co") → Colorado id (case-insensitive).
6. (test_name_based_match_quebec) (CA, "Quebec") → Quebec province id.
7. (test_missing_country) ("", "CO") → (False, False), no WARNING for blank.
8. (test_unknown_country_code) ("ZZ", "CO") → (False, False), WARNING logged.
9. (test_state_not_found_in_country) (US, "ZZ") → (country_id, False), WARNING.

TestResPartnerCompanyImporterState (integration — uses transform_companies
with a stubbed SAP cursor):
10. (test_company_us_co_resolves_to_colorado) OCRD row country=US, state1=CO
    → partner state_id = Colorado. Assert NOT Colonia.
11. (test_company_uy_co_resolves_to_colonia) OCRD row country=UY, state1=CO
    → partner state_id = Colonia. Assert NOT Colorado.

TestResPartnerAddressImporterState (integration — uses transform_addresses
with a stubbed SAP cursor):
12. (test_address_us_co_resolves_to_colorado) CRD1 row country=US, state=CO
    → partner state_id = Colorado. Assert NOT Colonia.
13. (test_address_uy_co_resolves_to_colonia) CRD1 row country=UY, state=CO
    → partner state_id = Colonia. Assert NOT Colorado.
"""

from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl import (
    get_countries_dict,
    get_states_dict,
    extract_sap_state_country,
)
from odoo.addons.etl_framework import ETLContext, ChunkableData


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company_ocrd_row(**overrides):
    """Return a minimal SAP OCRD dict sufficient for transform_companies."""
    row = {
        "cardcode": "C001",
        "cardname": "Test Company",
        "atcentry": 0,
        "country": "US",
        "state1": "CO",
        "address": "1 Main St",
        "block": "",
        "city": "Denver",
        "zipcode": "80203",
        "fathercard": None,
        "cardtype": "C",
        "phone1": "555-1234",
        "phone2": "",
        "e_mail": "test@example.com",
        "slpcode": -1,
        "currency": "USD",
        "groupnum": None,
        "partdelivr": "N",
        "notes": "",
        "debpayacct": "",
        "listnum": None,
    }
    row.update(overrides)
    return row


def _make_address_crd1_row(**overrides):
    """Return a minimal SAP CRD1 dict sufficient for transform_addresses."""
    row = {
        "cardcode": "C001",
        "linenum": 1,
        "country": "US",
        "state": "CO",
        "address": "Billing Address",
        "address2": "",
        "address3": "",
        "street": "1 Main St",
        "block": "",
        "city": "Denver",
        "zipcode": "80203",
        "adrestype": "B",
    }
    row.update(overrides)
    return row


def _make_fake_cursor(dictfetchall_result):
    """Return a mock SAP cursor that yields ``dictfetchall_result`` on the
    first ``dictfetchall()`` call (used for extract phase bypass)."""
    cursor = MagicMock()
    cursor.dictfetchall.return_value = dictfetchall_result
    return cursor


# ---------------------------------------------------------------------------
# Test helpers shared across integration test classes
# ---------------------------------------------------------------------------


def _us_country_id(env):
    return env.ref("base.us").id


def _uy_country_id(env):
    return env.ref("base.uy").id


def _ca_country_id(env):
    return env.ref("base.ca").id


def _us_co_state_id(env):
    """Colorado (US)."""
    return env["res.country.state"].search(
        [("country_id", "=", _us_country_id(env)), ("code", "=", "CO")],
        limit=1,
    ).id


def _uy_co_state_id(env):
    """Colonia (UY) — department code CO in Odoo base data."""
    uy_id = _uy_country_id(env)
    state = env["res.country.state"].search(
        [("country_id", "=", uy_id), ("code", "=", "CO")],
        limit=1,
    )
    if not state:
        # Colonia not in base data; create it for tests.
        state = env["res.country.state"].create(
            {
                "country_id": uy_id,
                "name": "Colonia",
                "code": "CO",
            }
        )
    return state.id


def _ca_qc_state_id(env):
    """Quebec province (CA)."""
    return env["res.country.state"].search(
        [("country_id", "=", _ca_country_id(env)), ("code", "=", "QC")],
        limit=1,
    ).id


# ---------------------------------------------------------------------------
# TestGetStatesDict — unit tests for the helper function
# ---------------------------------------------------------------------------


@tagged("-at_install", "post_install", "res_partner_etl_state")
class TestGetStatesDict(TransactionCase):
    """Unit tests for :func:`get_states_dict`."""

    def test_dict_keys_are_composite_tuples(self):
        """AC#1 — All keys in get_states_dict are (country_id, str) tuples."""
        sd = get_states_dict(self.env)
        self.assertTrue(len(sd) > 0, "states_dict must be non-empty")
        for key in sd:
            self.assertIsInstance(key, tuple, "Key must be a tuple")
            self.assertEqual(len(key), 2, "Key must be a 2-tuple")
            self.assertIsInstance(key[0], int, "First element must be country_id int")
            self.assertIsInstance(key[1], str, "Second element must be a string")

    def test_us_co_and_uy_co_are_distinct(self):
        """AC#2 — US CO (Colorado) and UY CO (Colonia) are distinct keys."""
        us_id = _us_country_id(self.env)
        uy_id = _uy_country_id(self.env)
        expected_us_co = _us_co_state_id(self.env)
        expected_uy_co = _uy_co_state_id(self.env)

        sd = get_states_dict(self.env)
        us_co_id = sd.get((us_id, "CO"))
        uy_co_id = sd.get((uy_id, "CO"))

        self.assertIsNotNone(us_co_id, "US CO key must exist in states_dict")
        self.assertIsNotNone(uy_co_id, "UY CO key must exist in states_dict")
        self.assertEqual(us_co_id, expected_us_co, "US CO must map to Colorado")
        self.assertEqual(uy_co_id, expected_uy_co, "UY CO must map to Colonia")
        self.assertNotEqual(
            us_co_id,
            uy_co_id,
            "US CO and UY CO must be distinct state IDs",
        )


# ---------------------------------------------------------------------------
# TestExtractSapStateCountry — unit tests for the resolver function
# ---------------------------------------------------------------------------


@tagged("-at_install", "post_install", "res_partner_etl_state")
class TestExtractSapStateCountry(TransactionCase):
    """Unit tests for :func:`extract_sap_state_country`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.countries_dict = get_countries_dict(cls.env)
        cls.states_dict = get_states_dict(cls.env)
        # Ensure UY CO exists.
        _uy_co_state_id(cls.env)
        cls.states_dict = get_states_dict(cls.env)  # rebuild after possible create

    def _resolve(self, country_code, state_code):
        return extract_sap_state_country(
            country_code, state_code, self.countries_dict, self.states_dict
        )

    def test_us_co_resolves_to_colorado(self):
        """AC#3 — (US, CO) → Colorado id."""
        country_id, state_id = self._resolve("US", "CO")
        self.assertEqual(country_id, _us_country_id(self.env))
        self.assertEqual(state_id, _us_co_state_id(self.env))
        self.assertNotEqual(state_id, _uy_co_state_id(self.env), "Must NOT be Colonia")

    def test_uy_co_resolves_to_colonia(self):
        """AC#4 — (UY, CO) → Colonia id."""
        country_id, state_id = self._resolve("UY", "CO")
        self.assertEqual(country_id, _uy_country_id(self.env))
        self.assertEqual(state_id, _uy_co_state_id(self.env))
        self.assertNotEqual(state_id, _us_co_state_id(self.env), "Must NOT be Colorado")

    def test_lowercase_code(self):
        """AC#5 — (US, "co") lowercase → Colorado (case-insensitive)."""
        country_id, state_id = self._resolve("US", "co")
        self.assertEqual(country_id, _us_country_id(self.env))
        self.assertEqual(state_id, _us_co_state_id(self.env))

    def test_name_based_match_quebec(self):
        """AC#6 — (CA, "Quebec") name match → Quebec province."""
        country_id, state_id = self._resolve("CA", "Quebec")
        self.assertEqual(country_id, _ca_country_id(self.env))
        self.assertEqual(state_id, _ca_qc_state_id(self.env))

    def test_missing_country(self):
        """AC#7 — blank country → (False, False) without WARNING."""
        with self.assertNoLogs(
            "odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl",
            level="WARNING",
        ):
            country_id, state_id = self._resolve("", "CO")
        self.assertFalse(country_id)
        self.assertFalse(state_id)

    def test_unknown_country_code(self):
        """AC#8 — unknown country 'ZZ' → (False, False), WARNING logged."""
        with self.assertLogs(
            "odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl",
            level="WARNING",
        ) as cm:
            country_id, state_id = self._resolve("ZZ", "CO")
        self.assertFalse(country_id)
        self.assertFalse(state_id)
        self.assertTrue(
            any("ZZ" in msg for msg in cm.output),
            "WARNING must name the bad country code ZZ",
        )

    def test_state_not_found_in_country(self):
        """AC#9 — country 'US' found, state 'ZZ' missing → (country_id, False), WARNING."""
        with self.assertLogs(
            "odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl",
            level="WARNING",
        ) as cm:
            country_id, state_id = self._resolve("US", "ZZ")
        self.assertEqual(country_id, _us_country_id(self.env))
        self.assertFalse(state_id)
        self.assertTrue(
            any("ZZ" in msg for msg in cm.output),
            "WARNING must name the bad state code ZZ",
        )


# ---------------------------------------------------------------------------
# TestResPartnerCompanyImporterState — integration via transform_companies
# ---------------------------------------------------------------------------


@tagged("-at_install", "post_install", "res_partner_etl_state")
class TestResPartnerCompanyImporterState(TransactionCase):
    """Integration tests for ResPartnerCompanyImporter state resolution.

    Calls transform_companies directly with a pre-built cache to avoid
    needing a live SAP connection.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Ensure UY/CO exists before building the dict.
        _uy_co_state_id(cls.env)

    def _run_transform(self, sap_rows):
        """Run transform_companies with a cache built from the real Odoo env."""
        importer = self.env["res.partner.company.importer"]
        from odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl import (
            get_countries_dict,
            get_states_dict,
            get_users_dict,
            get_payment_terms_dict,
        )
        cache = {
            "countries_dict": get_countries_dict(self.env),
            "states_dict": get_states_dict(self.env),
            "users_dict": get_users_dict(self.env),
            "terms_dict": get_payment_terms_dict(self.env),
            "currencies_dict": {
                curr.name: curr.id
                for curr in self.env["res.currency"].search([])
            },
            "company_currency_id": self.env.company.currency_id.id,
            "company_id": self.env.company.id,
            "accounts_dict": {},
            "pricelists_by_listnum": {},
        }
        data = ChunkableData(records=sap_rows, context=cache)
        ctx = ETLContext(cr=MagicMock(), env=self.env)
        extracted = {"extract_companies": data}
        return importer.transform_companies(ctx, extracted)

    def test_company_us_co_resolves_to_colorado(self):
        """AC#10 — OCRD country=US, state1=CO → Colorado (NOT Colonia)."""
        row = _make_company_ocrd_row(country="US", state1="CO")
        vals_list = self._run_transform([row])
        self.assertEqual(len(vals_list), 1)
        state_id = vals_list[0]["state_id"]
        self.assertEqual(state_id, _us_co_state_id(self.env))
        self.assertNotEqual(
            state_id,
            _uy_co_state_id(self.env),
            "Company US/CO must resolve to Colorado, not Colonia",
        )

    def test_company_uy_co_resolves_to_colonia(self):
        """AC#11 — OCRD country=UY, state1=CO → Colonia (NOT Colorado)."""
        row = _make_company_ocrd_row(country="UY", state1="CO")
        vals_list = self._run_transform([row])
        self.assertEqual(len(vals_list), 1)
        state_id = vals_list[0]["state_id"]
        self.assertEqual(state_id, _uy_co_state_id(self.env))
        self.assertNotEqual(
            state_id,
            _us_co_state_id(self.env),
            "Company UY/CO must resolve to Colonia, not Colorado",
        )

    def test_company_id_not_written(self):
        """company_id must NOT be emitted on company partners.

        res.partner.company_id has no default in Odoo — partners are shared
        across companies. The importer must leave it blank; hand-writing it is
        what pinned every contact to the main company and made EbizCharge choke.
        The acting company is set on the execution env, not on the partner.
        """
        row = _make_company_ocrd_row(country="US", state1="CO")
        vals_list = self._run_transform([row])
        self.assertNotIn("company_id", vals_list[0])


# ---------------------------------------------------------------------------
# TestResPartnerAddressImporterState — integration via transform_addresses
# ---------------------------------------------------------------------------


@tagged("-at_install", "post_install", "res_partner_etl_state")
class TestResPartnerAddressImporterState(TransactionCase):
    """Integration tests for ResPartnerAddressImporter state resolution.

    Calls transform_addresses directly with a pre-built cache.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _uy_co_state_id(cls.env)

    def _run_transform(self, sap_rows):
        """Run transform_addresses with a cache built from the real Odoo env."""
        importer = self.env["res.partner.address.importer"]
        cache = {
            "countries_dict": get_countries_dict(self.env),
            "states_dict": get_states_dict(self.env),
        }
        data = ChunkableData(records=sap_rows, context=cache)
        ctx = ETLContext(cr=MagicMock(), env=self.env)
        extracted = {"extract_addresses": data}
        return importer.transform_addresses(ctx, extracted)

    def test_address_us_co_resolves_to_colorado(self):
        """AC#12 — CRD1 country=US, state=CO → Colorado (NOT Colonia)."""
        row = _make_address_crd1_row(country="US", state="CO")
        vals_list = self._run_transform([row])
        self.assertEqual(len(vals_list), 1)
        state_id = vals_list[0]["state_id"]
        self.assertEqual(state_id, _us_co_state_id(self.env))
        self.assertNotEqual(
            state_id,
            _uy_co_state_id(self.env),
            "Address US/CO must resolve to Colorado, not Colonia",
        )

    def test_address_uy_co_resolves_to_colonia(self):
        """AC#13 — CRD1 country=UY, state=CO → Colonia (NOT Colorado)."""
        row = _make_address_crd1_row(country="UY", state="CO")
        vals_list = self._run_transform([row])
        self.assertEqual(len(vals_list), 1)
        state_id = vals_list[0]["state_id"]
        self.assertEqual(state_id, _uy_co_state_id(self.env))
        self.assertNotEqual(
            state_id,
            _us_co_state_id(self.env),
            "Address UY/CO must resolve to Colonia, not Colorado",
        )
