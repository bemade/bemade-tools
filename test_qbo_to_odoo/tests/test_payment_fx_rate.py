"""Test that ExchangeRateEnsurer.set_rate() causes a payment's journal
entry to use the exact QBO per-transaction exchange rate.

The pipeline sets the rate via raw SQL immediately before action_post().
This test verifies that currency._convert() picks up the upserted rate
(i.e. the ORM cache is properly invalidated).
"""

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPaymentFxRate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.usd = cls.env.ref("base.USD")
        cls.cad = cls.env.ref("base.CAD")
        cls.company = cls.env.company
        cls.company.currency_id = cls.cad

        cls.partner = cls.env["res.partner"].create({
            "name": "FX Payment Test Partner",
            "is_company": True,
        })

        # Use the 1020 (US DESJARDINS) bank journal if it exists,
        # otherwise fall back to any bank journal.
        cls.bank_journal = cls.env["account.journal"].search(
            [("code", "=", "1020"), ("type", "=", "bank")], limit=1,
        ) or cls.env["account.journal"].search(
            [("type", "=", "bank")], limit=1,
        )

    def _create_and_post_payment(self, amount_usd, date, qbo_rate):
        """Create a USD payment, set the rate, post, return the move."""
        from odoo.addons.qbo_to_odoo.models.pipelines.exchange_rate_helper import (
            ExchangeRateEnsurer,
        )

        payment = self.env["account.payment"].create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": self.partner.id,
            "amount": amount_usd,
            "currency_id": self.usd.id,
            "journal_id": self.bank_journal.id,
            "date": date,
        })
        # Direct-to-bank (matches QBO pipeline behaviour)
        payment.outstanding_account_id = self.bank_journal.default_account_id

        # Upsert the per-transaction rate — this is what the pipeline does
        ensurer = ExchangeRateEnsurer(self.env)
        ensurer.set_rate("USD", date, qbo_rate)

        payment.action_post()
        return payment

    def test_payment_uses_set_rate(self):
        """A single payment should use the rate we set."""
        date = "2019-06-15"
        qbo_rate = 1.25  # 1 USD = 1.25 CAD
        amount_usd = 1000.00
        expected_cad = 1250.00

        payment = self._create_and_post_payment(amount_usd, date, qbo_rate)
        move = payment.move_id
        self.assertTrue(move, f"No JE created (state={payment.state})")

        bank_line = move.line_ids.filtered(
            lambda l: l.account_id == self.bank_journal.default_account_id
        )
        self.assertTrue(bank_line, "No bank line found")
        self.assertAlmostEqual(
            bank_line[0].debit, expected_cad, delta=0.02,
            msg=f"Bank debit: got {bank_line[0].debit}, expected {expected_cad}",
        )

    def test_two_payments_same_date_different_rates(self):
        """Two payments on the same date with different QBO rates should
        each get their own CAD amount."""
        date = "2019-07-01"
        amount_usd = 1000.00

        pay_a = self._create_and_post_payment(amount_usd, date, qbo_rate=1.30)
        pay_b = self._create_and_post_payment(amount_usd, date, qbo_rate=1.40)

        self.assertTrue(pay_a.move_id, "Payment A has no JE")
        self.assertTrue(pay_b.move_id, "Payment B has no JE")

        bank_acct = self.bank_journal.default_account_id
        debit_a = pay_a.move_id.line_ids.filtered(
            lambda l: l.account_id == bank_acct
        )[0].debit
        debit_b = pay_b.move_id.line_ids.filtered(
            lambda l: l.account_id == bank_acct
        )[0].debit

        self.assertAlmostEqual(
            debit_a, 1300.00, delta=0.02,
            msg=f"Payment A should use rate 1.30 → 1300, got {debit_a}",
        )
        self.assertAlmostEqual(
            debit_b, 1400.00, delta=0.02,
            msg=f"Payment B should use rate 1.40 → 1400, got {debit_b}",
        )

    def test_payment_a_not_affected_by_later_rate_change(self):
        """After posting payment A, changing the rate for payment B
        must not retroactively alter A's amounts."""
        date = "2019-08-01"
        amount_usd = 1000.00

        pay_a = self._create_and_post_payment(amount_usd, date, qbo_rate=1.25)
        debit_a_before = pay_a.move_id.line_ids.filtered(
            lambda l: l.account_id == self.bank_journal.default_account_id
        )[0].debit

        # Post a second payment at a very different rate
        self._create_and_post_payment(amount_usd, date, qbo_rate=1.50)

        # Re-read A's debit — it must not have changed
        pay_a.move_id.invalidate_recordset()
        debit_a_after = pay_a.move_id.line_ids.filtered(
            lambda l: l.account_id == self.bank_journal.default_account_id
        )[0].debit

        self.assertAlmostEqual(
            debit_a_before, debit_a_after, delta=0.01,
            msg=f"Payment A debit changed from {debit_a_before} to "
                f"{debit_a_after} after rate update",
        )
