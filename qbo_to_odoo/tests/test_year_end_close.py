"""Tests for the qbo.year.end.close finalizer pipeline (task 4096).

Posts one balanced closing entry per already-closed fiscal year, sweeping
that year's net P&L result from the company's Current-Year-Earnings (CYE)
account into the configured Retained Earnings (RE) account, so Odoo's
report-only "Undistributed Profits/Losses" line reads 0.00 for every
closed fiscal year.

Acceptance criteria covered:
1. ``build_year_end_close_move_vals`` is a pure, balanced, correctly
   signed and correctly stamped move-vals builder (profit year and loss
   year).
2. ``compute_fiscal_year_close_rows`` derives its FY boundaries from
   ``company.compute_fiscalyear_dates`` (never hardcoded) — changing
   ``fiscalyear_last_month``/``_day`` shifts both the boundaries and the
   per-year results for the same underlying ledger data.
3. The earliest posted fiscal year needs no stub special-casing: its
   result naturally equals only the partial-period activity actually
   posted inside it.
4. AC #2: the load pass is idempotent — running it twice posts exactly
   one move per closed FY and leaves the CYE account active (unarchived
   exactly once, not re-duplicated).
5. AC #5/#6/#7: closing entries are correct and balanced, the FY
   containing "today" is never closed, P&L account balances are
   unaffected by the close, and the QBO_YE_CLOSE-stamped moves are
   excluded from ``qbo.migration.report._get_odoo_trial_balance``.
6. The pass is config-gated: disabled (the default) posts nothing and
   never touches the archived CYE account.
7. RE-account resolution validation (this task's fix to the WIP): the
   load step fails loudly, rather than silently posting misdirected
   moves or silently no-op'ing, when the configured RE account doesn't
   resolve or isn't a carry-forward equity account.
"""

from datetime import date

from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext
from odoo.addons.qbo_to_odoo.models.pipelines.gl_helpers import (
    YEAR_END_CLOSE_ENABLED_PARAM,
    YEAR_END_CLOSE_RE_CODE_PARAM,
    YEAR_END_CLOSE_REF_PREFIX,
    build_year_end_close_move_vals,
    compute_fiscal_year_close_rows,
)


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


@tagged("post_install", "-at_install", "qbo_year_end_close")
class TestBuildYearEndCloseMoveVals(TransactionCase):
    """AC #1: the pure move-vals builder — no DB needed."""

    def test_profit_year_debits_cye_credits_re(self):
        vals = build_year_end_close_move_vals(
            2015, date(2015, 10, 31), -46393.56, cye_account_id=101,
            re_account_id=202, journal_id=303,
        )
        self.assertEqual(vals["ref"], "QBO_YE_CLOSE:2015")
        self.assertEqual(vals["date"], date(2015, 10, 31))
        self.assertEqual(vals["journal_id"], 303)

        lines = {l[2]["account_id"]: l[2] for l in vals["line_ids"]}
        self.assertEqual(lines[101]["debit"], 46393.56)
        self.assertEqual(lines[101]["credit"], 0.0)
        self.assertEqual(lines[202]["debit"], 0.0)
        self.assertEqual(lines[202]["credit"], 46393.56)

        total_debit = sum(l[2]["debit"] for l in vals["line_ids"])
        total_credit = sum(l[2]["credit"] for l in vals["line_ids"])
        self.assertEqual(total_debit, total_credit, "move must be balanced")

    def test_loss_year_reverses_legs(self):
        vals = build_year_end_close_move_vals(
            2016, date(2016, 10, 31), 166263.98, cye_account_id=101,
            re_account_id=202, journal_id=303,
        )
        self.assertEqual(vals["ref"], "QBO_YE_CLOSE:2016")

        lines = {l[2]["account_id"]: l[2] for l in vals["line_ids"]}
        self.assertEqual(lines[101]["debit"], 0.0)
        self.assertEqual(lines[101]["credit"], 166263.98)
        self.assertEqual(lines[202]["debit"], 166263.98)
        self.assertEqual(lines[202]["credit"], 0.0)

        total_debit = sum(l[2]["debit"] for l in vals["line_ids"])
        total_credit = sum(l[2]["credit"] for l in vals["line_ids"])
        self.assertEqual(total_debit, total_credit, "move must be balanced")


@tagged("post_install", "-at_install", "qbo_year_end_close")
class TestYearEndClosePipeline(TransactionCase):
    """AC #2-#7: the DB-backed row computation and the ETL pipeline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        Account = cls.env["account.account"]

        # This test company has no installed chart of accounts. Odoo lazily
        # auto-provisions two chartless-fallback placeholder accounts
        # (xml_ids account.<company>_profit_or_loss /
        # account.<company>_retained_earnings) the first time accounting
        # config is touched — and in this Odoo version BOTH default to
        # account_type='equity_unaffected'. That is exactly the
        # "duplicate CYE account" scenario the pipeline's
        # get_unaffected_earnings_account() is designed to refuse (see
        # gl_helpers.py) — appropriate for a company mid-migration, but it
        # would make every fixture in this file trip that guard by
        # accident. Neutralize them so this test's own dedicated CYE/RE
        # accounts are the only equity_unaffected candidate, matching the
        # real target (a company with a genuine, already-migrated CoA
        # where a stray duplicate is the true exceptional case under
        # test).
        stray_cye = Account.with_context(active_test=False).search([
            ("account_type", "=", "equity_unaffected"),
            *Account._check_company_domain(cls.company),
        ])
        if stray_cye:
            stray_cye.write({"account_type": "asset_current"})

        cls.cye_account = Account.create({
            "name": "Current Year Earnings (YEC Test)",
            "code": "YEC.CYE",
            "account_type": "equity_unaffected",
        })
        cls.re_account = Account.create({
            "name": "Retained Earnings (YEC Test)",
            "code": "YEC.RE",
            "account_type": "equity",
        })
        cls.income_account = Account.create({
            "name": "Sales (YEC Test)", "code": "YEC.INC", "account_type": "income",
        })
        cls.expense_account = Account.create({
            "name": "Expenses (YEC Test)", "code": "YEC.EXP", "account_type": "expense",
        })
        cls.bank_account = Account.create({
            "name": "Bank (YEC Test)", "code": "YEC.BANK", "account_type": "asset_current",
        })

        cls.journal = cls.env["account.journal"].search([
            ("type", "=", "general"), ("company_id", "=", cls.company.id),
        ], limit=1)
        if not cls.journal:
            cls.journal = cls.env["account.journal"].create({
                "name": "Miscellaneous (YEC Test)",
                "code": "MISC",
                "type": "general",
                "company_id": cls.company.id,
            })

    def _post_pnl_move(self, move_date, expense_debit, income_credit):
        """Post a 3-line balanced move: DR Bank (plug), DR Expense, CR Income.

        The Bank leg is a non-P&L plug so the P&L legs alone can net to a
        non-zero signed result while the whole move still balances.
        """
        bank_debit = round(income_credit - expense_debit, 2)
        lines = [
            Command.create({
                "account_id": self.expense_account.id,
                "name": "YEC test expense",
                "debit": expense_debit,
                "credit": 0.0,
            }),
            Command.create({
                "account_id": self.income_account.id,
                "name": "YEC test income",
                "debit": 0.0,
                "credit": income_credit,
            }),
        ]
        if bank_debit >= 0:
            lines.append(Command.create({
                "account_id": self.bank_account.id,
                "name": "YEC test plug",
                "debit": bank_debit,
                "credit": 0.0,
            }))
        else:
            lines.append(Command.create({
                "account_id": self.bank_account.id,
                "name": "YEC test plug",
                "debit": 0.0,
                "credit": -bank_debit,
            }))
        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": self.journal.id,
            "date": move_date,
            "line_ids": lines,
        })
        move.action_post()
        return move

    def _config(self, retained_earnings_code=None):
        return {"retained_earnings_code": retained_earnings_code or self.re_account.code}

    def _run_pipeline(self):
        model = self.env["qbo.year.end.close"]
        ctx = _make_ctx(self.env)
        extracted = {
            "extract_year_end_close_rows": model.extract_year_end_close_rows(ctx)
        }
        transformed = {
            "transform_year_end_close": model.transform_year_end_close(ctx, extracted)
        }
        model.load_year_end_close(ctx, transformed)
        return extracted, transformed

    # -- AC #2/#3: FY enumeration, boundaries, and the natural stub-year --

    def test_per_fy_results_and_stub_year(self):
        """Calendar-year default: two closed FYs, first one is a natural stub."""
        self._post_pnl_move(date(2020, 6, 15), expense_debit=300.0, income_credit=800.0)
        self._post_pnl_move(date(2021, 6, 15), expense_debit=200.0, income_credit=500.0)

        rows = compute_fiscal_year_close_rows(
            self.env, self.company, self._config()
        )
        by_label = {r[0]: r for r in rows}

        self.assertIn(2020, by_label)
        fy_label, date_to, signed_result, cye_id, re_id = by_label[2020]
        self.assertEqual(date_to, date(2020, 12, 31))
        self.assertEqual(signed_result, -500.0, "FY2020: 800 income - 300 expense = 500 profit")
        self.assertEqual(cye_id, self.cye_account.id)
        self.assertEqual(re_id, self.re_account.id)

        self.assertIn(2021, by_label)
        _, date_to_2021, signed_result_2021, _, _ = by_label[2021]
        self.assertEqual(date_to_2021, date(2021, 12, 31))
        self.assertEqual(signed_result_2021, -300.0, "FY2021: 500 income - 200 expense = 300 profit")

    def test_boundary_shift_not_hardcoded(self):
        """Changing fiscalyear_last_month/_day shifts the FY windows for the same ledger."""
        self._post_pnl_move(date(2020, 6, 15), expense_debit=300.0, income_credit=800.0)
        self._post_pnl_move(date(2021, 6, 15), expense_debit=200.0, income_credit=500.0)

        self.company.write({"fiscalyear_last_month": "6", "fiscalyear_last_day": 30})

        rows = compute_fiscal_year_close_rows(
            self.env, self.company, self._config()
        )
        by_label = {r[0]: r for r in rows}

        self.assertIn(2020, by_label)
        self.assertEqual(
            by_label[2020][1], date(2020, 6, 30),
            "FY-end must follow the new fiscalyear_last_month/_day config",
        )
        self.assertEqual(by_label[2020][2], -500.0)

        self.assertIn(2021, by_label)
        self.assertEqual(by_label[2021][1], date(2021, 6, 30))
        self.assertEqual(by_label[2021][2], -300.0)

    def test_zero_result_fy_is_skipped(self):
        """A closed FY whose income/expense lines net to exactly zero needs no close."""
        self._post_pnl_move(date(2020, 6, 15), expense_debit=500.0, income_credit=500.0)

        rows = compute_fiscal_year_close_rows(
            self.env, self.company, self._config()
        )
        self.assertFalse(
            [r for r in rows if r[0] == 2020],
            "a zero-result FY must be omitted, not produce a zero-amount close",
        )

    # -- AC #4/#5/#6/#7: the full pipeline --

    def test_idempotent_double_run(self):
        self._post_pnl_move(date(2020, 6, 15), expense_debit=300.0, income_credit=800.0)
        self._post_pnl_move(date(2021, 6, 15), expense_debit=200.0, income_credit=500.0)
        self.cye_account.write({"active": False})
        self.env["ir.config_parameter"].sudo().set_param(YEAR_END_CLOSE_ENABLED_PARAM, "1")
        self.env["ir.config_parameter"].sudo().set_param(
            YEAR_END_CLOSE_RE_CODE_PARAM, self.re_account.code
        )

        self._run_pipeline()
        AccountMove = self.env["account.move"]
        first_run_ids = AccountMove.search(
            [("ref", "like", f"{YEAR_END_CLOSE_REF_PREFIX}%")]
        ).ids
        self.assertEqual(len(first_run_ids), 2)
        self.assertTrue(self.cye_account.active, "CYE account must be unarchived")

        self._run_pipeline()
        second_run_ids = AccountMove.search(
            [("ref", "like", f"{YEAR_END_CLOSE_REF_PREFIX}%")]
        ).ids
        self.assertEqual(
            sorted(second_run_ids), sorted(first_run_ids),
            "re-running must not create duplicate closing moves",
        )
        self.assertTrue(self.cye_account.active)

    def test_close_correctness_open_fy_and_report_exclusion(self):
        self._post_pnl_move(date(2020, 6, 15), expense_debit=300.0, income_credit=800.0)
        self._post_pnl_move(date(2021, 6, 15), expense_debit=200.0, income_credit=500.0)
        # Activity in the currently-open FY must never be closed (AC #5).
        self._post_pnl_move(date.today(), expense_debit=0.0, income_credit=100.0)

        pnl_accounts = self.income_account | self.expense_account
        pnl_balance_before = sum(
            self.env["account.move.line"].search([
                ("account_id", "in", pnl_accounts.ids),
                ("parent_state", "=", "posted"),
            ]).mapped("balance")
        )

        self.env["ir.config_parameter"].sudo().set_param(YEAR_END_CLOSE_ENABLED_PARAM, "1")
        self.env["ir.config_parameter"].sudo().set_param(
            YEAR_END_CLOSE_RE_CODE_PARAM, self.re_account.code
        )
        self._run_pipeline()

        AccountMove = self.env["account.move"]
        closes = AccountMove.search([("ref", "like", f"{YEAR_END_CLOSE_REF_PREFIX}%")])
        self.assertEqual(
            len(closes), 2,
            "only the two closed FYs are stamped — the open FY is never closed",
        )
        for move in closes:
            self.assertEqual(move.state, "posted")
            self.assertEqual(move.journal_id, self.journal)
            self.assertEqual(
                sum(move.line_ids.mapped("debit")),
                sum(move.line_ids.mapped("credit")),
            )

        # Both FYs were profit years: RE nets credited, CYE nets debited by
        # the same accumulated amount (500 + 300 = 800).
        AccountMoveLine = self.env["account.move.line"]
        re_balance = sum(
            AccountMoveLine.search([("account_id", "=", self.re_account.id)])
            .mapped("balance")
        )
        cye_balance = sum(
            AccountMoveLine.search([("account_id", "=", self.cye_account.id)])
            .mapped("balance")
        )
        self.assertAlmostEqual(re_balance, -800.0, places=2)
        self.assertAlmostEqual(cye_balance, 800.0, places=2)

        # AC #6: P&L account balances are unaffected by the close.
        pnl_balance_after = sum(
            self.env["account.move.line"].search([
                ("account_id", "in", pnl_accounts.ids),
                ("parent_state", "=", "posted"),
            ]).mapped("balance")
        )
        self.assertAlmostEqual(pnl_balance_before, pnl_balance_after, places=2)

        # AC #7 (in-repo piece): the report excludes QBO_YE_CLOSE moves, so
        # the RE/CYE test accounts — touched only by the closing entries —
        # show no activity in the report's trial balance.
        report_model = self.env["qbo.migration.report"]
        tb, _txns = report_model._get_odoo_trial_balance()
        self.assertNotIn(self.re_account.code, tb)
        self.assertNotIn(self.cye_account.code, tb)

    def test_config_gate_disabled_posts_nothing(self):
        self._post_pnl_move(date(2020, 6, 15), expense_debit=300.0, income_credit=800.0)
        self.cye_account.write({"active": False})
        self.env["ir.config_parameter"].sudo().set_param(YEAR_END_CLOSE_ENABLED_PARAM, "0")

        self._run_pipeline()

        self.assertEqual(
            self.env["account.move"].search_count(
                [("ref", "like", f"{YEAR_END_CLOSE_REF_PREFIX}%")]
            ),
            0,
        )
        self.assertFalse(
            self.cye_account.active,
            "a disabled pass must never touch the archived CYE account",
        )

    # -- Fail-loud validation (this task's fix) --

    def test_re_account_wrong_type_raises(self):
        """RE code pointing at a non-equity account must abort loudly, not post."""
        self._post_pnl_move(date(2020, 6, 15), expense_debit=300.0, income_credit=800.0)
        self.env["ir.config_parameter"].sudo().set_param(YEAR_END_CLOSE_ENABLED_PARAM, "1")
        self.env["ir.config_parameter"].sudo().set_param(
            YEAR_END_CLOSE_RE_CODE_PARAM, self.income_account.code
        )

        with self.assertRaises(UserError):
            self._run_pipeline()

        self.assertEqual(
            self.env["account.move"].search_count(
                [("ref", "like", f"{YEAR_END_CLOSE_REF_PREFIX}%")]
            ),
            0,
            "no partial/misdirected close may be posted when validation fails",
        )

    def test_re_account_missing_raises(self):
        self._post_pnl_move(date(2020, 6, 15), expense_debit=300.0, income_credit=800.0)
        self.env["ir.config_parameter"].sudo().set_param(YEAR_END_CLOSE_ENABLED_PARAM, "1")
        self.env["ir.config_parameter"].sudo().set_param(
            YEAR_END_CLOSE_RE_CODE_PARAM, "YEC.NONEXISTENT"
        )

        with self.assertRaises(UserError):
            self._run_pipeline()
