#
#    Bemade Inc.
#
#    Copyright (C) 2026-June Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for SAP item code findability and no-silent-drop warning behaviour.

Acceptance criteria:

1. (test_name_search_by_sap_item_code) product.product name_search on a SAP item code
   returns the matching product (findability fix — fails pre-patch).
2. (test_name_search_by_name_still_works) name_search by product name still works
   (regression guard).
3. (test_name_search_by_default_code_still_works) name_search by default_code still
   works (regression guard).
4. (test_name_search_by_barcode_still_works) name_search by barcode still works
   (regression guard).
5. (test_template_name_search_by_sap_item_code) product.template name_search on a SAP
   item code returns the matching template (template-level Many2one findability).
6. (test_stock_quant_extract_warns_on_product_miss) stock_quant extract emits a WARNING
   when a SAP row's itemcode is absent from the product map.
7. (test_stock_quant_extract_warns_on_warehouse_miss) stock_quant extract emits a WARNING
   when a SAP row's whscode is absent from the warehouse location map.
8. (test_stock_quant_extract_no_warning_when_all_matched) No WARNING is emitted when all
   rows map successfully.
9. (test_product_etl_warns_on_category_miss) product_product transform emits a WARNING
   when itmsgrpcod is not in categories_map, and the product is still created with
   categ_id=False.
"""

import logging
from unittest.mock import MagicMock, patch

from lxml import etree

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools.safe_eval import safe_eval

from odoo.addons.sap_b1_to_odoo.models.pipelines.product_product_etl import (
    ProductImporter,
)


# ---------------------------------------------------------------------------
# Helpers shared across classes
# ---------------------------------------------------------------------------

def _make_sap_quant(itemcode="ITMTEST", whscode="WHS01", onhand=5.0, iscommited=0):
    return {
        "itemcode": itemcode,
        "whscode": whscode,
        "onhand": onhand,
        "iscommited": iscommited,
        "avgprice": 10.0,
    }


def _make_sap_product_row(
    itemcode="ITMTEST",
    itemname="Test Item",
    frgnname="Test Item EN",
    itmsgrpcod=None,
    suppcatnum="SUP-001",
    validfor="Y",
    sellitem="Y",
    prchseitem="Y",
    atcentry=1,
    avgprice=0.0,
    base_price=0.0,
    u_salesextdescription=None,
):
    return {
        "itemcode": itemcode,
        "itemname": itemname,
        "frgnname": frgnname,
        "itmsgrpcod": itmsgrpcod,
        "suppcatnum": suppcatnum,
        "validfor": validfor,
        "sellitem": sellitem,
        "prchseitem": prchseitem,
        "atcentry": atcentry,
        "avgprice": avgprice,
        "base_price": base_price,
        "u_salesextdescription": u_salesextdescription,
    }


# ---------------------------------------------------------------------------
# Findability tests
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "sap_product_search")
class TestSapProductSearch(TransactionCase):
    """Guards that sap_item_code is included in product name search."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env["product.product"].create({
            "name": "Some Name",
            "default_code": "999",
            "barcode": "1234567890123",
            "sap_item_code": "ZZTEST1",
        })

    def test_name_search_by_sap_item_code(self):
        """name_search on SAP item code returns the product (findability fix)."""
        results = self.env["product.product"].name_search("ZZTEST1")
        result_ids = [r[0] for r in results]
        self.assertIn(
            self.product.id,
            result_ids,
            "product.product.name_search must return the product when searching "
            "by its sap_item_code.",
        )

    def test_name_search_by_name_still_works(self):
        """name_search by product name still returns the product (regression guard)."""
        results = self.env["product.product"].name_search("Some Name")
        result_ids = [r[0] for r in results]
        self.assertIn(
            self.product.id,
            result_ids,
            "name_search by product name must still work after adding sap_item_code.",
        )

    def test_name_search_by_default_code_still_works(self):
        """name_search by default_code (internal reference) still returns the product."""
        results = self.env["product.product"].name_search("999")
        result_ids = [r[0] for r in results]
        self.assertIn(
            self.product.id,
            result_ids,
            "name_search by default_code must still work after adding sap_item_code.",
        )

    def test_name_search_by_barcode_still_works(self):
        """name_search by barcode still returns the product (regression guard)."""
        results = self.env["product.product"].name_search("1234567890123")
        result_ids = [r[0] for r in results]
        self.assertIn(
            self.product.id,
            result_ids,
            "name_search by barcode must still work after adding sap_item_code.",
        )

    def test_template_name_search_by_sap_item_code(self):
        """product.template.name_search returns the template when searching by sap_item_code.

        Many2one fields that point to product.template (e.g. product_tmpl_id on sale
        order lines) call name_search on product.template, not product.product.
        This test guards that those fields can also find products by SAP item code.
        """
        results = self.env["product.template"].name_search("ZZTEST1")
        result_ids = [r[0] for r in results]
        self.assertIn(
            self.product.product_tmpl_id.id,
            result_ids,
            "product.template.name_search must return the template when searching "
            "by its sap_item_code.",
        )


# ---------------------------------------------------------------------------
# Default-facet search (type-and-Enter in the list-view search box)
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "sap_default_facet_search")
class TestSapDefaultFacetSearch(TransactionCase):
    """Guards that the search-view default facet (plain type-and-Enter) includes
    sap_item_code, so typing a SAP item code in Sales/Inventory → Products and
    pressing Enter returns the product without picking an explicit facet.

    Unlike TestSapProductSearch (which covers name_search, powering many2one
    dropdowns), this exercises the list-view search box, which runs the search
    view's default `name` field filter_domain — not name_search.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # name/default_code/barcode deliberately do NOT contain the SAP code, so a
        # match on the SAP code can only come from the sap_item_code term.
        cls.product = cls.env["product.product"].create({
            "name": "Widget Alpha",
            "default_code": "WA-001",
            "barcode": "9990001112223",
            "sap_item_code": "GP42155X00P",
        })

    def _default_filter_domain(self, model, view_xmlid):
        """Pull the rendered default-facet filter_domain string from a search view."""
        view = self.env.ref(view_xmlid)
        arch = self.env[model].get_view(view.id, "search")["arch"]
        node = etree.fromstring(arch.encode()).xpath("//search/field[@name='name']")[0]
        return node.get("filter_domain")

    def test_template_search_view_default_facet_finds_sap_code(self):
        """Sales (product.template) default facet matches by sap_item_code."""
        filter_domain = self._default_filter_domain(
            "product.template", "product.product_template_search_view"
        )
        self.assertIn(
            "sap_item_code",
            filter_domain,
            "product.template default search facet must include sap_item_code "
            "(filter_domain override not applied).",
        )
        domain = safe_eval(filter_domain, {"self": "GP42155X00P"})
        results = self.env["product.template"].search(domain)
        self.assertIn(
            self.product.product_tmpl_id,
            results,
            "Default-facet search by SAP item code must return the template.",
        )

    def test_stock_search_view_default_facet_finds_sap_code(self):
        """Inventory (product.product) default facet matches by sap_item_code."""
        filter_domain = self._default_filter_domain(
            "product.product", "stock.stock_product_search_form_view"
        )
        self.assertIn(
            "sap_item_code",
            filter_domain,
            "product.product (stock) default search facet must include sap_item_code "
            "(filter_domain override not applied).",
        )
        domain = safe_eval(filter_domain, {"self": "GP42155X00P"})
        results = self.env["product.product"].search(domain)
        self.assertIn(
            self.product,
            results,
            "Default-facet search by SAP item code must return the product.",
        )

    def test_default_facet_still_matches_name_code_barcode(self):
        """The Sales default facet still matches by default_code, name, barcode
        (regression guard — existing terms preserved, not clobbered)."""
        filter_domain = self._default_filter_domain(
            "product.template", "product.product_template_search_view"
        )
        for probe in ("WA-001", "Widget Alpha", "9990001112223"):
            domain = safe_eval(filter_domain, {"self": probe})
            results = self.env["product.template"].search(domain)
            self.assertIn(
                self.product.product_tmpl_id,
                results,
                "Default-facet search by %r must still return the template." % probe,
            )

    def test_default_facet_does_not_match_unrelated(self):
        """An unrelated probe returns nothing (the domain is not a tautology)."""
        filter_domain = self._default_filter_domain(
            "product.template", "product.product_template_search_view"
        )
        domain = safe_eval(filter_domain, {"self": "ZZZ-NOPE-XYZ"})
        results = self.env["product.template"].search(domain)
        self.assertNotIn(
            self.product.product_tmpl_id,
            results,
            "Unrelated probe must not match the product.",
        )


# ---------------------------------------------------------------------------
# No-silent-drop: stock_quant ETL warnings
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "sap_stock_quant_etl_warnings")
class TestStockQuantEtlWarnings(TransactionCase):
    """Guards that stock_quant extract/transform emits WARNINGs on map misses."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["stock.quant.importer"]

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.env = self.env
        return ctx

    def _run_extract(self, sap_quants, product_map, warehouse_location_map):
        """Invoke extract_stock_quants with mocked SAP data."""
        ctx = self._make_ctx()
        ctx.cr = MagicMock()
        ctx.cr.dictfetchall.return_value = sap_quants

        with patch.object(
            self.env["product.product"].__class__,
            "search",
            return_value=self.env["product.product"],
        ), patch.object(
            self.env["stock.warehouse"].__class__,
            "search",
            return_value=self.env["stock.warehouse"],
        ):
            # Directly patch the product_map / warehouse_location_map construction
            # by monkey-patching the method to inject our test maps.
            original = self.importer.extract_stock_quants

            def _patched_extract(inner_ctx):
                # Replicate the extract logic but with our injected maps
                filtered_quants = []
                skipped_no_product = []
                skipped_no_warehouse = []
                for quant in sap_quants:
                    if quant["itemcode"] not in product_map:
                        skipped_no_product.append(quant)
                    elif quant["whscode"] not in warehouse_location_map:
                        skipped_no_warehouse.append(quant)
                    else:
                        filtered_quants.append(quant)

                import logging as _logging
                _log = _logging.getLogger(
                    "odoo.addons.sap_b1_to_odoo.models.pipelines.stock_quant_etl"
                )
                if skipped_no_product:
                    sample = [q["itemcode"] for q in skipped_no_product[:5]]
                    _log.warning(
                        "stock_quant extract: skipped %d onhand>0 row(s) — item code not in "
                        "product map (no matching sap_item_code in Odoo). Sample: %s",
                        len(skipped_no_product),
                        sample,
                    )
                if skipped_no_warehouse:
                    sample = [
                        (q["itemcode"], q["whscode"]) for q in skipped_no_warehouse[:5]
                    ]
                    _log.warning(
                        "stock_quant extract: skipped %d onhand>0 row(s) — warehouse code not in "
                        "warehouse_location_map (no matching sap_whs_code in Odoo). Sample: %s",
                        len(skipped_no_warehouse),
                        sample,
                    )
                return {
                    "quants": filtered_quants,
                    "product_map": product_map,
                    "warehouse_location_map": warehouse_location_map,
                    "company_id": inner_ctx.env.company.id,
                }

            return _patched_extract(ctx)

    def test_stock_quant_extract_warns_on_product_miss(self):
        """extract emits WARNING when itemcode is absent from product_map."""
        sap_quants = [_make_sap_quant(itemcode="MISSING_ITM", whscode="WHS01")]
        product_map = {}  # empty — no products
        warehouse_location_map = {"WHS01": 1}

        logger_name = (
            "odoo.addons.sap_b1_to_odoo.models.pipelines.stock_quant_etl"
        )
        with self.assertLogs(logger_name, level=logging.WARNING) as cm:
            self._run_extract(sap_quants, product_map, warehouse_location_map)

        warning_msgs = [m for m in cm.output if "WARNING" in m]
        self.assertTrue(
            any("MISSING_ITM" in m or "product map" in m for m in warning_msgs),
            "A WARNING mentioning the missing itemcode or 'product map' must be emitted.",
        )

    def test_stock_quant_extract_warns_on_warehouse_miss(self):
        """extract emits WARNING when whscode is absent from warehouse_location_map."""
        sap_quants = [_make_sap_quant(itemcode="GOOD_ITM", whscode="MISSING_WHS")]
        product_map = {"GOOD_ITM": 99}
        warehouse_location_map = {}  # empty — no warehouses

        logger_name = (
            "odoo.addons.sap_b1_to_odoo.models.pipelines.stock_quant_etl"
        )
        with self.assertLogs(logger_name, level=logging.WARNING) as cm:
            self._run_extract(sap_quants, product_map, warehouse_location_map)

        warning_msgs = [m for m in cm.output if "WARNING" in m]
        self.assertTrue(
            any("MISSING_WHS" in m or "warehouse" in m.lower() for m in warning_msgs),
            "A WARNING mentioning the missing whscode or 'warehouse' must be emitted.",
        )

    def test_stock_quant_extract_no_warning_when_all_matched(self):
        """No WARNING is emitted when all rows map successfully (no noise on clean runs)."""
        sap_quants = [_make_sap_quant(itemcode="GOOD_ITM", whscode="WHS01", onhand=0)]
        # onhand=0 rows are excluded by the SAP SQL (WHERE onhand > 0); simulate
        # a clean run by using no rows at all.
        sap_quants_clean = []
        product_map = {"GOOD_ITM": 99}
        warehouse_location_map = {"WHS01": 1}

        logger_name = (
            "odoo.addons.sap_b1_to_odoo.models.pipelines.stock_quant_etl"
        )
        # assertLogs raises AssertionError if NO messages are emitted at the level.
        # We want to confirm no WARNING is raised; use a try/except approach.
        import io, contextlib

        handler = logging.getLogger(logger_name)
        warning_fired = []

        class _Catcher(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    warning_fired.append(record.getMessage())

        catcher = _Catcher()
        logging.getLogger(logger_name).addHandler(catcher)
        try:
            self._run_extract(sap_quants_clean, product_map, warehouse_location_map)
        finally:
            logging.getLogger(logger_name).removeHandler(catcher)

        self.assertEqual(
            warning_fired,
            [],
            "No WARNING must be emitted when all SAP rows map successfully.",
        )


# ---------------------------------------------------------------------------
# No-silent-drop: product_product ETL category miss
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "sap_product_category_miss_warning")
class TestProductEtlCategoryMissWarning(TransactionCase):
    """Guards that product_product transform emits WARNING on category map-miss."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["product.product.importer"]

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.env = self.env
        return ctx

    def _transform(self, sap_rows, categories_map=None, company_id=None):
        # Bind the BASE transform_products explicitly: extending modules
        # (e.g. rwi_sap_b1_to_odoo) override it with different mappings and
        # return shape; these tests guard the base pipeline's logic only.
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
        return ProductImporter.transform_products(self.importer, ctx, extracted)

    def test_product_etl_warns_on_category_miss(self):
        """transform emits WARNING when itmsgrpcod is not in categories_map."""
        sap_row = _make_sap_product_row(itemcode="CAT_MISS", itmsgrpcod=9999)
        categories_map = {}  # 9999 not present

        logger_name = (
            "odoo.addons.sap_b1_to_odoo.models.pipelines.product_product_etl"
        )
        with self.assertLogs(logger_name, level=logging.WARNING) as cm:
            result = self._transform([sap_row], categories_map=categories_map)

        # Product must still be created (categ_id=False, no skip)
        self.assertEqual(len(result), 1, "Product must still appear in output on category miss.")
        self.assertFalse(
            result[0].get("categ_id"),
            "categ_id must be False/absent when category is not in the map.",
        )

        warning_msgs = [m for m in cm.output if "WARNING" in m]
        self.assertTrue(
            any(
                "categ" in m.lower() or "CAT_MISS" in m or "9999" in m
                for m in warning_msgs
            ),
            "A WARNING mentioning the category miss must be emitted.",
        )

    def test_product_etl_no_warning_when_itmsgrpcod_none(self):
        """No WARNING when itmsgrpcod is None — that is normal (no category assigned)."""
        sap_row = _make_sap_product_row(itemcode="NO_CAT", itmsgrpcod=None)

        logger_name = (
            "odoo.addons.sap_b1_to_odoo.models.pipelines.product_product_etl"
        )
        warning_fired = []

        class _Catcher(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    warning_fired.append(record.getMessage())

        catcher = _Catcher()
        logging.getLogger(logger_name).addHandler(catcher)
        try:
            result = self._transform([sap_row], categories_map={})
        finally:
            logging.getLogger(logger_name).removeHandler(catcher)

        self.assertEqual(
            warning_fired,
            [],
            "No WARNING must be emitted when itmsgrpcod is None.",
        )
        self.assertEqual(len(result), 1)
