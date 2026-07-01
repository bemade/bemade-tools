#
#    Bemade Inc.
#
#    Copyright (C) 2025-January Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for ProductImporter.transform_products — u_salesextdescription → description_ecommerce.

Acceptance criteria:

1. (test_extended_description_mapped_to_description_ecommerce)
   Given a SAP product dict with ``u_salesextdescription = "Long extended web
   description."``, ``transform_products`` returns a vals dict where
   ``description_ecommerce == "Long extended web description."``.

2. (test_extended_description_empty_leaves_description_ecommerce_blank)
   Given a SAP product dict where ``u_salesextdescription`` is ``None`` (or
   empty string), the returned vals set ``description_ecommerce`` to ``False``
   and do NOT fall back to ``itemname`` or ``frgnname``.

3. (test_extended_description_quotes_sanitized)
   Given a SAP product dict with ``u_salesextdescription`` containing SAP-style
   smart quotes (the inputs that ``fix_quotes`` handles), the returned
   ``description_ecommerce`` matches ``fix_quotes(input)``.

4. (test_extended_description_does_not_clobber_other_fields)
   Given a SAP product with both ``u_salesextdescription`` filled and
   ``itemname``/``frgnname`` filled per the existing convention, the returned
   vals still set ``name`` and ``default_code`` per existing logic AND set
   ``description_ecommerce`` to the extended description (no regression in
   ``name``/``default_code`` selection). Per task 3923, ``name`` is driven by
   ``itemname`` (frgnname no longer participates in ``name``).
"""

from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.sap_b1_to_odoo.tools import fix_quotes


def _make_sap_product(**overrides):
    """Return a minimal SAP OITM row dict suitable for transform_products."""
    base = {
        "itemcode": "ITEM001",
        "atcentry": 1,
        "itemname": "ITEM001",
        "frgnname": "Display Name",
        "itmsgrpcod": None,
        "sellitem": "Y",
        "prchseitem": "Y",
        "validfor": "Y",
        "avgprice": 0.0,
        "base_price": 0.0,
        "u_salesextdescription": None,
    }
    base.update(overrides)
    return base


def _make_extracted(sap_products, categories_map=None, company_id=1):
    """Wrap a list of SAP product dicts in the shape extract_products returns."""
    data = MagicMock()
    data.records = sap_products
    data.context = {
        "categories_map": categories_map or {},
        "company_id": company_id,
    }
    return {"extract_products": data}


@tagged("-at_install", "post_install", "product_description_ecommerce")
class TestProductDescriptionEcommerce(TransactionCase):
    """Guards the u_salesextdescription → description_ecommerce mapping."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["product.product.importer"]

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.env = self.env
        return ctx

    def test_extended_description_mapped_to_description_ecommerce(self):
        """u_salesextdescription is mapped to description_ecommerce in transform output."""
        sap_product = _make_sap_product(
            u_salesextdescription="Long extended web description."
        )
        ctx = self._make_ctx()
        extracted = _make_extracted([sap_product], company_id=self.env.company.id)

        result = self.importer.transform_products(ctx, extracted)

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["description_ecommerce"],
            "Long extended web description.",
            "description_ecommerce must equal the u_salesextdescription value.",
        )

    def test_extended_description_empty_leaves_description_ecommerce_blank(self):
        """None/empty u_salesextdescription → description_ecommerce is False; no fallback."""
        for empty_val in (None, ""):
            with self.subTest(u_salesextdescription=empty_val):
                sap_product = _make_sap_product(
                    u_salesextdescription=empty_val,
                    itemname="ITEMREF",
                    frgnname="Display Name",
                )
                ctx = self._make_ctx()
                extracted = _make_extracted(
                    [sap_product], company_id=self.env.company.id
                )

                result = self.importer.transform_products(ctx, extracted)

                self.assertEqual(len(result), 1)
                vals = result[0]
                self.assertFalse(
                    vals.get("description_ecommerce"),
                    "description_ecommerce must be False/empty when "
                    "u_salesextdescription is empty — must not fall back to "
                    "itemname or frgnname.",
                )
                # Confirm name is still driven by frgnname/itemname, not the
                # ecommerce description field
                self.assertNotEqual(
                    vals.get("description_ecommerce"),
                    vals.get("name"),
                    "description_ecommerce must not be set to the product name.",
                )

    def test_extended_description_quotes_sanitized(self):
        """SAP smart quotes in u_salesextdescription are sanitized by fix_quotes."""
        # Use the same input pattern that fix_quotes is designed to handle:
        # SAP sometimes stores curly/smart quotes that should be straightened.
        raw = "“Special” product ‘description’"
        expected = fix_quotes(raw)

        sap_product = _make_sap_product(u_salesextdescription=raw)
        ctx = self._make_ctx()
        extracted = _make_extracted([sap_product], company_id=self.env.company.id)

        result = self.importer.transform_products(ctx, extracted)

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["description_ecommerce"],
            expected,
            "description_ecommerce must equal fix_quotes(u_salesextdescription).",
        )

    def test_extended_description_does_not_clobber_other_fields(self):
        """Setting description_ecommerce does not regress name/default_code logic."""
        sap_product = _make_sap_product(
            itemname="ItemName Internal",
            frgnname="Customer Facing Name",
            suppcatnum="REF-001",
            u_salesextdescription="Detailed web copy for this product.",
        )
        ctx = self._make_ctx()
        extracted = _make_extracted([sap_product], company_id=self.env.company.id)

        result = self.importer.transform_products(ctx, extracted)

        self.assertEqual(len(result), 1)
        vals = result[0]

        # name must come from itemname (frgnname goes to description_sale instead;
        # see task 3923).
        self.assertEqual(
            vals["name"],
            "ItemName Internal",
            "name must be itemname; frgnname no longer participates in name.",
        )
        # default_code must come from suppcatnum (per task 3626 contract)
        self.assertEqual(
            vals["default_code"],
            "REF-001",
            "default_code must be the supplier catalog number (suppcatnum).",
        )
        # description_ecommerce must be the extended description
        self.assertEqual(
            vals["description_ecommerce"],
            "Detailed web copy for this product.",
            "description_ecommerce must be set to u_salesextdescription.",
        )
