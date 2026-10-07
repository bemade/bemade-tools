"""Sales orders still open in Sage at cutover -> confirmed Odoo orders.

Acceptance criteria
-------------------
- Only orders Sage has not cleared, and not quotes, are imported; historical
  orders are not (Sage keeps no order -> invoice link to match them with).
- Each line comes in for what is still to deliver:
  - a line nothing has been delivered on: its full ordered quantity;
  - a line sold by weight (kg) with something delivered: done — the order
    was for an estimated weight and the delivery is the actual one;
  - a line sold by count (caisse, sac, chaque, ...) with part delivered: the
    rest, still to deliver;
  - but a count line whose delivered quantity is fractional was weighed, not
    counted (ordered 12 "chaque", delivered 5.418 kg): done;
  - anything delivered in full or over: done.
- An order with nothing left to deliver is not imported.
- The order is confirmed (Odoo creates the deliveries), keeps Sage's order
  number as the customer reference, its order date (which confirming would
  otherwise reset to "now") and its requested ship date.
- Where Sage's unit is not the product's Odoo unit (sold by the caisse, stocked
  in kg), the line keeps Sage's unit in its description and the import reports
  it: the conversion factor is not in the Sage file.
- Re-running the import does not create the order twice.
"""

from odoo.tests import BaseCase, TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext
from odoo.addons.sage50_to_odoo.models.pipelines.sale_order_etl import (
    remaining_quantity,
    same_unit,
)

WEIGHT = {"kg", "kilo", "kilos", "kg cuit"}


def _line(ordered, delivered, units):
    return {"dOrdered": ordered, "dQuantity": delivered, "sUnits": units}


class TestRemainingQuantity(BaseCase):

    def test_untouched_line_is_ordered_quantity(self):
        self.assertEqual(remaining_quantity(_line(160, 0, "caisse"), WEIGHT), 160)
        self.assertEqual(remaining_quantity(_line(30, 0, "kg"), WEIGHT), 30)

    def test_weight_line_with_a_delivery_is_done(self):
        self.assertEqual(remaining_quantity(_line(35, 34.589, "kg"), WEIGHT), 0)
        self.assertEqual(remaining_quantity(_line(20, 5, "KILO"), WEIGHT), 0)

    def test_count_line_partial_keeps_the_rest(self):
        self.assertEqual(remaining_quantity(_line(36, 28, "caisse"), WEIGHT), 8)
        self.assertEqual(remaining_quantity(_line(10, 4, "sac"), WEIGHT), 6)

    def test_weighed_count_line_is_done(self):
        self.assertEqual(
            remaining_quantity(_line(12, 5.418, "chaque"), WEIGHT), 0,
        )

    def test_over_delivered_is_done(self):
        self.assertEqual(remaining_quantity(_line(2000, 2723, "chaque"), WEIGHT), 0)


@tagged("post_install", "-at_install")
class TestSameUnit(TransactionCase):
    """Sage's unit names are free text in the file's language; only a real
    difference (a caisse on a kg product) needs a conversion factor."""

    def test_synonyms_are_the_same_unit(self):
        units = self.env.ref("uom.product_uom_unit")
        kg = self.env.ref("uom.product_uom_kgm")
        self.assertTrue(same_unit("Chaque", units))
        self.assertTrue(same_unit("unité", units))
        self.assertTrue(same_unit("KILO", kg))
        self.assertTrue(same_unit("kg", kg))

    def test_case_on_a_weight_product_is_not(self):
        units = self.env.ref("uom.product_uom_unit")
        kg = self.env.ref("uom.product_uom_kgm")
        self.assertFalse(same_unit("caisse", kg))
        self.assertFalse(same_unit("chaque", kg))
        self.assertFalse(same_unit("caisse", units))


@tagged("post_install", "-at_install")
class TestOpenSaleOrderLoad(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({
            "name": "Open order customer", "sage_customer_id": 4101,
        })
        self.kg = self.env.ref("uom.product_uom_kgm")
        self.product = self.env["product.product"].create({
            "name": "Lentils", "sage_product_id": 802, "uom_id": self.kg.id,
        })
        self.ctx = ETLContext(cr=None, env=self.env, source_config={
            "company_id": self.env.company.id,
        })
        self.importer = self.env["sage.sale.order.importer"]

    def _order(self, **overrides):
        order = {
            "sage_order_id": 9001,
            "number": "700100",
            "sage_customer_id": 4101,
            "date": "2024-08-20",
            "ship_date": "2024-08-27",
            "lines": [{
                "sage_product_id": 802, "description": "Lentils (4/2kg)",
                "quantity": 160.0, "price_unit": 40.0, "units": "caisse",
            }],
        }
        order.update(overrides)
        return order

    def test_open_order_is_confirmed_with_its_remaining_lines(self):
        self.importer._load_orders(self.ctx, [self._order()])
        order = self.env["sale.order"].search([("sage_order_id", "=", 9001)])
        self.assertEqual(len(order), 1)
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.client_order_ref, "700100")
        self.assertEqual(str(order.commitment_date.date()), "2024-08-27")
        # Confirming resets the order date to "now" in Odoo; Sage's stays.
        self.assertEqual(str(order.date_order.date()), "2024-08-20")
        self.assertEqual(order.order_line.product_uom_qty, 160.0)
        self.assertAlmostEqual(order.order_line.price_unit, 40.0)
        # Sold by the caisse, stocked in kg: the Sage unit stays visible.
        self.assertIn("caisse", order.order_line.name)

    def test_archived_product_still_fills_its_open_order(self):
        """A product the client no longer sells is imported archived, but an
        order taken before it was dropped is still delivered."""
        self.product.product_tmpl_id.active = False
        self.importer._load_orders(self.ctx, [self._order()])
        order = self.env["sale.order"].search([("sage_order_id", "=", 9001)])
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.order_line.product_id, self.product)
        if "picking_ids" in order._fields:  # deliveries need sale_stock
            self.assertEqual(
                order.picking_ids.move_ids.product_id, self.product,
            )

    def test_rerun_does_not_duplicate(self):
        self.importer._load_orders(self.ctx, [self._order()])
        self.importer._load_orders(self.ctx, [self._order()])
        self.assertEqual(
            self.env["sale.order"].search_count([("sage_order_id", "=", 9001)]),
            1,
        )

    def test_order_with_nothing_left_is_skipped(self):
        self.importer._load_orders(self.ctx, [self._order(lines=[])])
        self.assertFalse(
            self.env["sale.order"].search([("sage_order_id", "=", 9001)])
        )
