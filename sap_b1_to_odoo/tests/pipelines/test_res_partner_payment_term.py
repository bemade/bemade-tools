#
#    Bemade Inc.
#
#    Copyright (C) 2026-May Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Regression tests for payment-term assignment in ResPartnerCompanyImporter.

Acceptance criteria covered here:

AC #1. (test_customer_with_payment_term_gets_property_set) A customer OCRD row
       with groupnum=10 produces vals with property_payment_term_id == net30.id;
       after _create_partners_from_vals, the partner read with_company has
       property_payment_term_id == net30.

AC #2. (test_import_is_idempotent_for_payment_term) Running transform + create
       twice on disjoint cardcodes with groupnum=10 yields the same
       property_payment_term_id on both runs.

AC #3. (test_customer_with_no_payment_term_is_unset,
        test_customer_with_sentinel_groupnum_is_unset_and_no_warning) Sentinel
       values (None, 0, -1, False) produce property_payment_term_id == False
       without emitting a warning.

AC #4. (test_customer_with_unknown_groupnum_emits_warning_and_unset)
       Non-sentinel groupnum not in terms_dict produces
       property_payment_term_id == False AND emits a _logger.warning containing
       both the cardcode and the groupnum.

AC #5. The suite as a whole fails against unpatched code (tests 1, 3-partial, 4)
       and passes post-fix.
"""

from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.etl_framework import ChunkableData


@tagged("-at_install", "post_install", "payment_term_partner_assignment")
class TestPartnerPaymentTermAssignment(TransactionCase):
    """Guards payment-term assignment written during partner create from OCRD.GroupNum."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.importer = cls.env["res.partner.company.importer"]

        # Create two payment terms that simulate OCTG rows already imported.
        cls.net30 = cls.env["account.payment.term"].create({
            "name": "Net 30",
            "sap_groupnum": 10,
        })
        cls.net60 = cls.env["account.payment.term"].create({
            "name": "Net 60",
            "sap_groupnum": 20,
        })

    # ------------------------------------------------------------------
    # Helpers (mirrored from test_res_partner_pricelist.py)
    # ------------------------------------------------------------------

    def _make_ocrd_row(self, cardcode, cardtype="C", groupnum=False, **kwargs):
        """Build a minimal OCRD dict suitable for transform_companies."""
        defaults = {
            "cardcode": cardcode,
            "cardname": f"Test Partner {cardcode}",
            "cardtype": cardtype,
            "groupnum": groupnum,
            "listnum": None,
            "country": False,
            "state1": False,
            "address": False,
            "block": False,
            "slpcode": False,
            "currency": False,
            "partdelivr": "N",
            "e_mail": False,
            "phone1": False,
            "phone2": False,
            "zipcode": False,
            "city": False,
            "fathercard": False,
            "notes": False,
            "atcentry": False,
            "debpayacct": False,
        }
        defaults.update(kwargs)
        return defaults

    def _run_transform(self, ocrd_rows, terms_dict=None):
        """Run transform_companies with the given OCRD rows and cache."""
        if terms_dict is None:
            terms_dict = {
                10: self.net30.id,
                20: self.net60.id,
            }

        cache = {
            "countries_dict": {},
            "states_dict": {},
            "users_dict": {},
            "terms_dict": terms_dict,
            "currencies_dict": {},
            "company_currency_id": self.company.currency_id.id,
            "company_id": self.company.id,
            "accounts_dict": {},
            "pricelists_by_listnum": {},
        }

        ctx = MagicMock()
        ctx.env = self.env

        extracted = {
            "extract_companies": ChunkableData(records=ocrd_rows, context=cache),
        }
        return self.importer.transform_companies(ctx, extracted)

    def _create_partners_from_vals(self, partner_vals):
        """Create partners using with_company so company-dependent properties land correctly."""
        return self.env["res.partner"].with_company(self.company).create(partner_vals)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_customer_with_payment_term_gets_property_set(self):
        """Customer with groupnum=10 gets property_payment_term_id=net30 (AC #1)."""
        rows = [self._make_ocrd_row("C_TERM_01", cardtype="C", groupnum=10)]
        vals_list = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        self.assertEqual(
            vals_list[0]["property_payment_term_id"],
            self.net30.id,
            "transform should set property_payment_term_id to net30 id.",
        )

        partner = self._create_partners_from_vals(vals_list)
        self.assertEqual(
            partner.with_company(self.company).property_payment_term_id,
            self.net30,
            "Partner created with groupnum=10 must resolve to net30 when read "
            "in the company context.",
        )

    def test_customer_with_no_payment_term_is_unset(self):
        """Customer with groupnum=False gets property_payment_term_id == False (AC #3)."""
        rows = [self._make_ocrd_row("C_NOTERM_01", cardtype="C", groupnum=False)]
        vals_list = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        self.assertFalse(
            vals_list[0]["property_payment_term_id"],
            "property_payment_term_id must be False when groupnum is False.",
        )

        partner = self._create_partners_from_vals(vals_list)
        self.assertFalse(
            partner.with_company(self.company).property_payment_term_id,
            "Created partner with groupnum=False must have no payment term.",
        )

    def test_customer_with_sentinel_groupnum_is_unset_and_no_warning(self):
        """Sentinel groupnums (0, -1) produce False and no warning (AC #3, AC #4)."""
        logger_path = (
            "odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl._logger"
        )
        for sentinel in (0, -1):
            with self.subTest(sentinel=sentinel):
                rows = [
                    self._make_ocrd_row(
                        f"C_SENTINEL_{sentinel}", cardtype="C", groupnum=sentinel
                    )
                ]
                with patch(logger_path) as mock_logger:
                    vals_list = self._run_transform(rows)

                self.assertEqual(len(vals_list), 1)
                self.assertFalse(
                    vals_list[0]["property_payment_term_id"],
                    f"property_payment_term_id must be False for sentinel groupnum={sentinel}.",
                )
                mock_logger.warning.assert_not_called()

    def test_customer_with_unknown_groupnum_emits_warning_and_unset(self):
        """Non-sentinel groupnum=999 (not in terms_dict) produces False + warning (AC #4)."""
        rows = [self._make_ocrd_row("C_UNKNOWN_99", cardtype="C", groupnum=999)]

        logger_path = (
            "odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl._logger"
        )
        with patch(logger_path) as mock_logger:
            vals_list = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        self.assertFalse(
            vals_list[0]["property_payment_term_id"],
            "property_payment_term_id must be False when groupnum has no matching term.",
        )

        warning_calls = mock_logger.warning.call_args_list
        self.assertTrue(
            warning_calls,
            "_logger.warning must be called when groupnum has no matching payment term.",
        )
        # At least one warning must reference both the cardcode and the groupnum
        found = any(
            "C_UNKNOWN_99" in str(call) and "999" in str(call)
            for call in warning_calls
        )
        self.assertTrue(
            found,
            "Warning message must reference both the cardcode 'C_UNKNOWN_99' and groupnum 999.",
        )

    def test_vendor_groupnum_routes_to_supplier_term_only(self):
        """Vendor (cardtype=S) with groupnum=10 gets supplier term, not customer term (regression)."""
        rows = [self._make_ocrd_row("S_VENDOR_01", cardtype="S", groupnum=10)]
        vals_list = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        self.assertFalse(
            vals_list[0]["property_payment_term_id"],
            "Vendor must not receive property_payment_term_id.",
        )
        self.assertEqual(
            vals_list[0]["property_supplier_payment_term_id"],
            self.net30.id,
            "Vendor with groupnum=10 must receive property_supplier_payment_term_id=net30.",
        )

        partner = self._create_partners_from_vals(vals_list)
        self.assertFalse(
            partner.with_company(self.company).property_payment_term_id,
            "Loaded vendor must have no customer payment term.",
        )

    def test_lead_groupnum_routes_to_customer_term(self):
        """Lead (cardtype=L) with groupnum=20 gets customer term, not supplier term."""
        rows = [self._make_ocrd_row("L_LEAD_01", cardtype="L", groupnum=20)]
        vals_list = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        self.assertEqual(
            vals_list[0]["property_payment_term_id"],
            self.net60.id,
            "Lead with groupnum=20 must receive property_payment_term_id=net60.",
        )
        self.assertFalse(
            vals_list[0]["property_supplier_payment_term_id"],
            "Lead must not receive property_supplier_payment_term_id.",
        )

    def test_import_is_idempotent_for_payment_term(self):
        """Two separate runs with disjoint cardcodes both yield property_payment_term_id=net30 (AC #2)."""
        for run, cardcode in enumerate(("C_IDEM_01", "C_IDEM_02"), start=1):
            with self.subTest(run=run, cardcode=cardcode):
                rows = [self._make_ocrd_row(cardcode, cardtype="C", groupnum=10)]
                vals_list = self._run_transform(rows)

                self.assertEqual(len(vals_list), 1)
                self.assertEqual(
                    vals_list[0]["property_payment_term_id"],
                    self.net30.id,
                    f"Run {run}: property_payment_term_id must be net30 for {cardcode}.",
                )

                partner = self._create_partners_from_vals(vals_list)
                self.assertEqual(
                    partner.with_company(self.company).property_payment_term_id,
                    self.net30,
                    f"Run {run}: loaded partner {cardcode} must have net30.",
                )
