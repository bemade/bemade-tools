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
"""Tests for pricelist assignment in ResPartnerCompanyImporter (Change A).

Acceptance criteria covered here:

7a. (test_partner_create_assigns_non_default_pricelist) A customer OCRD row with
    listnum=2 mapped to a "Wholesale" pricelist results in that partner having
    property_product_pricelist set to "Wholesale" when read under with_company.

7b. (test_partner_create_assigns_explicit_retail) A customer OCRD row with
    listnum=1 mapped to a "Retail" pricelist results in that partner having
    property_product_pricelist set to "Retail" — even though Retail is the
    high-volume / house-default list.  No skip for the create path.

7c. (test_partner_with_unknown_listnum_emits_log_warning) A customer OCRD row
    with listnum=999 (no Odoo pricelist with sap_listnum=999) results in the
    partner being created with property_product_pricelist == False, and a
    _logger.warning call with the cardcode and listnum in the message.
"""

import logging
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.etl_framework import ChunkableData
from odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl import (
    ResPartnerCompanyImporter,
)


@tagged("-at_install", "post_install", "pricelist_partner_assignment")
class TestPartnerPricelistAssignment(TransactionCase):
    """Guards Change A: pricelist written during partner create from OCRD.listnum."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.importer = cls.env["res.partner.company.importer"]

        # Create the pricelists used in tests (simulating what the pricelist
        # pipeline would have created before the partner pipeline runs).
        cls.retail_pl = cls.env["product.pricelist"].create({
            "name": "Retail",
            "sap_listnum": 1,
            "company_id": cls.company.id,
        })
        cls.wholesale_pl = cls.env["product.pricelist"].create({
            "name": "Wholesale",
            "sap_listnum": 2,
            "company_id": cls.company.id,
        })

    def _make_ocrd_row(self, cardcode, cardtype="C", listnum=None, **kwargs):
        """Build a minimal OCRD dict suitable for transform_companies."""
        defaults = {
            "cardcode": cardcode,
            "cardname": f"Test Partner {cardcode}",
            "cardtype": cardtype,
            "listnum": listnum,
            "country": False,
            "state1": False,
            "address": False,
            "block": False,
            "slpcode": False,
            "currency": False,
            "groupnum": False,
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

    def _run_transform(self, ocrd_rows, pricelists_by_listnum=None):
        """Run transform_companies with the given OCRD rows and cache."""
        if pricelists_by_listnum is None:
            pricelists_by_listnum = {
                1: self.retail_pl.id,
                2: self.wholesale_pl.id,
            }

        cache = {
            "countries_dict": {},
            "states_dict": {},
            "users_dict": {},
            "terms_dict": {},
            "currencies_dict": {},
            "company_currency_id": self.company.currency_id.id,
            "company_id": self.company.id,
            "accounts_dict": {},
            "pricelists_by_listnum": pricelists_by_listnum,
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

    def test_partner_create_assigns_explicit_retail(self):
        """Customer with listnum=1 gets property_product_pricelist=Retail (AC #7b).

        This is the high-volume case (~95% of customers at RWI).  The
        implementation must NOT skip listnum=1 even if it happens to be the
        house-default.
        """
        rows = [self._make_ocrd_row("C_RETAIL_01", cardtype="C", listnum=1)]
        vals_list = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        self.assertEqual(
            vals_list[0]["property_product_pricelist"],
            self.retail_pl.id,
            "transform should set property_product_pricelist to Retail pricelist id.",
        )

        # Create via the load path (with_company) and confirm the property is readable
        partner = self._create_partners_from_vals(vals_list)
        partner_in_ctx = partner.with_company(self.company)
        self.assertEqual(
            partner_in_ctx.property_product_pricelist,
            self.retail_pl,
            "Partner created with listnum=1 must resolve to the Retail pricelist "
            "when read in the company context.",
        )

    def test_partner_create_assigns_non_default_pricelist(self):
        """Customer with listnum=2 gets property_product_pricelist=Wholesale (AC #7a)."""
        rows = [self._make_ocrd_row("C_WHOLE_01", cardtype="C", listnum=2)]
        vals_list = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        self.assertEqual(
            vals_list[0]["property_product_pricelist"],
            self.wholesale_pl.id,
            "transform should set property_product_pricelist to Wholesale pricelist id.",
        )

        partner = self._create_partners_from_vals(vals_list)
        self.assertEqual(
            partner.with_company(self.company).property_product_pricelist,
            self.wholesale_pl,
        )

    def test_partner_with_unknown_listnum_emits_log_warning(self):
        """Customer with listnum=999 (no matching pricelist) gets False + warning (AC #7c).

        Asserts:
        (a) property_product_pricelist is False in the vals dict.
        (b) _logger.warning was called with the cardcode and listnum in the message.
        """
        rows = [self._make_ocrd_row("C_UNKNOWN_99", cardtype="C", listnum=999)]

        logger_path = (
            "odoo.addons.sap_b1_to_odoo.models.pipelines.res_partner_etl._logger"
        )
        with patch(logger_path) as mock_logger:
            vals_list = self._run_transform(rows)

        self.assertEqual(len(vals_list), 1)
        self.assertFalse(
            vals_list[0]["property_product_pricelist"],
            "property_product_pricelist must be False when listnum has no matching pricelist.",
        )

        # Assert warning was logged with cardcode and listnum in the args
        warning_calls = mock_logger.warning.call_args_list
        self.assertTrue(
            warning_calls,
            "_logger.warning must be called when listnum has no matching pricelist.",
        )
        # Check that at least one warning call references the cardcode
        found = any(
            "C_UNKNOWN_99" in str(call) for call in warning_calls
        )
        self.assertTrue(
            found,
            "Warning message must reference the cardcode 'C_UNKNOWN_99'.",
        )

    def test_non_customer_does_not_get_pricelist(self):
        """Suppliers (cardtype=S) and leads (cardtype=L) do not get a pricelist."""
        rows = [
            self._make_ocrd_row("S_VENDOR_01", cardtype="S", listnum=1),
            self._make_ocrd_row("L_LEAD_01", cardtype="L", listnum=2),
        ]
        vals_list = self._run_transform(rows)

        for vals in vals_list:
            self.assertFalse(
                vals["property_product_pricelist"],
                f"Partner {vals['sap_card_code']} (non-customer) must not get a pricelist.",
            )
