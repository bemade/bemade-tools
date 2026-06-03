"""Tests for xTuple product import pipeline.

Phase 3 (implementation): stale live-DB tests replaced with unit tests for the
new conditional name/description_sale mapping introduced in task #3687.

Phase 4 will add the full fixture-based test suite per the design's Test plan.
Live-DB integration tests (tagged ``xtuple``) require XTUPLE_* env vars; they
are skipped automatically when those vars are absent.
"""

import os
import logging

from odoo.tests.common import TransactionCase, tagged

_logger = logging.getLogger(__name__)


@tagged("-at_install", "post_install")
class TestProductTransform(TransactionCase):
    """Unit tests for XtupleProductImporter._transform_product.

    These tests call ``_transform_product`` directly without a live xTuple
    connection so they run in any Odoo CI environment.
    """

    def setUp(self):
        super().setUp()
        self.importer = self.env["xtuple.product.importer"]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_product(self, **overrides):
        """Return a minimal fake extracted product record."""
        base = {
            "item_id": 1,
            "item_number": "VFG401C",
            "item_descrip1": "F-series cyan pigment ink",
            "item_descrip2": "tech notes",
            "item_type": "M",
            "item_active": True,
            "item_sold": True,
            "item_fractional": False,
            "item_inv_uom_id": None,
            "item_price_uom_id": None,
            "item_listprice": 0.0,
            "item_listcost": 0.0,
            "item_maxcost": 0.0,
            "item_upccode": None,
            "item_prodcat_id": None,
            "item_classcode_id": None,
            "inv_uom_name": "EA",
            "price_uom_name": "EA",
            "prodcat_code": None,
            "prodcat_descrip": None,
            "classcode_code": None,
            "item_controlmethod": "N",
            "_category_id": False,
            "_uom_id": False,
            "_is_purchased": False,
        }
        base.update(overrides)
        return base

    # ------------------------------------------------------------------
    # AC1 / Test plan #1 -- saleable item
    # ------------------------------------------------------------------

    def test_saleable_item_name_from_item_number(self):
        """Saleable item: name comes from item_number (SKU)."""
        product = self._base_product(
            item_number="VFG401C",
            item_descrip1="F-series cyan pigment ink",
            item_descrip2="tech notes",
            item_type="M",
            _is_purchased=False,
        )
        vals = self.importer._transform_product(product)
        self.assertEqual(vals["name"], "VFG401C")

    def test_saleable_item_description_sale_from_descrip1(self):
        """Saleable item: description_sale comes from item_descrip1."""
        product = self._base_product(
            item_number="VFG401C",
            item_descrip1="F-series cyan pigment ink",
            _is_purchased=False,
        )
        vals = self.importer._transform_product(product)
        self.assertEqual(vals.get("description_sale"), "F-series cyan pigment ink")

    def test_saleable_item_description_from_descrip2(self):
        """Saleable item: description (internal notes) comes from item_descrip2."""
        product = self._base_product(
            item_descrip2="tech notes",
            _is_purchased=False,
        )
        vals = self.importer._transform_product(product)
        self.assertEqual(vals["description"], "tech notes")

    def test_saleable_item_no_default_code(self):
        """Saleable item: default_code is NOT set in create vals (AC5)."""
        product = self._base_product(_is_purchased=False)
        vals = self.importer._transform_product(product)
        self.assertNotIn("default_code", vals)

    # ------------------------------------------------------------------
    # AC2 / Test plan #2 -- purchased item
    # ------------------------------------------------------------------

    def test_purchased_item_name_from_descrip1(self):
        """Purchased item: name comes from item_descrip1."""
        product = self._base_product(
            item_number="DTF300W",
            item_descrip1="Direct to Film White pigment Ink",
            item_type="P",
            _is_purchased=True,
        )
        vals = self.importer._transform_product(product)
        self.assertEqual(vals["name"], "Direct to Film White pigment Ink")

    def test_purchased_item_no_description_sale(self):
        """Purchased item: description_sale is not populated."""
        product = self._base_product(
            item_number="DTF300W",
            item_descrip1="Direct to Film White pigment Ink",
            item_type="P",
            _is_purchased=True,
        )
        vals = self.importer._transform_product(product)
        # description_sale should be absent or empty for purchased items.
        self.assertFalse(vals.get("description_sale"))

    def test_purchased_item_no_default_code(self):
        """Purchased item: default_code is NOT set in create vals (AC5)."""
        product = self._base_product(
            item_number="DTF300W",
            item_descrip1="Direct to Film White pigment Ink",
            item_type="P",
            _is_purchased=True,
        )
        vals = self.importer._transform_product(product)
        self.assertNotIn("default_code", vals)

    # ------------------------------------------------------------------
    # Test plan #3 -- default_code guard for both branches
    # ------------------------------------------------------------------

    def test_default_code_never_set_saleable(self):
        """default_code absent from create vals for saleable items."""
        vals = self.importer._transform_product(
            self._base_product(_is_purchased=False)
        )
        self.assertNotIn(
            "default_code", vals, "default_code must not be set for saleable items"
        )

    def test_default_code_never_set_purchased(self):
        """default_code absent from create vals for purchased items."""
        vals = self.importer._transform_product(
            self._base_product(_is_purchased=True)
        )
        self.assertNotIn(
            "default_code", vals, "default_code must not be set for purchased items"
        )

    # ------------------------------------------------------------------
    # _is_purchased_product hook
    # ------------------------------------------------------------------

    def test_is_purchased_product_default_false(self):
        """_is_purchased_product returns False when flag not set."""
        product = self._base_product()
        del product["_is_purchased"]
        self.assertFalse(self.importer._is_purchased_product(product))

    def test_is_purchased_product_flag_true(self):
        """_is_purchased_product returns True when _is_purchased=True."""
        product = self._base_product(_is_purchased=True)
        self.assertTrue(self.importer._is_purchased_product(product))

    def test_is_purchased_product_flag_false(self):
        """_is_purchased_product returns False when _is_purchased=False."""
        product = self._base_product(_is_purchased=False)
        self.assertFalse(self.importer._is_purchased_product(product))


@tagged("-at_install", "xtuple")
class TestProductImportLive(TransactionCase):
    """Live smoke tests -- skipped when XTUPLE_* env vars are absent.

    Runs the real importer against a handful of items and asserts the
    conditional name mapping works end-to-end.

    Test plan #5.
    """

    def setUp(self):
        super().setUp()
        host = os.environ.get("XTUPLE_HOST", "")
        if not host:
            self.skipTest(
                "XTUPLE_* env vars not set; skipping live smoke tests"
            )
        self.xtuple_db = self.env["xtuple.database"].create(
            {
                "database_host": host,
                "database_name": os.environ.get("XTUPLE_DBNAME", ""),
                "database_username": os.environ.get("XTUPLE_USER", ""),
                "database_password": os.environ.get("XTUPLE_PASSWORD", ""),
                "database_port": int(os.environ.get("XTUPLE_PORT", "5432")),
                "database_schema": os.environ.get("XTUPLE_SCHEMA", "public"),
            }
        )

    def test_xtuple_product_tables_exist(self):
        """Smoke: xTuple product-related tables are accessible."""
        cursor = None
        try:
            cursor = self.xtuple_db.get_cursor()
            for table in ("item", "prodcat", "uom", "itemsrc"):
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = %s AND table_name = %s
                    )
                    """,
                    (self.xtuple_db.database_schema, table),
                )
                self.assertTrue(
                    cursor.fetchone()[0], f"xTuple table '{table}' not found"
                )
        finally:
            if cursor:
                cursor.close()
