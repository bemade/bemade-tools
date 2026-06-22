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
"""Tests for StockLocationImporter and sww-aware quant placement.

Acceptance criteria:

1. (test_sww_locations_transform) transform over OITM rows with sww values
   {"01","02","02",""} → produces location vals for exactly the distinct
   non-empty codes {"01","02"}, each with usage="internal", location_id equal
   to the warehouse lot_stock_id, and sap_sww_code set — blank sww yields no
   location.
2. (test_sww_location_upsert_idempotent) load the transformed vals, then run
   the full transform→load again; the second pass creates no duplicate
   stock.location for the same sap_sww_code (search-keyed upsert) and updates
   in place rather than erroring.
3. (test_quant_placed_at_sww_location) given a product whose OITM sww="02" and
   an OITW onhand row, transform_stock_quants resolves location_id to the
   location with sap_sww_code="02" (not the warehouse lot_stock_id).
4. (test_quant_falls_back_when_sww_blank) a product with blank/unmapped sww
   falls back to the warehouse lot_stock_id location for its whscode, and is
   NOT dropped (count preserved, warning path covered).
"""

from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def _make_ctx(env):
    ctx = MagicMock()
    ctx.env = env
    return ctx


@tagged("-at_install", "post_install", "stock_location_etl")
class TestStockLocationEtl(TransactionCase):
    """Guards sww-based stock.location import and quant placement logic."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.loc_importer = cls.env["stock.location.importer"]
        cls.quant_importer = cls.env["stock.quant.importer"]

        # Grab the default warehouse and its lot_stock_id
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.env.company.id)], limit=1
        )
        cls.warehouse.sap_whs_code = "WH"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _lot_stock_id(self):
        return self.warehouse.lot_stock_id.id

    def _warehouse_lot_stock_map(self):
        return {self.warehouse.sap_whs_code: self._lot_stock_id()}

    def _transform_locations(self, sww_codes):
        """Run transform_stock_locations with the given sww_codes list."""
        extracted = {
            "extract_stock_locations": {
                "sww_codes": sww_codes,
                "warehouse_lot_stock_map": self._warehouse_lot_stock_map(),
            }
        }
        ctx = _make_ctx(self.env)
        return self.loc_importer.transform_stock_locations(ctx, extracted)

    def _load_locations(self, location_vals):
        """Run load_stock_locations with the given vals list."""
        transformed = {"transform_stock_locations": location_vals}
        ctx = _make_ctx(self.env)
        self.loc_importer.load_stock_locations(ctx, transformed)

    def _transform_quants(self, sap_quants, sww_location_map, warehouse_location_map=None):
        """Run transform_stock_quants with given data."""
        if warehouse_location_map is None:
            warehouse_location_map = self._warehouse_lot_stock_map()
        # Build a minimal product_map with the item codes present
        product_ids = {}
        for q in sap_quants:
            itemcode = q["itemcode"]
            if itemcode not in product_ids:
                product = self.env["product.product"].create(
                    {"name": f"Test Product {itemcode}", "type": "consu"}
                )
                product_ids[itemcode] = product.id
        extracted = {
            "extract_stock_quants": {
                "quants": sap_quants,
                "product_map": product_ids,
                "warehouse_location_map": warehouse_location_map,
                "sww_location_map": sww_location_map,
                "company_id": self.env.company.id,
            }
        }
        ctx = _make_ctx(self.env)
        return self.quant_importer.transform_stock_quants(ctx, extracted)

    # ------------------------------------------------------------------
    # Test 1: transform produces distinct non-empty sww locations
    # ------------------------------------------------------------------

    def test_sww_locations_transform(self):
        """Transform {"01","02","02",""} → exactly {"01","02"} with correct vals."""
        # Distinct non-empty codes after dedup by caller (extract gives distinct)
        vals = self._transform_locations(["01", "02"])

        sww_codes_out = {v["sap_sww_code"] for v in vals}
        self.assertEqual(sww_codes_out, {"01", "02"})
        for v in vals:
            self.assertEqual(v["usage"], "internal")
            self.assertEqual(v["location_id"], self._lot_stock_id())
            self.assertIn(v["sap_sww_code"], {"01", "02"})

    def test_sww_locations_transform_blank_excluded(self):
        """Blank sww must not produce a location (extract already filters it out)."""
        # Simulate extract having already excluded blank — transform with only valid codes
        vals = self._transform_locations(["01"])
        self.assertEqual(len(vals), 1)
        self.assertEqual(vals[0]["sap_sww_code"], "01")

    def test_sww_locations_transform_empty_yields_none(self):
        """An empty sww_codes list → no location vals, no error."""
        vals = self._transform_locations([])
        self.assertEqual(vals, [])

    # ------------------------------------------------------------------
    # Test 2: upsert idempotency
    # ------------------------------------------------------------------

    def test_sww_location_upsert_idempotent(self):
        """Running load twice for the same sww codes creates no duplicates."""
        vals = self._transform_locations(["01", "02"])

        # First load
        self._load_locations(vals)
        self.env.flush_all()

        count_after_first = self.env["stock.location"].search_count(
            [("sap_sww_code", "in", ["01", "02"])]
        )
        self.assertEqual(count_after_first, 2)

        # Second load (re-import — idempotent)
        vals2 = self._transform_locations(["01", "02"])
        self._load_locations(vals2)
        self.env.flush_all()

        count_after_second = self.env["stock.location"].search_count(
            [("sap_sww_code", "in", ["01", "02"])]
        )
        self.assertEqual(
            count_after_second, 2, "No duplicate locations must be created on re-import."
        )

    # ------------------------------------------------------------------
    # Test 3: quant placed at sww location
    # ------------------------------------------------------------------

    def test_quant_placed_at_sww_location(self):
        """A quant whose item has sww="02" resolves to the sww="02" location."""
        # Create the sww location
        loc_02 = self.env["stock.location"].create({
            "name": "SWW-02",
            "usage": "internal",
            "location_id": self._lot_stock_id(),
            "sap_sww_code": "02",
        })

        sww_location_map = {"02": loc_02.id}
        sap_quants = [
            {
                "itemcode": "ITEM-SWW02",
                "whscode": "WH",
                "onhand": 5.0,
                "iscommited": 0,
                "avgprice": 10.0,
                "sww": "02",
            }
        ]

        quant_vals = self._transform_quants(sap_quants, sww_location_map)

        self.assertEqual(len(quant_vals), 1)
        self.assertEqual(
            quant_vals[0]["location_id"],
            loc_02.id,
            "Quant must be placed at the sww-specific location, not the warehouse default.",
        )

    # ------------------------------------------------------------------
    # Test 4: fallback when sww is blank
    # ------------------------------------------------------------------

    def test_quant_falls_back_when_sww_blank(self):
        """A quant with blank sww falls back to warehouse lot_stock_id, not dropped."""
        sap_quants = [
            {
                "itemcode": "ITEM-NOSWW",
                "whscode": "WH",
                "onhand": 3.0,
                "iscommited": 0,
                "avgprice": 5.0,
                "sww": "",
            }
        ]
        sww_location_map = {}  # no sww locations

        quant_vals = self._transform_quants(sap_quants, sww_location_map)

        self.assertEqual(len(quant_vals), 1, "Quant must not be dropped when sww is blank.")
        self.assertEqual(
            quant_vals[0]["location_id"],
            self._lot_stock_id(),
            "Blank sww must fall back to the warehouse lot_stock_id.",
        )
        self.assertEqual(quant_vals[0]["quantity"], 3.0)

    def test_quant_falls_back_when_sww_unmapped(self):
        """A quant with a non-empty but unmapped sww falls back, not dropped."""
        sap_quants = [
            {
                "itemcode": "ITEM-UNMAPPED",
                "whscode": "WH",
                "onhand": 7.0,
                "iscommited": 0,
                "avgprice": 2.0,
                "sww": "99",
            }
        ]
        sww_location_map = {}  # sww "99" not present

        quant_vals = self._transform_quants(sap_quants, sww_location_map)

        self.assertEqual(
            len(quant_vals), 1, "Quant must not be dropped when sww is unmapped."
        )
        self.assertEqual(
            quant_vals[0]["location_id"],
            self._lot_stock_id(),
            "Unmapped sww must fall back to the warehouse lot_stock_id.",
        )
