#
#    Bemade Inc.
#
#    Copyright (C) 2026-April Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for year-end closing-JE handling in the JDT1 GL pipeline.

Current design (``_classify_closing_je`` → "normal" | "skip" | "transfer"):

* A JE is a *closing* JE when ``transtype == '-3'`` OR its memo matches the
  closing-period regex.
* Among closing JEs, one carrying a **P&L leg** (any line whose SAP
  ``OACT.groupmask`` is in {4, 5, 6} with a non-zero amount) is a P&L close →
  **"skip"** (not imported: Odoo's balance sheet auto-accumulates P&L into
  ``equity_unaffected`` / 999999 via cross-report, so importing would
  double-count).
* A closing JE with **no P&L leg** is an RE Clearing → Year-N RE transfer →
  **"transfer"**: the leg whose SAP account *name* matches "clearing" is
  rewritten to the Odoo ``equity_unaffected`` account (code 999999); the Year-N
  RE leg passes through untouched.
* Everything else is **"normal"** — no transformation.

Classification keys on SAP ``groupmask`` (not ``acttype``): RWI's chart carries
COGS accounts at groupmask=5 with acttype='N', so acttype is unreliable.

Acceptance criteria:

1. (test_classify_normal) Non-closing JE → "normal".
2. (test_classify_skip_pl_close) transtype='-3' with a P&L leg → "skip".
3. (test_classify_transfer_re_clearing) transtype='-3' with no P&L leg →
   "transfer".
4. (test_classify_memo_arm_skip / _transfer) A closing-period memo (transtype
   != '-3') is detected; P&L leg → "skip", no P&L leg → "transfer".
5. (test_classify_zero_amount_pl_leg_ignored) A zero-amount P&L line does NOT
   make a JE a P&L close; it classifies "transfer".
6. (test_transfer_rewrites_clearing_leg_to_999999) On a transfer JE the
   "clearing"-named leg is rewritten to 999999; the Year-N RE leg keeps its
   mapped account; the move stays balanced.
7. (test_normal_je_not_rewritten) A normal JE's accounts are untouched.
8. (test_transfer_preserves_currency) FX fields survive the clearing rewrite.
9. (test_idempotency_skips_already_imported) Already-imported OJDT docentries
   are detected by _get_already_imported.
10. (test_999999_missing_raises_user_error) A missing 999999 account makes
    _build_lookups raise UserError.
11. (test_diagnostic_emits_per_closing_year) transform_journal_entries emits one
    per-year closing diagnostic (source_ref="closing-<year>").
"""

import contextlib
from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools.misc import mute_logger

from odoo.addons.sap_b1_to_odoo.models.pipelines.account_move_jdt1_etl import (
    AccountMoveJDT1Importer,
)

# SAP OACT.groupmask categories used by the classifier.
GM_REVENUE = 4  # P&L
GM_EXPENSE = 6  # P&L
GM_EQUITY = 3  # balance sheet (Capital/Equity)


@tagged("-at_install", "post_install", "sap_jdt1_yearend")
class TestAccountMoveJDT1YearendRedirect(TransactionCase):
    """Guards year-end closing-JE classification and the RE-clearing rewrite."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Account = cls.env["account.account"]
        cls.importer = cls.env["account.move.jdt1.importer"]

        # Source-side accounts (with sap_acct_code so accounts_dict resolves).
        cls.income_account = cls._ensure_account(
            "TESTINC", "Test Income", "income", sap_acct_code="4000-INC",
        )
        cls.expense_account = cls._ensure_account(
            "TESTEXP", "Test Expense", "expense", sap_acct_code="5000-EXP",
        )
        # RE Clearing transit account (name contains "clearing" → rewritten).
        cls.clearing_account = cls._ensure_account(
            "TESTCLR", "Retained Earnings Clearing", "equity",
            sap_acct_code="3999-CLR",
        )
        # Year-N RE destination account (name has no "clearing" → passthrough).
        cls.re_year_account = cls._ensure_account(
            "TESTRE24", "2024 Retained Earnings", "equity",
            sap_acct_code="3001-RE24",
        )

        # Unallocated Earnings — Odoo allows ONE equity_unaffected.
        existing = cls.Account.with_context(active_test=False).search(
            [("code", "=", "999999")], limit=1,
        )
        if existing:
            cls.unallocated = existing
            if not cls.unallocated.active:
                cls.unallocated.active = True
            if cls.unallocated.account_type != "equity_unaffected":
                cls.unallocated.account_type = "equity_unaffected"
        else:
            existing_unalloc = cls.Account.search(
                [("account_type", "=", "equity_unaffected")], limit=1,
            )
            if existing_unalloc:
                existing_unalloc.code = "999999"
                cls.unallocated = existing_unalloc
            else:
                cls.unallocated = cls.Account.create({
                    "name": "Unallocated Earnings",
                    "code": "999999",
                    "account_type": "equity_unaffected",
                })

        cls.accounts_dict = {
            "4000-INC": (cls.income_account.id, "income"),
            "5000-EXP": (cls.expense_account.id, "expense"),
            "3999-CLR": (cls.clearing_account.id, "equity"),
            "3001-RE24": (cls.re_year_account.id, "equity"),
        }
        cls.partners_dict = {}
        cls.currencies_dict = {
            c.name: c.id
            for c in cls.env["res.currency"].with_context(
                active_test=False,
            ).search([])
        }
        cls.company_currency_id = cls.env.company.currency_id.id

        cls.misc_journal = cls.env["account.journal"].search(
            [("type", "=", "general")], limit=1,
        )
        if not cls.misc_journal:
            cls.misc_journal = cls.env["account.journal"].create({
                "name": "Misc", "code": "MISC", "type": "general",
            })

    @classmethod
    def _ensure_account(cls, code, name, account_type, sap_acct_code=None):
        acc = cls.Account.with_context(active_test=False).search(
            [("code", "=", code)], limit=1,
        )
        if not acc:
            vals = {"code": code, "name": name, "account_type": account_type}
            if sap_acct_code:
                vals["sap_acct_code"] = sap_acct_code
            acc = cls.Account.create(vals)
        elif sap_acct_code and not acc.sap_acct_code:
            acc.sap_acct_code = sap_acct_code
        return acc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_header(self, transtype="-3", transid=11111, docnum=22222,
                     memo="Manual JE", refdate=None):
        return {
            "transid": transid,
            "transtype": transtype,
            # psycopg2 returns datetime for SQL DATE columns; pass a datetime.
            "refdate": refdate if refdate is not None else datetime(2024, 12, 31),
            "memo": memo,
            "createdby": 0,
            "docnum": docnum,
            "_lines": [],
        }

    def _make_jdt1_row(self, account_code, debit, credit, acct_groupmask=None,
                       line_id=0, fccurrency=None, fcdebit=0.0, fccredit=0.0,
                       acct_name=None, acttype="N"):
        return {
            "transid": 11111,
            "line_id": line_id,
            "account": account_code,
            "debit": debit,
            "credit": credit,
            "shortname": "",
            "fccurrency": fccurrency or "",
            "fcdebit": fcdebit,
            "fccredit": fccredit,
            "ref1": "",
            "ref2": "",
            "project": "",
            "acct_formatcode": account_code,
            # Classification keys on groupmask; acttype is kept for realism only.
            "acct_groupmask": acct_groupmask,
            "acttype": acttype,
            "acct_name": acct_name or "",
        }

    def _classify(self, header, jdt1_lines):
        return AccountMoveJDT1Importer._classify_closing_je(header, jdt1_lines)

    def _build_move_vals(self, header, jdt1_lines, unallocated_id=None):
        # Production sets header["_closing_class"] in the transform loop before
        # calling _build_generic_entry_vals; mirror that here.
        if "_closing_class" not in header:
            header["_closing_class"] = self._classify(header, jdt1_lines)
        return AccountMoveJDT1Importer._build_generic_entry_vals(
            header, jdt1_lines, self.accounts_dict, self.partners_dict,
            self.currencies_dict, self.company_currency_id,
            self.misc_journal.id,
            unallocated_earnings_id=(
                unallocated_id if unallocated_id is not None
                else self.unallocated.id
            ),
        )

    def _line_vals(self, move_vals):
        return [
            cmd[2] for cmd in move_vals["line_ids"]
            if isinstance(cmd, (list, tuple)) and cmd[0] == 0
        ]

    # ------------------------------------------------------------------
    # Classification (pure — no DB)
    # ------------------------------------------------------------------

    def test_classify_normal(self):
        """Non-closing JE (transtype != -3, non-closing memo) → 'normal'."""
        header = self._make_header(transtype="30", memo="Manual JE")
        lines = [
            self._make_jdt1_row("4000-INC", 100.0, 0.0, GM_REVENUE),
            self._make_jdt1_row("3999-CLR", 0.0, 100.0, GM_EQUITY),
        ]
        self.assertEqual(self._classify(header, lines), "normal")

    def test_classify_skip_pl_close(self):
        """Closing JE (transtype -3) carrying a P&L leg → 'skip'."""
        header = self._make_header(transtype="-3")
        lines = [
            self._make_jdt1_row("4000-INC", 1000.0, 0.0, GM_REVENUE),
            self._make_jdt1_row("3999-CLR", 0.0, 1000.0, GM_EQUITY),
        ]
        self.assertEqual(self._classify(header, lines), "skip")

    def test_classify_transfer_re_clearing(self):
        """Closing JE with no P&L leg (RE-clearing transfer) → 'transfer'."""
        header = self._make_header(transtype="-3")
        lines = [
            self._make_jdt1_row("3999-CLR", 0.0, 1000.0, GM_EQUITY,
                                acct_name="Retained Earnings Clearing"),
            self._make_jdt1_row("3001-RE24", 1000.0, 0.0, GM_EQUITY,
                                acct_name="2024 Retained Earnings"),
        ]
        self.assertEqual(self._classify(header, lines), "transfer")

    def test_classify_memo_arm_skip(self):
        """Memo arm: transtype != -3 + 'For Closing Period' + P&L leg → 'skip'."""
        header = self._make_header(transtype="30", memo="For Closing Period 2024")
        lines = [
            self._make_jdt1_row("5000-EXP", 0.0, 750.0, GM_EXPENSE),
            self._make_jdt1_row("3999-CLR", 750.0, 0.0, GM_EQUITY),
        ]
        self.assertEqual(self._classify(header, lines), "skip")

    def test_classify_memo_arm_transfer(self):
        """Memo arm closing JE with no P&L leg → 'transfer'."""
        header = self._make_header(transtype="30", memo="Closing Period 2024")
        lines = [
            self._make_jdt1_row("3999-CLR", 0.0, 500.0, GM_EQUITY,
                                acct_name="Retained Earnings Clearing"),
            self._make_jdt1_row("3001-RE24", 500.0, 0.0, GM_EQUITY,
                                acct_name="2024 Retained Earnings"),
        ]
        self.assertEqual(self._classify(header, lines), "transfer")

    def test_classify_zero_amount_pl_leg_ignored(self):
        """A zero-amount P&L line does not make a JE a P&L close → 'transfer'."""
        header = self._make_header(transtype="-3")
        lines = [
            # P&L account but zero amount — must be ignored by has_pl.
            self._make_jdt1_row("4000-INC", 0.0, 0.0, GM_REVENUE),
            self._make_jdt1_row("3999-CLR", 0.0, 1000.0, GM_EQUITY,
                                acct_name="Retained Earnings Clearing"),
            self._make_jdt1_row("3001-RE24", 1000.0, 0.0, GM_EQUITY,
                                acct_name="2024 Retained Earnings"),
        ]
        self.assertEqual(self._classify(header, lines), "transfer")

    # ------------------------------------------------------------------
    # Transfer rewrite (RE Clearing leg → 999999)
    # ------------------------------------------------------------------

    def test_transfer_rewrites_clearing_leg_to_999999(self):
        header = self._make_header(transtype="-3")
        lines = [
            self._make_jdt1_row("3999-CLR", 0.0, 1000.0, GM_EQUITY,
                                line_id=0,
                                acct_name="Retained Earnings Clearing"),
            self._make_jdt1_row("3001-RE24", 1000.0, 0.0, GM_EQUITY,
                                line_id=1,
                                acct_name="2024 Retained Earnings"),
        ]
        move_vals = self._build_move_vals(header, lines)
        self.assertIsNotNone(move_vals)
        line_vals = self._line_vals(move_vals)
        self.assertEqual(len(line_vals), 2)

        clearing_line = next(l for l in line_vals if l["credit"] == 1000.0)
        re_line = next(l for l in line_vals if l["debit"] == 1000.0)
        self.assertEqual(
            clearing_line["account_id"], self.unallocated.id,
            "The 'clearing'-named leg of a transfer JE must be rewritten to 999999.",
        )
        self.assertEqual(
            re_line["account_id"], self.re_year_account.id,
            "The Year-N RE leg must keep its mapped account.",
        )
        total_debit = sum(l.get("debit", 0) for l in line_vals)
        total_credit = sum(l.get("credit", 0) for l in line_vals)
        self.assertEqual(round(total_debit, 2), round(total_credit, 2),
                         "Transfer JE must stay balanced after the rewrite.")

    def test_normal_je_not_rewritten(self):
        """A 'normal' JE keeps every mapped account (no clearing rewrite)."""
        header = self._make_header(transtype="30", memo="Manual JE")
        lines = [
            self._make_jdt1_row("4000-INC", 200.0, 0.0, GM_REVENUE, line_id=0),
            self._make_jdt1_row("3999-CLR", 0.0, 200.0, GM_EQUITY, line_id=1,
                                acct_name="Retained Earnings Clearing"),
        ]
        move_vals = self._build_move_vals(header, lines)
        account_ids = [l["account_id"] for l in self._line_vals(move_vals)]
        self.assertIn(self.income_account.id, account_ids)
        self.assertIn(self.clearing_account.id, account_ids)
        self.assertNotIn(
            self.unallocated.id, account_ids,
            "A normal JE must not rewrite any leg to 999999.",
        )

    def test_transfer_preserves_currency(self):
        """FX fields survive the clearing → 999999 rewrite."""
        other = self.env["res.currency"].with_context(active_test=False).search(
            [("id", "!=", self.company_currency_id)], limit=1,
        )
        if not other:
            self.skipTest("No second currency available for FX test.")
        if not other.active:
            other.active = True
        self.currencies_dict[other.name] = other.id

        header = self._make_header(transtype="-3")
        lines = [
            self._make_jdt1_row("3999-CLR", 0.0, 1000.0, GM_EQUITY, line_id=0,
                                acct_name="Retained Earnings Clearing",
                                fccurrency=other.name, fcdebit=0.0, fccredit=800.0),
            self._make_jdt1_row("3001-RE24", 1000.0, 0.0, GM_EQUITY, line_id=1,
                                acct_name="2024 Retained Earnings",
                                fccurrency=other.name, fcdebit=800.0, fccredit=0.0),
        ]
        move_vals = self._build_move_vals(header, lines)
        clearing_line = next(
            l for l in self._line_vals(move_vals) if l["credit"] == 1000.0
        )
        self.assertEqual(clearing_line["account_id"], self.unallocated.id)
        self.assertEqual(clearing_line.get("currency_id"), other.id,
                         "Currency must be preserved across the rewrite.")
        self.assertEqual(clearing_line.get("amount_currency"), -800.0,
                         "amount_currency (credit → negative) must be preserved.")

    # ------------------------------------------------------------------
    # Idempotency (unrelated to closing classification)
    # ------------------------------------------------------------------

    def test_idempotency_skips_already_imported(self):
        seeded = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": self.misc_journal.id,
            "ref": "Pre-imported closing",
            "sap_table": "ojdt",
            "sap_docentry": 99999,
            "line_ids": [
                (0, 0, {"account_id": self.unallocated.id,
                        "debit": 100.0, "credit": 0.0, "name": "x"}),
                (0, 0, {"account_id": self.clearing_account.id,
                        "debit": 0.0, "credit": 100.0, "name": "y"}),
            ],
        })
        self.assertTrue(seeded.exists())

        ctx = MagicMock()
        ctx.env = self.env
        ctx.cr = self.env.cr
        already = AccountMoveJDT1Importer._get_already_imported(ctx)
        self.assertIn(
            99999, already,
            "Already-imported OJDT docentry must be detected for idempotency.",
        )

    @mute_logger("odoo.sql_db", "odoo.addons.base.models.ir_model")
    def test_999999_missing_raises_user_error(self):
        original_code = self.unallocated.code
        try:
            self.unallocated.code = "ZZZ999999TEST"
            with self.assertRaises(UserError) as cm:
                self.importer._build_lookups()
            self.assertIn("999999", str(cm.exception))
        finally:
            self.unallocated.code = original_code

    # ------------------------------------------------------------------
    # Per-year diagnostic
    # ------------------------------------------------------------------

    def test_diagnostic_emits_per_closing_year(self):
        """transform_journal_entries emits one closing diagnostic per year."""
        def _closing_header(year, transid):
            return {
                "transid": transid,
                "transtype": "-3",
                "refdate": datetime(year, 12, 31),
                "memo": "Manual JE",
                "createdby": 0,
                "docnum": transid,
                # No P&L leg → classified "transfer".
                "_lines": [
                    self._make_jdt1_row("3999-CLR", 0.0, 100.0, GM_EQUITY,
                                        line_id=0,
                                        acct_name="Retained Earnings Clearing"),
                    self._make_jdt1_row("3001-RE24", 100.0, 0.0, GM_EQUITY,
                                        line_id=1,
                                        acct_name="2024 Retained Earnings"),
                ],
            }

        headers = [
            _closing_header(2023, 77701),
            _closing_header(2023, 77702),
            _closing_header(2024, 77703),
        ]

        inner_lookups = {
            "accounts": self.accounts_dict,
            "currencies": self.currencies_dict,
            "company_currency_id": self.company_currency_id,
            "unallocated_earnings_id": self.unallocated.id,
        }
        extracted = {
            "extract_journal_entries": {"records": headers, "context": {}},
            "extract_lookups": {
                "partners": self.partners_dict,
                "lookups": inner_lookups,
                "misc_journal_id": self.misc_journal.id,
                "tax_account_ids": set(),
                "order_lines_dict": {},
            },
        }

        mock_report = MagicMock()

        @contextlib.contextmanager
        def _pass_through_skippable(_ref):
            yield

        ctx = MagicMock()
        ctx.env = self.env
        ctx.cr = self.env.cr
        ctx.report = mock_report
        ctx.skippable = _pass_through_skippable

        def _stub_generic(header, jdt1_lines, *args, **kwargs):
            return {
                "move_type": "entry", "date": False, "ref": "stub",
                "journal_id": self.misc_journal.id, "line_ids": [],
                "sap_docentry": header.get("transid", 0),
                "sap_docnum": header.get("docnum", 0), "sap_table": "ojdt",
            }

        with patch.object(
            AccountMoveJDT1Importer, "_build_generic_entry_vals",
            side_effect=_stub_generic,
        ):
            self.importer.transform_journal_entries(ctx, extracted)

        source_refs = [
            c.kwargs.get("source_ref") for c in mock_report.warning.call_args_list
        ]
        self.assertIn("closing-2023", source_refs)
        self.assertIn("closing-2024", source_refs)
        # 2023 has two transfer JEs, 2024 has one.
        for call in mock_report.warning.call_args_list:
            msg = call.kwargs.get("message", "")
            if "2023" in msg:
                self.assertIn("transfers rewritten=2", msg)
            elif "2024" in msg:
                self.assertIn("transfers rewritten=1", msg)
