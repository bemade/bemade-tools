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
"""Tests for closing-JE redirection to Unallocated Earnings (code 999999).

Acceptance criteria (task 3476):

1. (test_closing_pl_debit_leg_redirected) Given a -3 JDT1 row pair with the
   debit on an OACT row whose acttype='I' and credit on the N-type
   "Retained Earnings Clearing", the produced Odoo move has the income line's
   account_id == unallocated_earnings_id (999999), the clearing line is
   unchanged, and the debit/credit amounts are preserved exactly.

2. (test_closing_pl_credit_leg_redirected) Mirror of test 1 with acttype='E'
   on the credit side; redirection works regardless of debit/credit side.

3. (test_balance_sheet_leg_untouched) Lines on a -3 move whose acttype='N'
   keep their original mapped account_id.

4. (test_non_closing_je_untouched) A JE with transtype='30' hitting an
   income account is NOT redirected; the income account is preserved.

5. (test_resulting_move_balanced) Sum of debits == sum of credits on the
   produced move_vals (no rebalance needed).

6. (test_idempotency_skips_already_imported) Running extract a second time
   with the move already in account_move (sap_table='ojdt', sap_docentry=...)
   skips it via _get_already_imported.

7. (test_999999_missing_raises_user_error) With the 999999 account
   archived/deleted, _build_lookups() raises UserError.

8. (test_currency_bearing_closing_line) A -3 JE with fccurrency set still
   produces correct currency_id / amount_currency after account redirection.
"""

from unittest.mock import MagicMock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools.misc import mute_logger


@tagged("-at_install", "post_install", "sap_jdt1_yearend")
class TestAccountMoveJDT1YearendRedirect(TransactionCase):
    """Guards the closing-JE P&L redirection in the JDT1 GL pipeline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Account = cls.env["account.account"]
        cls.importer = cls.env["account.move.jdt1.importer"]

        # Source-side P&L and clearing accounts (with sap_acct_code so the
        # accounts_dict lookup resolves). Use the format SAP exposes via
        # OACT.formatcode for the JDT1 SELECT.
        cls.income_account = cls._ensure_account(
            "TEST_INC", "Test Income", "income",
            sap_acct_code="4000-INC",
        )
        cls.expense_account = cls._ensure_account(
            "TEST_EXP", "Test Expense", "expense",
            sap_acct_code="5000-EXP",
        )
        cls.clearing_account = cls._ensure_account(
            "TEST_CLR", "Retained Earnings Clearing",
            "equity",
            sap_acct_code="3999-CLR",
        )

        # Unallocated Earnings — Odoo only allows ONE equity_unaffected.
        # Find or repurpose an existing one.
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

        # Build the lookups dict shape expected by _build_jdt1_line_vals
        # / _build_generic_entry_vals.
        cls.accounts_dict = {
            "4000-INC": (cls.income_account.id, "income"),
            "5000-EXP": (cls.expense_account.id, "expense"),
            "3999-CLR": (cls.clearing_account.id, "equity"),
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
                "name": "Misc",
                "code": "MISC",
                "type": "general",
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

    def _make_header(self, transtype="-3", transid=11111, docnum=22222):
        return {
            "transid": transid,
            "transtype": transtype,
            "refdate": "2024-12-31",
            "memo": "Period End Closing",
            "createdby": 0,
            "docnum": docnum,
            "_lines": [],
        }

    def _make_jdt1_row(
        self, account_code, acttype, debit, credit, line_id=0,
        fccurrency=None, fcdebit=0.0, fccredit=0.0,
    ):
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
            "acttype": acttype,
        }

    def _build_move_vals(
        self, header, jdt1_lines, unallocated_id=None,
    ):
        cls = type(self.importer)
        # The static method lives on the AccountMoveJDT1Importer concrete
        # class; route via the importer's class dict.
        from odoo.addons.sap_b1_to_odoo.models.pipelines.\
            account_move_jdt1_etl import AccountMoveJDT1Importer
        return AccountMoveJDT1Importer._build_generic_entry_vals(
            header, jdt1_lines, self.accounts_dict, self.partners_dict,
            self.currencies_dict, self.company_currency_id,
            self.misc_journal.id,
            unallocated_earnings_id=(
                unallocated_id if unallocated_id is not None
                else self.unallocated.id
            ),
        )

    def _line_account_ids(self, move_vals):
        return [
            cmd[2]["account_id"]
            for cmd in move_vals["line_ids"]
            if isinstance(cmd, (list, tuple)) and cmd[0] == 0
        ]

    # ------------------------------------------------------------------
    # AC1: Closing P&L debit leg redirected
    # ------------------------------------------------------------------

    def test_closing_pl_debit_leg_redirected(self):
        header = self._make_header(transtype="-3")
        jdt1_lines = [
            # Income closed via debit (closing entry side)
            self._make_jdt1_row("4000-INC", "I", debit=1000.0, credit=0.0,
                                line_id=0),
            # Retained Earnings Clearing offset (credit)
            self._make_jdt1_row("3999-CLR", "N", debit=0.0, credit=1000.0,
                                line_id=1),
        ]
        move_vals = self._build_move_vals(header, jdt1_lines)
        self.assertIsNotNone(move_vals)

        line_vals = [
            cmd[2] for cmd in move_vals["line_ids"]
            if isinstance(cmd, (list, tuple)) and cmd[0] == 0
        ]
        self.assertEqual(len(line_vals), 2)

        income_line = next(
            l for l in line_vals if l["debit"] == 1000.0
        )
        clearing_line = next(
            l for l in line_vals if l["credit"] == 1000.0
        )
        self.assertEqual(
            income_line["account_id"], self.unallocated.id,
            "Closing P&L debit leg must be redirected to 999999.",
        )
        self.assertEqual(income_line["sap_acct_id"], self.unallocated.id)
        self.assertEqual(income_line["debit"], 1000.0)
        self.assertEqual(income_line["credit"], 0.0)
        self.assertEqual(
            clearing_line["account_id"], self.clearing_account.id,
            "Clearing leg (acttype N) must NOT be redirected.",
        )
        self.assertEqual(clearing_line["credit"], 1000.0)

    # ------------------------------------------------------------------
    # AC2: Closing P&L credit leg redirected
    # ------------------------------------------------------------------

    def test_closing_pl_credit_leg_redirected(self):
        header = self._make_header(transtype="-3")
        jdt1_lines = [
            # Expense closed via credit (closing entry side)
            self._make_jdt1_row("5000-EXP", "E", debit=0.0, credit=750.0,
                                line_id=0),
            # Retained Earnings Clearing offset (debit)
            self._make_jdt1_row("3999-CLR", "N", debit=750.0, credit=0.0,
                                line_id=1),
        ]
        move_vals = self._build_move_vals(header, jdt1_lines)
        line_vals = [
            cmd[2] for cmd in move_vals["line_ids"]
            if isinstance(cmd, (list, tuple)) and cmd[0] == 0
        ]
        expense_line = next(
            l for l in line_vals if l["credit"] == 750.0
        )
        self.assertEqual(
            expense_line["account_id"], self.unallocated.id,
            "Closing P&L credit leg must be redirected to 999999.",
        )
        self.assertEqual(expense_line["credit"], 750.0)
        self.assertEqual(expense_line["debit"], 0.0)

    # ------------------------------------------------------------------
    # AC3: Balance-sheet leg untouched
    # ------------------------------------------------------------------

    def test_balance_sheet_leg_untouched(self):
        header = self._make_header(transtype="-3")
        jdt1_lines = [
            self._make_jdt1_row("4000-INC", "I", debit=500.0, credit=0.0,
                                line_id=0),
            self._make_jdt1_row("3999-CLR", "N", debit=0.0, credit=500.0,
                                line_id=1),
        ]
        move_vals = self._build_move_vals(header, jdt1_lines)
        account_ids = self._line_account_ids(move_vals)
        self.assertIn(
            self.clearing_account.id, account_ids,
            "Balance-sheet (acttype N) line must keep its original "
            "account_id even on a closing JE.",
        )

    # ------------------------------------------------------------------
    # AC4: Non-closing JE untouched
    # ------------------------------------------------------------------

    def test_non_closing_je_untouched(self):
        header = self._make_header(transtype="30", transid=22222)
        jdt1_lines = [
            self._make_jdt1_row("4000-INC", "I", debit=200.0, credit=0.0,
                                line_id=0),
            self._make_jdt1_row("3999-CLR", "N", debit=0.0, credit=200.0,
                                line_id=1),
        ]
        move_vals = self._build_move_vals(header, jdt1_lines)
        account_ids = self._line_account_ids(move_vals)
        self.assertIn(
            self.income_account.id, account_ids,
            "Non-closing JE (transtype != -3) must NOT redirect P&L "
            "lines, even when acttype is I.",
        )
        self.assertNotIn(self.unallocated.id, account_ids)

    # ------------------------------------------------------------------
    # AC5: Resulting move is balanced
    # ------------------------------------------------------------------

    def test_resulting_move_balanced(self):
        header = self._make_header(transtype="-3")
        jdt1_lines = [
            self._make_jdt1_row("4000-INC", "I", debit=1234.56, credit=0.0,
                                line_id=0),
            self._make_jdt1_row("3999-CLR", "N", debit=0.0, credit=1234.56,
                                line_id=1),
        ]
        move_vals = self._build_move_vals(header, jdt1_lines)
        line_vals = [
            cmd[2] for cmd in move_vals["line_ids"]
            if isinstance(cmd, (list, tuple)) and cmd[0] == 0
        ]
        total_debit = sum(l.get("debit", 0) for l in line_vals)
        total_credit = sum(l.get("credit", 0) for l in line_vals)
        self.assertEqual(
            round(total_debit, 2), round(total_credit, 2),
            "Closing JE move_vals must be balanced after redirection.",
        )

    # ------------------------------------------------------------------
    # AC6: Idempotency
    # ------------------------------------------------------------------

    def test_idempotency_skips_already_imported(self):
        # Seed an account.move that the pipeline would consider already
        # imported (sap_table='ojdt', sap_docentry=99999).
        seeded = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": self.misc_journal.id,
            "ref": "Pre-imported closing",
            "sap_table": "ojdt",
            "sap_docentry": 99999,
            "line_ids": [
                (0, 0, {
                    "account_id": self.unallocated.id,
                    "debit": 100.0, "credit": 0.0, "name": "x",
                }),
                (0, 0, {
                    "account_id": self.clearing_account.id,
                    "debit": 0.0, "credit": 100.0, "name": "y",
                }),
            ],
        })
        self.assertTrue(seeded.exists())

        ctx = MagicMock()
        ctx.env = self.env
        ctx.cr = self.env.cr

        from odoo.addons.sap_b1_to_odoo.models.pipelines.\
            account_move_jdt1_etl import AccountMoveJDT1Importer
        already = AccountMoveJDT1Importer._get_already_imported(ctx)
        self.assertIn(
            99999, already,
            "Already-imported closing JE (sap_table='ojdt', "
            "sap_docentry=99999) must be detected by "
            "_get_already_imported, ensuring idempotency.",
        )

    # ------------------------------------------------------------------
    # AC7: 999999 missing raises UserError
    # ------------------------------------------------------------------

    @mute_logger("odoo.sql_db", "odoo.addons.base.models.ir_model")
    def test_999999_missing_raises_user_error(self):
        # Temporarily relabel the 999999 account so the lookup fails.
        # We can't delete it (it has links), but renaming the code is
        # enough to make the search miss.
        original_code = self.unallocated.code
        try:
            # Need to swap to a code that doesn't collide.
            self.unallocated.code = "ZZZ999999_TEST"
            with self.assertRaises(UserError) as cm:
                self.importer._build_lookups()
            self.assertIn("999999", str(cm.exception))
        finally:
            self.unallocated.code = original_code

    # ------------------------------------------------------------------
    # AC8: Currency-bearing closing line
    # ------------------------------------------------------------------

    def test_currency_bearing_closing_line(self):
        # Pick a currency that is NOT the company currency.
        other_currency = self.env["res.currency"].with_context(
            active_test=False,
        ).search([("id", "!=", self.company_currency_id)], limit=1)
        if not other_currency:
            self.skipTest("No second currency available for FX test.")
        if not other_currency.active:
            other_currency.active = True
            self.currencies_dict[other_currency.name] = other_currency.id

        header = self._make_header(transtype="-3")
        jdt1_lines = [
            self._make_jdt1_row(
                "4000-INC", "I", debit=1000.0, credit=0.0, line_id=0,
                fccurrency=other_currency.name,
                fcdebit=800.0, fccredit=0.0,
            ),
            self._make_jdt1_row(
                "3999-CLR", "N", debit=0.0, credit=1000.0, line_id=1,
                fccurrency=other_currency.name,
                fcdebit=0.0, fccredit=800.0,
            ),
        ]
        move_vals = self._build_move_vals(header, jdt1_lines)
        line_vals = [
            cmd[2] for cmd in move_vals["line_ids"]
            if isinstance(cmd, (list, tuple)) and cmd[0] == 0
        ]
        income_line = next(
            l for l in line_vals if l["debit"] == 1000.0
        )
        self.assertEqual(
            income_line["account_id"], self.unallocated.id,
            "P&L line redirected even with FX fields set.",
        )
        self.assertEqual(
            income_line.get("currency_id"), other_currency.id,
            "Currency must be preserved across the redirect.",
        )
        self.assertEqual(
            income_line.get("amount_currency"), 800.0,
            "amount_currency must be preserved across the redirect.",
        )
