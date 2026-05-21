#
#    Bemade Inc.
#
#    Copyright (C) 2024-June Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for ProductPricelistItemImporter house-default and step-4 pricelist assignment.

Acceptance criteria:

1. (test_house_default_hook_base_returns_empty) The base
   ``_get_house_default_pricelist`` must return an empty ``product.pricelist``
   recordset, and ``_apply_house_default_pricelist`` called with an empty
   recordset must make no sequence or archive writes (no-op).

2. (test_apply_house_default_archives_empty_default_and_lowers_sequence)
   Given an empty "Default"-shaped pricelist (no sap_listnum, no items,
   active=True, sequence=10) and a house_default pricelist (sequence=16, one
   item), after calling ``_apply_house_default_pricelist``:
   - The empty Default pricelist is archived (active=False).
   - house_default.sequence is strictly less than every other remaining
     active pricelist in the company domain.
"""

from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("-at_install", "post_install", "pricelist_house_default")
class TestProductPricelistHouseDefault(TransactionCase):
    """Guards the new step-5 house-default hook and apply logic in the base pipeline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["product.pricelist.item.importer"]

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.env = self.env
        return ctx

    def test_house_default_hook_base_returns_empty(self):
        """Base _get_house_default_pricelist returns an empty recordset (no-op).

        Also verifies that calling _apply_house_default_pricelist with an empty
        recordset leaves all pricelist sequences and active flags unchanged.
        """
        ctx = self._make_ctx()

        result = self.importer._get_house_default_pricelist(ctx)

        self.assertFalse(
            result,
            "_get_house_default_pricelist must return an empty recordset in the "
            "base pipeline.",
        )
        self.assertFalse(
            result.ids,
            "_get_house_default_pricelist must return a falsy recordset with no ids.",
        )

        # Capture sequences before calling apply with empty recordset
        Pricelist = self.env["product.pricelist"]
        before = {
            pl.id: (pl.sequence, pl.active)
            for pl in Pricelist.with_context(active_test=False).search([])
        }

        self.importer._apply_house_default_pricelist(ctx, result)

        # Flush ORM writes; then verify nothing changed
        self.env.flush_all()
        Pricelist.invalidate_model()
        after = {
            pl.id: (pl.sequence, pl.active)
            for pl in Pricelist.with_context(active_test=False).search([])
        }
        self.assertEqual(
            before,
            after,
            "_apply_house_default_pricelist with empty recordset must be a no-op: "
            "no sequence or active changes.",
        )

    def test_apply_house_default_archives_empty_default_and_lowers_sequence(self):
        """After applying, empty Default is archived and house_default has lowest sequence.

        Setup:
          - empty_default: no sap_listnum, no items, active=True, sequence=10
          - house_default: one item, active=True, sequence=16
        After _apply_house_default_pricelist:
          - empty_default.active == False
          - house_default.sequence < sequence of every other remaining active pl
        """
        company = self.env.company

        # Create the empty "Default"-shaped pricelist (no sap_listnum, no items)
        empty_default = self.env["product.pricelist"].create({
            "name": "Default",
            "company_id": company.id,
            "sequence": 10,
            "active": True,
        })

        # Create house_default with one item so the item_ids predicate doesn't archive it
        house_default = self.env["product.pricelist"].create({
            "name": "Retail",
            "company_id": company.id,
            "sequence": 16,
            "active": True,
            "item_ids": [(0, 0, {
                "applied_on": "3_global",
                "compute_price": "fixed",
                "fixed_price": 0.0,
            })],
        })

        ctx = self._make_ctx()
        self.importer._apply_house_default_pricelist(ctx, house_default)

        self.env.flush_all()
        empty_default.invalidate_recordset()
        house_default.invalidate_recordset()

        # empty_default must be archived
        self.assertFalse(
            empty_default.active,
            "The empty Default pricelist must be archived after applying house default.",
        )

        # house_default.sequence must be strictly less than all other active pricelists
        other_active_seqs = self.env["product.pricelist"].search([
            ("id", "!=", house_default.id),
            ("active", "=", True),
            "|",
            ("company_id", "=", company.id),
            ("company_id", "=", False),
        ]).mapped("sequence")
        for seq in other_active_seqs:
            self.assertLess(
                house_default.sequence,
                seq,
                f"house_default.sequence ({house_default.sequence}) must be less "
                f"than every other active pricelist sequence ({seq}).",
            )


@tagged("-at_install", "post_install", "pricelist_partner_assignment")
class TestProductPricelistStep4(TransactionCase):
    """Guards Change B: step 4 writes explicit pricelist for every customer.

    Acceptance criteria:

    - test_step4_writes_explicit_pricelist_even_when_equals_house_default:
      Both C001 (listnum=1, Retail = house default) and C002 (listnum=2,
      Wholesale) end up with an explicit specific pricelist after
      load_pricelists_and_blankets is called with the synthetic transformed dict.
      The skip-house-default branch from the old implementation must NOT exist.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.importer = cls.env["product.pricelist.item.importer"]

        # Create two pricelists with SAP listnums
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

        # Create two customer partners
        cls.partner_c001 = cls.env["res.partner"].create({
            "name": "Customer C001",
            "sap_card_code": "C001",
            "sap_partner_type": "C",
            "is_company": True,
            "company_id": cls.company.id,
        })
        cls.partner_c002 = cls.env["res.partner"].create({
            "name": "Customer C002",
            "sap_card_code": "C002",
            "sap_partner_type": "C",
            "is_company": True,
            "company_id": cls.company.id,
        })

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.env = self.env
        ctx.report = MagicMock()
        return ctx

    def _make_transformed(self, customer_listnum_map, partners_map):
        """Build a minimal transformed dict that load_pricelists_and_blankets can consume."""
        return {
            "transform_pricelists_and_blankets": {
                "base_pricelist_vals": [],
                "derived_pricelist_vals": [],
                "customer_pricelist_vals": [],
                "customer_listnum_map": customer_listnum_map,
                "partners_map": partners_map,
            }
        }

    def test_step4_writes_explicit_pricelist_even_when_equals_house_default(self):
        """Both customers get explicit pricelist; listnum=1 (Retail/house-default) is NOT skipped.

        Setup:
          - C001 → listnum=1 → Retail (simulates the house-default pricelist)
          - C002 → listnum=2 → Wholesale

        The old step 4 would have skipped C001 because Retail might equal
        house_default.  The new step 4 must write for both.
        """
        customer_listnum_map = {
            "C001": 1,
            "C002": 2,
        }
        partners_map = {
            "C001": self.partner_c001.id,
            "C002": self.partner_c002.id,
        }
        ctx = self._make_ctx()
        transformed = self._make_transformed(customer_listnum_map, partners_map)

        # Patch _get_house_default_pricelist to return Retail so the old
        # skip-house-default branch (if it still existed) would skip C001.
        with patch.object(
            type(self.importer),
            "_get_house_default_pricelist",
            return_value=self.retail_pl,
        ):
            self.importer.load_pricelists_and_blankets(ctx, transformed)

        self.env.flush_all()
        self.partner_c001.invalidate_recordset()
        self.partner_c002.invalidate_recordset()

        c001_pl = (
            self.partner_c001.with_company(self.company).property_product_pricelist
        )
        c002_pl = (
            self.partner_c002.with_company(self.company).property_product_pricelist
        )

        self.assertEqual(
            c001_pl,
            self.retail_pl,
            "C001 (listnum=1, Retail=house-default) must have Retail pricelist assigned "
            "explicitly — the house-default skip must NOT occur.",
        )
        self.assertEqual(
            c002_pl,
            self.wholesale_pl,
            "C002 (listnum=2, Wholesale) must have Wholesale pricelist assigned.",
        )
