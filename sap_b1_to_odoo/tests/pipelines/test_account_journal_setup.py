#
#    Bemade Inc.
#
#    Copyright (C) 2026-September Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for AccountJournalSetup — one bank journal per SAP cash account.

Regression for the lost first cash account: ``transform_journals`` numbered
bank journals ``BNK{idx}`` from 1 and skipped the *account* whenever that
code already existed — and ``existing_codes`` includes archived journals, so
Odoo's chart-template default ``BNK1`` (archived by this very pipeline)
silently swallowed the lowest-coded cash account. On a real SAP chart that
was the operating checking account, the one every A/P payment posts to.

Acceptance criteria:

1. (test_first_cash_account_gets_a_journal_despite_archived_bnk1) with an
   archived journal already coded ``BNK1`` and two cash accounts, transform
   emits a bank journal for EACH cash account; the first account is not
   skipped, and the code chosen for it is one no journal (active or archived)
   already carries.
2. (test_cash_account_with_existing_journal_is_not_duplicated) a cash account
   that already has a bank journal pointing at it (``default_account_id``)
   gets no second journal, whatever the existing journal's code — presence is
   keyed on the account, not on the ``BNK{n}`` code.
3. (test_bank_journal_codes_are_unique) the emitted bank journal codes are
   distinct from each other and from every existing journal code.
4. (test_archived_journal_does_not_count_as_presence) an ARCHIVED journal
   pointing at a cash account does not suppress a new one: the account still
   ends up with an active journal, under a code the archived one is not using.
"""

from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.sap_b1_to_odoo.models.pipelines.account_journal_setup import (
    AccountJournalSetup,
)


def _make_ctx(env):
    ctx = MagicMock()
    ctx.env = env
    return ctx


@tagged("-at_install", "post_install", "account_journal_setup")
class TestAccountJournalSetup(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.setup = cls.env["account.journal.setup"]
        cls.Journal = cls.env["account.journal"].with_context(active_test=False)
        cls.Account = cls.env["account.account"]

        # The chart template's default bank journal, archived — what the
        # pipeline itself leaves behind before creating the SAP ones.
        bnk1 = cls.Journal.search(
            [("code", "=", "BNK1"), ("company_id", "=", cls.company.id)], limit=1
        )
        if not bnk1:
            bnk1 = cls.Journal.create(
                {"name": "Bank", "code": "BNK1", "type": "bank"}
            )
        bnk1.active = False
        cls.bnk1 = bnk1

        cls.checking = cls.Account.create(
            {
                "name": "Checking Account",
                "code": "10210.000",
                "account_type": "asset_cash",
            }
        )
        cls.sweep = cls.Account.create(
            {
                "name": "Sweep Account",
                "code": "10215.000",
                "account_type": "asset_cash",
            }
        )

    def _transform(self, cash_accounts):
        extracted = {
            "extract_journal_accounts": {
                "cash_accounts": cash_accounts,
                "ar_account": None,
                "ap_account": None,
                "income_account": None,
                "expense_account": None,
                "stock_valuation_account": None,
            }
        }
        # Bound to the base implementation so an overlay's override cannot
        # change what this test guards.
        return AccountJournalSetup.transform_journals(
            self.setup, _make_ctx(self.env), extracted
        )

    def _bank_vals(self, result):
        return [v for v in result["journal_vals"] if v.get("type") == "bank"]

    def test_first_cash_account_gets_a_journal_despite_archived_bnk1(self):
        result = self._transform(self.checking | self.sweep)
        bank_vals = self._bank_vals(result)
        by_account = {v["default_account_id"]: v for v in bank_vals}

        self.assertIn(
            self.checking.id,
            by_account,
            "the first cash account lost its journal to the archived BNK1",
        )
        self.assertIn(self.sweep.id, by_account)
        existing_codes = set(self.Journal.search([]).mapped("code"))
        self.assertNotIn(by_account[self.checking.id]["code"], existing_codes)

    def test_cash_account_with_existing_journal_is_not_duplicated(self):
        self.Journal.create(
            {
                "name": "Checking Account",
                "code": "CHK",
                "type": "bank",
                "default_account_id": self.checking.id,
            }
        )
        result = self._transform(self.checking | self.sweep)
        accounts = [v["default_account_id"] for v in self._bank_vals(result)]
        self.assertNotIn(self.checking.id, accounts)
        self.assertEqual(accounts, [self.sweep.id])

    def test_bank_journal_codes_are_unique(self):
        result = self._transform(self.checking | self.sweep)
        codes = [v["code"] for v in self._bank_vals(result)]
        self.assertEqual(len(codes), len(set(codes)))
        existing_codes = set(self.Journal.search([]).mapped("code"))
        self.assertFalse(set(codes) & existing_codes)

    def test_archived_journal_does_not_count_as_presence(self):
        self.Journal.create(
            {
                "name": "Old Checking",
                "code": "OLDCHK",
                "type": "bank",
                "default_account_id": self.checking.id,
                "active": False,
            }
        )
        result = self._transform(self.checking | self.sweep)
        by_account = {v["default_account_id"]: v for v in self._bank_vals(result)}
        self.assertIn(self.checking.id, by_account)
        self.assertNotEqual(by_account[self.checking.id]["code"], "OLDCHK")
