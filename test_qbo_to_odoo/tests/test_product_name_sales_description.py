"""Tests for QBO product import: saleable products take the sales description
as their name.

Acceptance criteria:
1. A saleable QBO item (Type in Service/NonInventory/Inventory) is transformed
   with ``name`` = its QBO Description (sales description), while
   ``description_sale`` is still populated with the same Description.
2. A non-saleable QBO item keeps ``name`` = its QBO Name.
3. A saleable QBO item whose Description is blank falls back to ``name`` = Name
   (the required name field is never left empty).
4. ``default_code`` is always the QBO Sku, untouched by the name logic.
"""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext
import odoo.addons.qbo_to_odoo.models.pipelines.product_etl as product_etl


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env.

    transform_items issues read-only SQL lookups for the account and category
    maps; against the clean test DB these return empty maps, which is fine for
    exercising the name / description / default_code mapping under test.  The
    income->category fallback (which joins on xtuple-owned columns not present
    when only qbo_to_odoo is installed) is stubbed to {} below; it is unrelated
    to the name logic under test.
    """
    return ETLContext(cr=env.cr, env=env)


def _extracted(items):
    return {"extract_items": items}


@tagged("post_install", "-at_install", "qbo")
class TestQboSaleableProductName(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["qbo.item.importer"]

    def _transform_one(self, item):
        ctx = _make_ctx(self.env)
        # Stub the income->category fallback: it joins on xtuple_item_id, an
        # xtuple_to_odoo column absent in a qbo_to_odoo-only test DB, and is
        # orthogonal to the name/description/default_code mapping under test.
        with patch.object(product_etl, "_build_income_to_categ", return_value={}):
            vals_list = self.importer.transform_items(ctx, _extracted([item]))
        self.assertEqual(len(vals_list), 1, "expected exactly one transformed product")
        return vals_list[0]

    def test_saleable_name_is_sales_description(self):
        """AC1 — saleable item: name=Description, description_sale=Description."""
        vals = self._transform_one(
            {
                "Id": "10",
                "Type": "Inventory",
                "Name": "WIDGET-SKU-001",
                "Description": "Premium widget, 200 micron",
                "Sku": "WIDGET-SKU-001",
            }
        )
        self.assertEqual(vals["name"], "Premium widget, 200 micron")
        self.assertEqual(vals["description_sale"], "Premium widget, 200 micron")
        self.assertTrue(vals["sale_ok"])

    def test_non_saleable_keeps_qbo_name(self):
        """AC2 — non-saleable item keeps name=Name."""
        vals = self._transform_one(
            {
                "Id": "11",
                "Type": "OtherCharge",
                "Name": "SHIPPING-FEE",
                "Description": "Freight charge",
                "Sku": "SHIP01",
            }
        )
        self.assertEqual(vals["name"], "SHIPPING-FEE")
        self.assertFalse(vals["sale_ok"])

    def test_saleable_blank_description_falls_back_to_name(self):
        """AC3 — saleable item with blank Description falls back to name=Name."""
        vals = self._transform_one(
            {
                "Id": "12",
                "Type": "NonInventory",
                "Name": "FALLBACK-NAME",
                "Description": "",
                "Sku": "FB01",
            }
        )
        self.assertEqual(vals["name"], "FALLBACK-NAME")
        self.assertTrue(vals["sale_ok"])

    def test_default_code_is_sku_untouched(self):
        """AC4 — default_code is always the Sku regardless of name logic."""
        saleable = self._transform_one(
            {
                "Id": "13",
                "Type": "Inventory",
                "Name": "NAME13",
                "Description": "Desc 13",
                "Sku": "SKU13",
            }
        )
        self.assertEqual(saleable["default_code"], "SKU13")
        non_saleable = self._transform_one(
            {
                "Id": "14",
                "Type": "OtherCharge",
                "Name": "NAME14",
                "Description": "Desc 14",
                "Sku": "SKU14",
            }
        )
        self.assertEqual(non_saleable["default_code"], "SKU14")
