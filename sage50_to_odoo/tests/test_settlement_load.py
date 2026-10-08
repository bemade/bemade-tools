"""Posting a settlement move and reconciling it exactly as Sage applied it.

Acceptance criteria
-------------------
- The settlement posts as one move in the take-on journal, carrying the Sage
  entry reference so the general-ledger replay does not post it again.
- Each split control line is reconciled with its own document, for exactly
  the applied amount: a document Sage shows as part paid is left with Sage's
  residual, one Sage shows as settled — money and discount together — is paid.
- Reconciling creates nothing else: no exchange-difference or cash-basis move.
- A remainder line (money applied to something not imported) stays open.
"""

from odoo import Command
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.etl_framework import ETLContext

AR, BANK, DISCOUNT = 11000000, 10200000, 41400000


@tagged("post_install", "-at_install")
class TestSettlementLoad(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.accounts = {
            AR: cls.company_data["default_account_receivable"].id,
            BANK: cls.company_data["default_journal_bank"].default_account_id.id,
            DISCOUNT: cls.company_data["default_account_expense"].id,
        }
        cls.ctx = ETLContext(cr=None, env=cls.env, source_config={
            "company_id": cls.company_data["company"].id,
            "journal_id": cls.company_data["default_journal_misc"].id,
        })
        cls.importer = cls.env["sage.settlement.importer"]

    def _invoice(self, amount):
        return self._create_invoice(
            partner_id=self.partner_a.id,
            invoice_date="2022-02-01",
            invoice_line_ids=[Command.create({
                "name": "Goods", "price_unit": amount, "tax_ids": [],
            })],
            post=True,
        )

    def _line(self, account, balance, move=None, partner=None):
        return {
            "account": account,
            "balance": balance,
            "label": "",
            "partner_id": partner.id if partner else False,
            "reconcile_move_id": move.id if move else False,
        }

    def _spec(self, lines):
        return {
            "sage_gl_entry_ref": "tjently:501",
            "date": "2022-03-01",
            "ref": "Sage R-1001",
            "narration": "Receipt",
            "lines": lines,
        }

    def test_part_payment_leaves_sage_residual(self):
        invoice = self._invoice(1000.0)
        moves_before = self.env["account.move"].search_count([])
        move = self.importer._post_settlement(self.ctx, self._spec([
            self._line(BANK, 600.0),
            self._line(AR, -600.0, invoice, self.partner_a),
        ]), self.accounts)
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.sage_gl_entry_ref, "tjently:501")
        self.assertEqual(move.journal_id,
                         self.company_data["default_journal_misc"])
        self.assertAlmostEqual(invoice.amount_residual, 400.0)
        self.assertEqual(invoice.payment_state, "partial")
        # One settlement, nothing created by the reconciliation itself.
        self.assertEqual(
            self.env["account.move"].search_count([]), moves_before + 1,
        )

    def test_money_and_discount_settle_the_document(self):
        invoice = self._invoice(100.0)
        self.importer._post_settlement(self.ctx, self._spec([
            self._line(BANK, 98.0),
            self._line(DISCOUNT, 2.0),
            self._line(AR, -98.0, invoice, self.partner_a),
            self._line(AR, -2.0, invoice, self.partner_a),
        ]), self.accounts)
        self.assertAlmostEqual(invoice.amount_residual, 0.0)
        self.assertEqual(invoice.payment_state, "paid")

    def test_one_receipt_two_invoices_and_a_remainder(self):
        first, second = self._invoice(600.0), self._invoice(300.0)
        move = self.importer._post_settlement(self.ctx, self._spec([
            self._line(BANK, 1000.0),
            self._line(AR, -600.0, first, self.partner_a),
            self._line(AR, -300.0, second, self.partner_a),
            self._line(AR, -100.0, partner=self.partner_a),
        ]), self.accounts)
        self.assertEqual(first.payment_state, "paid")
        self.assertEqual(second.payment_state, "paid")
        remainder = move.line_ids.filtered(
            lambda line: line.account_id.id == self.accounts[AR]
            and not line.reconciled
        )
        self.assertEqual(len(remainder), 1)
        self.assertAlmostEqual(remainder.balance, -100.0)
