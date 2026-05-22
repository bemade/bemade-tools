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
"""Tests for ProductImporter suppcatnum → default_code mapping and upsert behaviour.

Acceptance criteria:

1. (test_suppcatnum_maps_to_default_code) suppcatnum is written to default_code.
2. (test_frgnname_maps_to_description) frgnname is written to description and name.
3. (test_frgnname_not_in_default_code) frgnname is NOT written to default_code.
4. (test_missing_suppcatnum_default_code_blank) None suppcatnum → default_code is False.
5. (test_empty_suppcatnum_default_code_blank) "" suppcatnum → default_code is False.
6. (test_missing_frgnname_description_blank) None frgnname → description is False,
   name falls back to itemname, default_code still comes from suppcatnum.
7. (test_reimport_updates_existing_default_code) Re-import with same sap_item_code
   updates default_code and description on the existing record without duplication.
8. (test_reimport_creates_new_when_absent) Previously-unseen sap_item_code creates
   a new record.
9. (test_quotes_are_fixed_in_suppcatnum) fix_quotes is applied to suppcatnum before
   writing default_code.
"""

from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.sap_b1_to_odoo.tools import fix_quotes


def _make_sap_row(
    itemcode="ITM1",
    itemname="Widget Internal",
    frgnname="Widget FR",
    suppcatnum="SUP-001",
    itmsgrpcod=None,
    sellitem="Y",
    prchseitem="Y",
    validfor="Y",
    atcentry=1,
    avgprice=0.0,
    base_price=0.0,
):
    """Return a minimal OITM-style dict for use in transform tests."""
    return {
        "itemcode": itemcode,
        "itemname": itemname,
        "frgnname": frgnname,
        "suppcatnum": suppcatnum,
        "itmsgrpcod": itmsgrpcod,
        "sellitem": sellitem,
        "prchseitem": prchseitem,
        "validfor": validfor,
        "atcentry": atcentry,
        "avgprice": avgprice,
        "base_price": base_price,
    }


@tagged("-at_install", "post_install", "product_etl_suppcatnum")
class TestProductProductEtl(TransactionCase):
    """Guards suppcatnum→default_code mapping and upsert load logic."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["product.product.importer"]

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.env = self.env
        return ctx

    def _transform(self, sap_rows, categories_map=None, company_id=None):
        """Run transform_products against a list of SAP row dicts."""
        extracted = {
            "extract_products": MagicMock(
                records=sap_rows,
                context={
                    "categories_map": categories_map or {},
                    "company_id": company_id or self.env.company.id,
                },
            )
        }
        ctx = self._make_ctx()
        return self.importer.transform_products(ctx, extracted)

    # ------------------------------------------------------------------
    # Transform tests
    # ------------------------------------------------------------------

    def test_suppcatnum_maps_to_default_code(self):
        """suppcatnum is written to default_code."""
        vals = self._transform([_make_sap_row(suppcatnum="SUP-001")])
        self.assertEqual(vals[0]["default_code"], "SUP-001")

    def test_frgnname_maps_to_description(self):
        """frgnname is written to description and used as name."""
        vals = self._transform([_make_sap_row(frgnname="Widget FR")])
        self.assertEqual(vals[0]["description"], "Widget FR")
        self.assertEqual(vals[0]["name"], "Widget FR")

    def test_frgnname_not_in_default_code(self):
        """frgnname must NOT be written to default_code."""
        vals = self._transform([_make_sap_row(frgnname="Widget FR", suppcatnum="SUP-001")])
        self.assertNotEqual(vals[0]["default_code"], "Widget FR")

    def test_missing_suppcatnum_default_code_blank(self):
        """None suppcatnum → default_code is False, no exception raised."""
        vals = self._transform([_make_sap_row(suppcatnum=None)])
        self.assertIs(vals[0]["default_code"], False)

    def test_empty_suppcatnum_default_code_blank(self):
        """Empty string suppcatnum → default_code is False."""
        vals = self._transform([_make_sap_row(suppcatnum="")])
        self.assertIs(vals[0]["default_code"], False)

    def test_missing_frgnname_description_blank(self):
        """None frgnname → description is False; name falls back to itemname."""
        vals = self._transform([
            _make_sap_row(frgnname=None, itemname="Only Internal", suppcatnum="SUP-002")
        ])
        self.assertEqual(vals[0]["name"], "Only Internal")
        self.assertIs(vals[0]["description"], False)
        self.assertEqual(vals[0]["default_code"], "SUP-002")

    def test_quotes_are_fixed_in_suppcatnum(self):
        """fix_quotes is applied to suppcatnum before writing default_code."""
        raw = '"SUP\'001'
        expected = fix_quotes(raw)
        vals = self._transform([_make_sap_row(suppcatnum=raw)])
        self.assertEqual(vals[0]["default_code"], expected)

    # ------------------------------------------------------------------
    # Load (upsert) tests
    # ------------------------------------------------------------------

    def _run_load(self, product_vals):
        """Run load_products against a pre-built transformed dict."""
        transformed = {"transform_products": product_vals}
        ctx = self._make_ctx()
        self.importer.load_products(ctx, transformed)

    def test_reimport_updates_existing_default_code(self):
        """Re-import with same sap_item_code updates existing record without duplication."""
        # Create an existing product with old values
        existing = self.env["product.product"].create({
            "name": "Old Name",
            "sap_item_code": "ITM9",
            "default_code": "OLD",
        })
        before_count = self.env["product.product"].with_context(active_test=False).search_count(
            [("sap_item_code", "=", "ITM9")]
        )
        self.assertEqual(before_count, 1)

        # Run load with updated values
        self._run_load([{
            "sap_item_code": "ITM9",
            "name": "New Name",
            "default_code": "NEW-SUP",
            "description": "New FR Name",
            "list_price": 0.0,
            "standard_price": 0.0,
            "sale_ok": True,
            "purchase_ok": True,
            "active": True,
            "type": "consu",
            "is_storable": True,
            "company_id": self.env.company.id,
            "sap_atcentry": 1,
        }])

        self.env.flush_all()
        existing.invalidate_recordset()

        after_count = self.env["product.product"].with_context(active_test=False).search_count(
            [("sap_item_code", "=", "ITM9")]
        )
        self.assertEqual(after_count, 1, "No duplicate record must be created on re-import.")
        self.assertEqual(existing.default_code, "NEW-SUP")
        self.assertEqual(existing.description, "New FR Name")

    def test_reimport_creates_new_when_absent(self):
        """Previously-unseen sap_item_code creates a new record."""
        before_count = self.env["product.product"].with_context(active_test=False).search_count(
            [("sap_item_code", "=", "ITM_NEW")]
        )
        self.assertEqual(before_count, 0)

        self._run_load([{
            "sap_item_code": "ITM_NEW",
            "name": "Brand New",
            "default_code": "NEW-001",
            "description": False,
            "list_price": 0.0,
            "standard_price": 0.0,
            "sale_ok": True,
            "purchase_ok": True,
            "active": True,
            "type": "consu",
            "is_storable": True,
            "company_id": self.env.company.id,
            "sap_atcentry": 2,
        }])

        self.env.flush_all()

        after_count = self.env["product.product"].with_context(active_test=False).search_count(
            [("sap_item_code", "=", "ITM_NEW")]
        )
        self.assertEqual(after_count, 1, "A new record must be created for a new sap_item_code.")
