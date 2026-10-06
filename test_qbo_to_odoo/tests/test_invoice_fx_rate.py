"""Test that a foreign invoice QBO booked at PAR (ExchangeRate 1.0) posts its
revenue/AR at the foreign amount (CAD == USD), instead of the prevailing daily
res.currency.rate.

Reproduces the mechanism of the invoice par-rate fix: the builder sets
``invoice_currency_rate`` for every foreign invoice (including par), and the
load phase pins the rate into res.currency.rate with ``force=True``. Together
these make Odoo compute CAD at the exact QBO rate rather than a daily rate.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.qbo_to_odoo.models.pipelines.exchange_rate_helper import (
    ExchangeRateEnsurer,
    qbo_rate_to_odoo,
)


@tagged("post_install", "-at_install")
class TestInvoiceParFxRate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.usd = cls.env.ref("base.USD")
        cls.cad = cls.env.ref("base.CAD")
        cls.company = cls.env.company
        cls.company.currency_id = cls.cad

        cls.partner = cls.env["res.partner"].create({
            "name": "FX Invoice Test Partner",
            "is_company": True,
        })
        cls.income = cls.env["account.account"].search(
            [("account_type", "=", "income")], limit=1,
        )

    def _seed_daily_rate(self, date, qbo_rate):
        """Seed a *conflicting* daily USD rate so that, absent a per-invoice
        pin, Odoo would translate at this (wrong) rate."""
        ExchangeRateEnsurer(self.env).set_rate("USD", date, qbo_rate, force=True)

    def _make_usd_invoice(self, amount_usd, date, invoice_currency_rate=None):
        vals = {
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
            "currency_id": self.usd.id,
            "invoice_date": date,
            "date": date,
            "invoice_line_ids": [(0, 0, {
                "name": "Widget",
                "quantity": 1,
                "price_unit": amount_usd,
                "account_id": self.income.id,
                "tax_ids": [(6, 0, [])],
            })],
        }
        if invoice_currency_rate is not None:
            vals["invoice_currency_rate"] = invoice_currency_rate
        return self.env["account.move"].create(vals)

    def _income_cad(self, move):
        line = move.line_ids.filtered(lambda l: l.account_id == self.income)
        return abs(line[0].balance)

    def test_par_invoice_posts_at_par(self):
        """A par (rate 1.0) USD invoice books CAD == USD, ignoring a
        conflicting daily rate of 1.40."""
        date = "2016-01-04"
        self._seed_daily_rate(date, qbo_rate=1.40)  # the "wrong" market rate

        inv = self._make_usd_invoice(
            1000.00, date,
            invoice_currency_rate=qbo_rate_to_odoo(1.0),  # what the builder sets
        )
        # Pin the par rate the way the load phase does (force=True).
        ExchangeRateEnsurer(self.env).set_rate("USD", date, 1.0, force=True)
        inv.action_post()

        self.assertAlmostEqual(
            self._income_cad(inv), 1000.00, delta=0.02,
            msg=f"Par invoice should book 1000 CAD, got {self._income_cad(inv)}",
        )

    def test_non_par_invoice_still_uses_its_rate(self):
        """A non-par (rate 1.25) USD invoice books CAD = USD * 1.25 — the fix
        must not disturb ordinary foreign invoices."""
        date = "2016-02-01"
        inv = self._make_usd_invoice(
            1000.00, date,
            invoice_currency_rate=qbo_rate_to_odoo(1.25),
        )
        ExchangeRateEnsurer(self.env).set_rate("USD", date, 1.25, force=True)
        inv.action_post()

        self.assertAlmostEqual(
            self._income_cad(inv), 1250.00, delta=0.02,
            msg=f"Non-par invoice should book 1250 CAD, got {self._income_cad(inv)}",
        )
