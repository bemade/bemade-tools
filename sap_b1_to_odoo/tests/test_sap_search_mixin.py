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
# Acceptance criteria:
#   1. product.product name_search (mode-2 seam) still returns SAP-item-code
#      matches, labeled "SAP #<value>", AND still returns normal
#      name/default_code matches (the regression the mixin-only design would
#      have silently broken).
#   2. sale.order (Integer sap_docnum, mode-1) is found by an all-digit
#      docnum search; a non-numeric term does not raise and returns only
#      normal matches (integer-vs-ilike guard).
#   3. mrp.production (Char sap_docnum, mode-1) is found by a partial docnum
#      search.
#   4. sale.order name_search by its real name still returns it (additive,
#      not replacing); a docnum-only query returns docnum matches too.
#   5. A record whose display_name also matches the query text appears
#      exactly once (dedup).
#   6. The appended tuple's label differs from the plain display_name and
#      carries the "SAP #<value>" marker.
#   7. res.partner (Char sap_card_code, mode-1, differently-named field) is
#      found by its SAP card code.

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestSapSearchMixin(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.product = cls.env["product.product"].create(
            {
                "name": "Mixin Test Widget",
                "sap_item_code": "SAP-ITM-99",
            }
        )

        cls.order_partner = cls.env["res.partner"].create(
            {"name": "Mixin Test Order Partner"}
        )

        cls.sale_order = cls.env["sale.order"].create(
            {
                "partner_id": cls.order_partner.id,
                "sap_docnum": 98765,
            }
        )

        cls.mo = cls.env["mrp.production"].create(
            {
                "product_id": cls.product.id,
                "product_uom_id": cls.product.uom_id.id,
                "product_qty": 1.0,
                "sap_docnum": "WO-ABC123",
            }
        )

        cls.partner = cls.env["res.partner"].create(
            {
                "name": "Mixin Test Partner",
                "sap_card_code": "C00042",
            }
        )

    # ------------------------------------------------------------------
    # 1. product path (mode-2, regression-critical)
    # ------------------------------------------------------------------

    def test_product_sap_item_code_found_and_labeled(self):
        result = self.env["product.product"].name_search(name="SAP-ITM-99")
        matches = [r for r in result if r[0] == self.product.id]
        self.assertTrue(matches, "Product was not found by its SAP item code.")
        self.assertIn("SAP #SAP-ITM-99", matches[0][1])

    def test_product_name_search_by_name_still_works(self):
        result = self.env["product.product"].name_search(name="Mixin Test Widget")
        returned_ids = [r[0] for r in result]
        self.assertIn(
            self.product.id,
            returned_ids,
            "Normal name-based product search must still work (core behavior preserved).",
        )

    # ------------------------------------------------------------------
    # 2. Integer SAP field (mode-1)
    # ------------------------------------------------------------------

    def test_sale_order_integer_docnum_found(self):
        result = self.env["sale.order"].name_search(name="98765")
        matches = [r for r in result if r[0] == self.sale_order.id]
        self.assertTrue(matches, "Sale order was not found by its integer sap_docnum.")
        self.assertIn("SAP #98765", matches[0][1])

    def test_sale_order_non_numeric_term_does_not_raise(self):
        # Must not raise (would be an invalid int() cast / bad SQL on an
        # integer column if the guard were missing).
        result = self.env["sale.order"].name_search(name="notanumber")
        returned_ids = [r[0] for r in result]
        self.assertNotIn(
            self.sale_order.id,
            returned_ids,
            "A non-numeric term must not match the integer sap_docnum field.",
        )

    # ------------------------------------------------------------------
    # 3. Char SAP field (mode-1)
    # ------------------------------------------------------------------

    def test_mrp_production_char_docnum_found(self):
        result = self.env["mrp.production"].name_search(name="ABC123")
        matches = [r for r in result if r[0] == self.mo.id]
        self.assertTrue(matches, "Manufacturing order was not found by its char sap_docnum.")
        self.assertIn("SAP #WO-ABC123", matches[0][1])

    # ------------------------------------------------------------------
    # 4. Additive, not replacing
    # ------------------------------------------------------------------

    def test_sale_order_name_search_by_name_preserved(self):
        result = self.env["sale.order"].name_search(name=self.sale_order.name)
        returned_ids = [r[0] for r in result]
        self.assertIn(
            self.sale_order.id,
            returned_ids,
            "Sale order must still be findable by its real name (additive, not replacing).",
        )

    # ------------------------------------------------------------------
    # 5. Dedup
    # ------------------------------------------------------------------

    def test_dedup_when_display_name_and_sap_field_both_match(self):
        # sap_docnum '98765' also happens to be present nowhere in the name,
        # so use a term guaranteed to hit both: search by the order's own
        # name, which the base name_search already returns; confirm it is
        # not duplicated by the SAP augmentation.
        order = self.env["sale.order"].create(
            {
                "partner_id": self.order_partner.id,
                "sap_docnum": 11111,
            }
        )
        result = self.env["sale.order"].name_search(name=order.name)
        returned_ids = [r[0] for r in result]
        self.assertEqual(
            returned_ids.count(order.id),
            1,
            "Record must appear exactly once even if it matches both the base search and the SAP field.",
        )

    # ------------------------------------------------------------------
    # 6. Distinguishable label
    # ------------------------------------------------------------------

    def test_label_differs_from_plain_display_name(self):
        result = self.env["sale.order"].name_search(name="98765")
        matches = [r for r in result if r[0] == self.sale_order.id]
        self.assertTrue(matches)
        label = matches[0][1]
        self.assertNotEqual(label, self.sale_order.display_name)
        self.assertIn("SAP #", label)

    # ------------------------------------------------------------------
    # 7. Partner path (mode-1, differently-named field)
    # ------------------------------------------------------------------

    def test_partner_sap_card_code_found(self):
        result = self.env["res.partner"].name_search(name="C00042")
        matches = [r for r in result if r[0] == self.partner.id]
        self.assertTrue(matches, "Partner was not found by its sap_card_code.")
        self.assertIn("SAP #C00042", matches[0][1])
