"""No automatic stock-valuation lines on imported history.

Acceptance criteria
-------------------
- With real-time (perpetual) valuation, Odoo adds COGS / stock-interim lines
  to every customer invoice and bill carrying a storable product. On imported
  history those lines are invented numbers the Sage ledger never had, so the
  document import refuses to run while any imported product is valued in
  real time — before creating anything.
- With periodic valuation (or no stock valuation installed) it runs.

Needs `stock_account` for the real-time case; skipped where it is not
installed, since the guard is then vacuous.
"""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


@tagged("post_install", "-at_install")
class TestValuationGuard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ctx = ETLContext(cr=None, env=self.env, source_config={
            "company_id": self.env.company.id,
        })
        self.importer = self.env["sage.open.item.importer"]
        self.product = self.env["product.product"].create({
            "name": "Guard product",
        })

    def test_periodic_valuation_passes(self):
        # Nothing to raise: a plain product has no real-time valuation.
        self.importer._check_no_realtime_valuation(self.ctx, self.product)

    def test_real_time_valuation_is_refused(self):
        if "valuation" not in self.env["product.product"]._fields:
            self.skipTest("stock_account is not installed")
        category = self.env["product.category"].create({
            "name": "Perpetual",
            "property_valuation": "real_time",
        })
        self.product.write({"categ_id": category.id, "is_storable": True})
        if self.product.valuation != "real_time":
            self.skipTest("this Odoo decides valuation elsewhere")
        with self.assertRaises(UserError):
            self.importer._check_no_realtime_valuation(
                self.ctx, self.product,
            )
