"""Tests for the uniform per-txn realized-FX true-up finalizer.

QBO books 100% of realized FX inside the driving payment-type transaction
(regular payments AND zero-total credit/debit-note applications); Odoo books
it as reconciliation exchange-difference entries at its own rates.  Every
reconciliation stamps its exchange moves with the driving QBO transaction
(``ref = QBO_EXCH:<Kind>:<qbo_txn_id>``), and the finalizer books, per cache
transaction::

    gap = QBO_exch(txn) - stamped_exchange_FX(txn)

to the exchange account against the AR/AP control account QBO's own GL used,
dated to the transaction.  Heuristic-retry exchange moves (``QBO_EXCH:RETRY``)
are reversed first since their FX cannot be attributed to a QBO transaction.

Acceptance criteria:
1. A cache txn with an FX loss and no stamped Odoo FX gets a QBO_FX_TRUEUP
   entry: Dr exchange-loss / Cr the txn's AR control account for the full
   gap, dated to the txn, control leg partner-less.
2. Sign: an FX *gain* (cache credit) books Dr AR-control / Cr exchange-gain.
3. Idempotent — a second run books nothing (guarded by the QBO_FX_TRUEUP ref).
4. Safe no-op when the company has no exchange accounts configured.
5. Stamped exchange FX offsets the gap: only the shortfall is booked.
6. QBO_EXCH:RETRY moves are reversed (QBO_FX_RETRY_REVERSAL mirror entries).
"""

import logging

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext

_logger = logging.getLogger(__name__)


def _ctx(env):
    return ETLContext(cr=None, env=env)


@tagged("post_install", "-at_install", "payment_fx_trueup")
class TestPaymentFxTrueup(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.finalizer = cls.env["qbo.account.finalizer"]
        cls.company = cls.env.company
        Account = cls.env["account.account"]

        # Exchange gain/loss account — configured on the company. Uses a unique
        # code to avoid colliding with a real chart account; cache lines below
        # are seeded with the SAME code the method derives from it.
        cls.exch_code = "FX5900TEST"
        cls.exch_acct = Account.create({
            "name": "Exchange Gain or Loss (test)",
            "code": cls.exch_code,
            "account_type": "expense",
        })
        cls.company.income_currency_exchange_account_id = cls.exch_acct.id
        cls.company.expense_currency_exchange_account_id = cls.exch_acct.id

        # AR control account — the finalizer resolves it from the cache txn's
        # own AR/AP line via the account code.
        cls.ar_code = "AR1100TEST"
        cls.ar_acct = Account.create({
            "name": "Accounts Receivable (test)",
            "code": cls.ar_code,
            "account_type": "asset_receivable",
            "reconcile": True,
        })

        # Exchange-difference journal, wired onto the company (the finalizer
        # reads company.currency_exchange_journal_id, not a hardcoded code).
        cls.exch_journal = cls.company.currency_exchange_journal_id or (
            cls.env["account.journal"].search(
                [("type", "=", "general"), ("company_id", "=", cls.company.id)],
                limit=1,
            )
            or cls.env["account.journal"].create({
                "name": "Exchange Difference (test)", "code": "EXCHT",
                "type": "general", "company_id": cls.company.id,
            })
        )
        cls.company.currency_exchange_journal_id = cls.exch_journal.id

        # Journal-cache rows require a connection record (never contacted).
        cls.qbo_connection = cls.env["qbo.connection"].create({
            "name": "FXTU test connection",
            "client_id": "test", "client_secret": "test",
        })

    def _seed_cache(self, qbo_id, txn_date, fx_debit=0.0, fx_credit=0.0,
                    txn_type="Payment"):
        """Seed a QBO cache txn: an exchange line + the AR control line."""
        cache = self.env["qbo.journal.cache"].create({
            "qbo_connection_id": self.qbo_connection.id,
        })
        txn = self.env["qbo.journal.cache.transaction"].create({
            "cache_id": cache.id, "qbo_txn_id": str(qbo_id),
            "txn_type": txn_type, "txn_date": txn_date,
        })
        self.env["qbo.journal.cache.line"].create({
            "transaction_id": txn.id, "account_code": self.exch_code,
            "debit": fx_debit, "credit": fx_credit,
        })
        # The AR counterpart QBO's GL carries (drives control-account choice).
        self.env["qbo.journal.cache.line"].create({
            "transaction_id": txn.id, "account_code": self.ar_code,
            "debit": fx_credit, "credit": fx_debit,
        })

    def _post_stamped(self, ref, fx_amount, date):
        """Post an exchange-style entry (exch vs AR) carrying *ref*."""
        move = self.env["account.move"].create({
            "move_type": "entry",
            "date": date,
            "journal_id": self.exch_journal.id,
            "ref": ref,
            "line_ids": [
                (0, 0, {"account_id": self.exch_acct.id,
                        "debit": max(fx_amount, 0.0),
                        "credit": max(-fx_amount, 0.0),
                        "name": "test stamped FX"}),
                (0, 0, {"account_id": self.ar_acct.id,
                        "debit": max(-fx_amount, 0.0),
                        "credit": max(fx_amount, 0.0),
                        "name": "test stamped FX"}),
            ],
        })
        move.action_post()
        return move

    def _trueups(self):
        return self.env["account.move"].search(
            [("ref", "=like", "QBO_FX_TRUEUP%")]
        )

    def test_ac1_books_full_gap_as_loss(self):
        # QBO booked a 100.00 FX loss (debit) Odoo never booked -> gap = +100.
        self._seed_cache(910001, "2024-03-15", fx_debit=100.0)

        self.finalizer._book_payment_fx_trueup(_ctx(self.env))

        tu = self._trueups()
        self.assertEqual(len(tu), 1, "exactly one true-up entry expected")
        self.assertEqual(str(tu.date), "2024-03-15",
                         "true-up must be dated to the QBO transaction")
        exch_leg = tu.line_ids.filtered(lambda l: l.account_id == self.exch_acct)
        ctrl_leg = tu.line_ids.filtered(lambda l: l.account_id == self.ar_acct)
        self.assertAlmostEqual(exch_leg.debit, 100.0, 2,
                               msg="loss must Dr the exchange account")
        self.assertAlmostEqual(ctrl_leg.credit, 100.0, 2,
                               msg="counterpart must Cr the AR control account")
        self.assertFalse(ctrl_leg.partner_id,
                         "control leg must carry no partner (aging untouched)")

    def test_ac2_gain_reverses_sign(self):
        # QBO booked a 60.00 FX gain (credit) -> gap = -60 -> Dr AR / Cr exch.
        self._seed_cache(910002, "2024-04-20", fx_credit=60.0)

        self.finalizer._book_payment_fx_trueup(_ctx(self.env))

        tu = self._trueups()
        self.assertEqual(len(tu), 1)
        exch_leg = tu.line_ids.filtered(lambda l: l.account_id == self.exch_acct)
        ctrl_leg = tu.line_ids.filtered(lambda l: l.account_id == self.ar_acct)
        self.assertAlmostEqual(exch_leg.credit, 60.0, 2,
                               msg="gain must Cr the exchange account")
        self.assertAlmostEqual(ctrl_leg.debit, 60.0, 2,
                               msg="counterpart must Dr the AR control account")

    def test_ac3_idempotent(self):
        self._seed_cache(910003, "2024-05-01", fx_debit=25.0)

        self.finalizer._book_payment_fx_trueup(_ctx(self.env))
        first = len(self._trueups())
        self.finalizer._book_payment_fx_trueup(_ctx(self.env))
        second = len(self._trueups())
        self.assertEqual(first, second, "second run must book nothing (idempotent)")

    def test_ac4_safe_without_exchange_accounts(self):
        # Unset the exchange accounts -> method must no-op, not raise.
        self.company.income_currency_exchange_account_id = False
        self.company.expense_currency_exchange_account_id = False
        try:
            self.finalizer._book_payment_fx_trueup(_ctx(self.env))
        except Exception as e:
            self.fail(f"true-up must be a safe no-op with no exchange accounts: {e!r}")
        self.assertFalse(self._trueups(),
                         "nothing should be booked without exchange accounts")

    def test_ac5_stamped_fx_offsets_gap(self):
        # QBO loss 100, Odoo already booked 40 via a stamped exchange move ->
        # only the 60 shortfall is trued up.
        self._seed_cache(910005, "2024-06-10", fx_debit=100.0)
        self._post_stamped("QBO_EXCH:Payment:910005", 40.0, "2024-06-10")

        self.finalizer._book_payment_fx_trueup(_ctx(self.env))

        tu = self._trueups()
        self.assertEqual(len(tu), 1)
        exch_leg = tu.line_ids.filtered(lambda l: l.account_id == self.exch_acct)
        self.assertAlmostEqual(exch_leg.debit, 60.0, 2,
                               msg="only the shortfall beyond stamped FX books")

    def test_ac7_cent_parity_books_balanced_remainder(self):
        # A cache txn whose Odoo counterpart is off by balanced cents gets
        # ONE parity entry with a leg per account, dated at the txn.
        self._seed_cache(910007, "2024-08-05", fx_debit=0.03)
        # Odoo booked nothing for this txn: gaps = exch +0.03 / AR -0.03.
        self.finalizer._book_gl_cent_trueup(_ctx(self.env))

        parity = self.env["account.move"].search(
            [("ref", "=", "QBO_CENT_TRUEUP")]
        )
        self.assertEqual(len(parity), 1, "one balanced parity entry expected")
        self.assertEqual(str(parity.date), "2024-08-05")
        exch_leg = parity.line_ids.filtered(
            lambda l: l.account_id == self.exch_acct
        )
        ar_leg = parity.line_ids.filtered(
            lambda l: l.account_id == self.ar_acct
        )
        self.assertAlmostEqual(exch_leg.debit, 0.03, 2)
        self.assertAlmostEqual(ar_leg.credit, 0.03, 2)
        # Idempotent.
        self.finalizer._book_gl_cent_trueup(_ctx(self.env))
        self.assertEqual(
            self.env["account.move"].search_count(
                [("ref", "=", "QBO_CENT_TRUEUP")]
            ),
            1,
            "second run must book nothing",
        )

    def test_ac8_cent_parity_skips_non_rounding_gaps(self):
        # A gap over 1.00 is not rounding — the txn must be skipped.
        self._seed_cache(910008, "2024-09-01", fx_debit=25.0)
        self.finalizer._book_gl_cent_trueup(_ctx(self.env))
        self.assertFalse(
            self.env["account.move"].search(
                [("ref", "=", "QBO_CENT_TRUEUP")]
            ),
            "gaps over the rounding threshold must not be parity-booked",
        )

    def test_ac6_retry_fx_reversed(self):
        # A heuristic-retry exchange move has no driving QBO txn: its FX is
        # reversed, and the cache txn's FX books in full per-txn instead.
        self._seed_cache(910006, "2024-07-01", fx_debit=30.0)
        retry = self._post_stamped("QBO_EXCH:RETRY", 12.0, "2024-07-01")

        self.finalizer._book_payment_fx_trueup(_ctx(self.env))

        reversal = self.env["account.move"].search(
            [("ref", "=", "QBO_FX_RETRY_REVERSAL")]
        )
        self.assertEqual(len(reversal), 1, "retry FX must be reversed")
        self.assertEqual(reversal.state, "posted")
        rev_exch = reversal.line_ids.filtered(
            lambda l: l.account_id == self.exch_acct
        )
        self.assertAlmostEqual(rev_exch.credit, 12.0, 2,
                               msg="reversal mirrors the retry FX")
        self.assertEqual(str(reversal.date), str(retry.date),
                         "reversal keeps the retry move's date")
        tu = self._trueups()
        exch_leg = tu.line_ids.filtered(lambda l: l.account_id == self.exch_acct)
        self.assertAlmostEqual(sum(exch_leg.mapped("debit")), 30.0, 2,
                               msg="QBO's full per-txn FX books after reversal")
