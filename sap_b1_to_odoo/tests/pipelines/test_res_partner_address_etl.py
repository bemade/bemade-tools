#
#    Bemade Inc.
#
#    Copyright (C) 2026 Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Regression tests for ResPartnerAddressImporter — no-silent-drops hardening.

Acceptance criteria covered here:

AC #1. (test_address_row_transforms_to_valid_vals,
        test_address_row_loads_into_partner) An Ashes-shaped CRD1 row
        (address='GABRIEL MYERS', street='ASHES 2 ASHES LLC',
        block='306 1/2 E MAIN ST', city='TROY', state='OH', zip='45373',
        country='US', adrestype='S') for an unimported (cardcode, linenum)
        transforms to a vals dict with the expected field values and type,
        and load_addresses creates the partner record.  This is a true
        round-trip, not a restatement of the input.

AC #2. (test_already_present_row_is_skipped_with_info_summary) A partner
        pre-created with (sap_parent_card, sap_address_linenum) is excluded
        from extract, and the INFO-level summary correctly reflects the skip
        (extracted N / skipped K already-present).

AC #3a. (test_duplicate_crd1_key_emits_report_warning) Two CRD1 rows sharing
        the same (cardcode, linenum) cause extract_addresses to emit a
        ctx.report.warning with the matching source_ref.

AC #3b. (test_unresolvable_country_emits_report_warning) A CRD1 row whose
        country code cannot be resolved to a res.country causes
        transform_addresses to emit a ctx.report.warning with the matching
        source_ref.

AC #3c. (test_orphan_cardcode_emits_report_warning) A CRD1 row whose parent
        cardcode has no matching res.partner with sap_card_code causes
        transform_addresses to emit a ctx.report.warning with the matching
        source_ref.

AC #4. (test_clean_row_produces_no_report_warning) A fully-valid CRD1 row
        whose parent company exists and whose country/state resolve correctly
        produces no ctx.report.warning calls (no false positives).

Fail-before-fix note:
  AC#1 passes today (the data path is sound; staleness, not a parse bug).
  AC#2 passes today for the filter behaviour; the INFO-summary count wording
  changes with this fix, so this test validates the new phrasing.
  AC#3a, AC#3b, AC#3c, AC#4 all fail against unpatched code (no reporting).
"""

import logging
from unittest.mock import MagicMock, call

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.etl_framework import ChunkableData


_LOGGER_NAME = "odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl"


def _make_crd1_row(**overrides):
    """Return a minimal SAP CRD1 dict sufficient for the address importer."""
    row = {
        "cardcode": "C004549",
        "linenum": 17,
        "address": "GABRIEL MYERS",
        "address2": "",
        "address3": "",
        "street": "ASHES 2 ASHES LLC",
        "block": "306 1/2 E MAIN ST",
        "city": "TROY",
        "state": "OH",
        "zipcode": "45373",
        "country": "US",
        "adrestype": "S",
    }
    row.update(overrides)
    return row


@tagged("-at_install", "post_install", "address_no_silent_drops")
class TestResPartnerAddressETL(TransactionCase):
    """Guards no-silent-drops hardening for ResPartnerAddressImporter."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["res.partner.address.importer"]

        # Create a company partner that acts as the SAP parent for C004549.
        cls.parent_company = cls.env["res.partner"].create({
            "name": "ASHES 2 ASHES LLC",
            "is_company": True,
            "sap_card_code": "C004549",
        })

        # Resolve US country and Ohio state IDs for assertion use.
        cls.us_country = cls.env.ref("base.us")
        cls.oh_state = cls.env["res.country.state"].search(
            [("country_id", "=", cls.us_country.id), ("code", "=", "OH")],
            limit=1,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_cache(self, **overrides):
        """Build a minimal transform cache for the address importer.

        known_cardcodes defaults to {C004549} so the parent company is
        always considered present unless overridden.
        """
        from odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl import (
            get_countries_dict,
            get_states_dict,
        )
        cache = {
            "countries_dict": get_countries_dict(self.env),
            "states_dict": get_states_dict(self.env),
            "known_cardcodes": {"C004549"},
        }
        cache.update(overrides)
        return cache

    def _make_ctx(self, report=None):
        """Build a minimal mock ETLContext."""
        ctx = MagicMock()
        ctx.env = self.env
        ctx.report = report if report is not None else MagicMock()
        return ctx

    def _run_transform(self, crd1_rows, cache=None, ctx=None):
        """Run transform_addresses with the given rows and cache."""
        if cache is None:
            cache = self._make_cache()
        if ctx is None:
            ctx = self._make_ctx()
        extracted = {
            "extract_addresses": ChunkableData(records=crd1_rows, context=cache),
        }
        return self.importer.transform_addresses(ctx, extracted), ctx

    # ------------------------------------------------------------------
    # AC #1 — address row transforms to valid vals and loads into partner
    # ------------------------------------------------------------------

    def test_address_row_transforms_to_valid_vals(self):
        """AC #1 — Ashes linenum-17-shaped row transforms correctly (not a tautology)."""
        rows = [_make_crd1_row()]
        vals_list, _ = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        v = vals_list[0]

        self.assertEqual(v["type"], "delivery", "adrestype 'S' must become 'delivery'")
        self.assertEqual(v["city"], "TROY")
        self.assertEqual(v["zip"], "45373")
        self.assertEqual(v["country_id"], self.us_country.id)
        self.assertTrue(self.oh_state, "OH state must exist in base data")
        self.assertEqual(v["state_id"], self.oh_state.id)
        self.assertFalse(v["is_company"])
        self.assertEqual(v["sap_parent_card"], "C004549")
        self.assertEqual(v["sap_address_linenum"], 17)
        # street must be non-empty (comes from the SAP name/street fields)
        self.assertTrue(v["street"], "street must be populated from SAP address fields")

    def test_address_row_loads_into_partner(self):
        """AC #1 — load_addresses creates the res.partner record."""
        rows = [_make_crd1_row(cardcode="C_LOAD_01", linenum=99)]
        vals_list, ctx = self._run_transform(rows, cache=self._make_cache(
            known_cardcodes={"C_LOAD_01"},
        ))

        transformed = {"transform_addresses": vals_list}
        self.importer.load_addresses(ctx, transformed)

        partner = self.env["res.partner"].search([
            ("sap_parent_card", "=", "C_LOAD_01"),
            ("sap_address_linenum", "=", 99),
        ])
        self.assertEqual(len(partner), 1, "Exactly one address partner must be created")
        self.assertEqual(partner.type, "delivery")

    # ------------------------------------------------------------------
    # AC #2 — already-present row is skipped with INFO summary
    # ------------------------------------------------------------------

    def test_already_present_row_is_skipped_with_info_summary(self):
        """AC #2 — extract skips already-imported (cardcode, linenum) and logs it."""
        # Pre-create the partner so the extract sees it as existing.
        self.env["res.partner"].create({
            "name": "Existing Address",
            "is_company": False,
            "sap_parent_card": "C004549",
            "sap_address_linenum": 5,
        })

        # Build a minimal fake SAP cursor that returns one existing + one new row.
        existing_row = _make_crd1_row(cardcode="C004549", linenum=5)
        new_row = _make_crd1_row(cardcode="C004549", linenum=99)

        fake_cr = MagicMock()
        fake_cr.dictfetchall.return_value = [existing_row, new_row]

        ctx = self._make_ctx()
        ctx.cr = fake_cr

        with self.assertLogs(_LOGGER_NAME, level=logging.INFO) as cm:
            result = self.importer.extract_addresses(ctx)

        # Only the new row must be extracted.
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["linenum"], 99)

        # INFO summary must mention skipped count (1 already-present).
        summary_lines = [line for line in cm.output if "skipped" in line.lower()]
        self.assertTrue(
            summary_lines,
            "INFO summary must contain 'skipped' to report already-present rows",
        )
        self.assertTrue(
            any("1" in line for line in summary_lines),
            "INFO summary must report 1 skipped row",
        )

    # ------------------------------------------------------------------
    # AC #3a — duplicate CRD1 key emits ctx.report.warning
    # ------------------------------------------------------------------

    def test_duplicate_crd1_key_emits_report_warning(self):
        """AC #3a — two CRD1 rows with same (cardcode, linenum) → report warning."""
        dup_a = _make_crd1_row(cardcode="C_DUP_01", linenum=3)
        dup_b = _make_crd1_row(cardcode="C_DUP_01", linenum=3, city="DUPLICATE")

        fake_cr = MagicMock()
        fake_cr.dictfetchall.return_value = [dup_a, dup_b]

        mock_report = MagicMock()
        ctx = self._make_ctx(report=mock_report)
        ctx.cr = fake_cr

        self.importer.extract_addresses(ctx)

        # ctx.report.warning must be called at least once with the correct source_ref.
        warning_calls = mock_report.warning.call_args_list
        self.assertTrue(warning_calls, "ctx.report.warning must be called for dup key")
        source_refs = [c.kwargs.get("source_ref") or (c.args[0] if c.args else None)
                       for c in warning_calls]
        # Accept both positional and keyword source_ref
        source_refs_kw = [c.kwargs.get("source_ref") for c in warning_calls]
        self.assertIn(
            "C_DUP_01:3",
            source_refs_kw,
            "report.warning source_ref must be 'C_DUP_01:3' for the duplicate key",
        )

    # ------------------------------------------------------------------
    # AC #3b — unresolvable country emits ctx.report.warning
    # ------------------------------------------------------------------

    def test_unresolvable_country_emits_report_warning(self):
        """AC #3b — country code present but unresolvable → transform report warning."""
        rows = [_make_crd1_row(cardcode="C004549", linenum=20, country="ZZ")]
        mock_report = MagicMock()

        with self.assertLogs(_LOGGER_NAME, level=logging.WARNING):
            vals_list, ctx = self._run_transform(rows, ctx=self._make_ctx(report=mock_report))

        warning_calls = mock_report.warning.call_args_list
        self.assertTrue(
            warning_calls,
            "ctx.report.warning must be called for unresolvable country code",
        )
        source_refs = [c.kwargs.get("source_ref") for c in warning_calls]
        self.assertIn(
            "C004549:20",
            source_refs,
            "report.warning source_ref must be 'C004549:20'",
        )

    # ------------------------------------------------------------------
    # AC #3c — orphan cardcode (no matching company) emits report warning
    # ------------------------------------------------------------------

    def test_orphan_cardcode_emits_report_warning(self):
        """AC #3c — parent cardcode absent from Odoo → transform report warning."""
        rows = [_make_crd1_row(cardcode="C_ORPHAN_01", linenum=1)]
        # known_cardcodes does NOT include C_ORPHAN_01
        cache = self._make_cache(known_cardcodes={"C004549"})
        mock_report = MagicMock()

        with self.assertLogs(_LOGGER_NAME, level=logging.WARNING):
            vals_list, ctx = self._run_transform(
                rows, cache=cache, ctx=self._make_ctx(report=mock_report)
            )

        warning_calls = mock_report.warning.call_args_list
        self.assertTrue(
            warning_calls,
            "ctx.report.warning must be called for orphan cardcode",
        )
        source_refs = [c.kwargs.get("source_ref") for c in warning_calls]
        self.assertIn(
            "C_ORPHAN_01:1",
            source_refs,
            "report.warning source_ref must be 'C_ORPHAN_01:1'",
        )

    # ------------------------------------------------------------------
    # AC #4 — clean row produces no ctx.report.warning (no false positives)
    # ------------------------------------------------------------------

    def test_clean_row_produces_no_report_warning(self):
        """AC #4 — valid row with known parent and resolvable country/state → no warning."""
        rows = [_make_crd1_row(cardcode="C004549", linenum=17)]
        mock_report = MagicMock()

        vals_list, _ = self._run_transform(
            rows,
            cache=self._make_cache(known_cardcodes={"C004549"}),
            ctx=self._make_ctx(report=mock_report),
        )

        self.assertEqual(len(vals_list), 1)
        mock_report.warning.assert_not_called()
