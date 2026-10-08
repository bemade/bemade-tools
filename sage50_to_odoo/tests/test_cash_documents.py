"""Documents paid on the spot, and credit notes applied to invoices.

Acceptance criteria
-------------------
Cash documents ("paid by cash / card" in Sage):
- Sage posts the bill and its payment as ONE entry with no control line,
  numbered with the payment method rather than the document ("Comptant"), and
  whose comment starts with "<document number>, ". That entry is the
  document's own: found by the comment prefix, a CORR reversal never taken,
  and of an original / reversal / correction the correction (the latest).
- The document is built from that entry's expense and tax lines; the line on
  the account the payment was drawn from (the application's bank account) is
  not part of the document.
- That payment line becomes a settlement: the payment account line as Sage
  posted it, and the control split per application, reconciled with the
  document. Document + settlement together equal Sage's single entry.

Credit notes applied to invoices:
- Sage moves the amount from one document to the other with no GL entry at
  all (+x on one, -x on the other, same number and date). The two documents
  are reconciled with each other directly, for exactly that amount.
"""

from odoo import Command
from odoo.tests import BaseCase, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.etl_framework import ETLContext
from odoo.addons.sage50_to_odoo import tools
from odoo.addons.sage50_to_odoo.models.pipelines.account_move_settlement_etl \
    import build_cash_settlement

AP, CARD, EXPENSE, GST, QST = 20600000, 21500000, 52000000, 22150000, 22450000


class TestPickCashEntry(BaseCase):

    def _header(self, entry_id, source, comment):
        return {"lId": entry_id, "sSource": source, "sComment": comment}

    def test_correction_wins_over_original_and_reversal(self):
        headers = [
            self._header(310, "Comptant", "MC_0001, Office supplier"),
            self._header(319, "CORR Comptant",
                         "Contrep. de J310. Corr. est J320."),
            self._header(320, "Comptant", "MC_0001, Office supplier"),
        ]
        self.assertEqual(
            tools.pick_cash_entry(headers, "MC_0001")["lId"], 320,
        )

    def test_prefix_must_be_the_whole_number(self):
        headers = [
            self._header(10, "Comptant", "MC_0001B, Office supplier"),
        ]
        self.assertIsNone(tools.pick_cash_entry(headers, "MC_0001"))

    def test_reversal_alone_is_never_taken(self):
        headers = [
            self._header(319, "CORR Comptant", "MC_0001, Office supplier"),
        ]
        self.assertIsNone(tools.pick_cash_entry(headers, "MC_0001"))


class TestBuildCashSettlement(BaseCase):

    def test_card_line_becomes_the_settlement(self):
        entry = {
            "sage_gl_entry_ref": "tjeh01:320",
            "date": "2021-09-01",
            "source": "Comptant",
            "comment": "MC_0001, Office supplier",
            "lines": [
                {"account": EXPENSE, "balance": 193.96, "label": ""},
                {"account": GST, "balance": 9.70, "label": ""},
                {"account": QST, "balance": 19.35, "label": ""},
                {"account": CARD, "balance": -223.01, "label": ""},
            ],
        }
        spec = build_cash_settlement(
            entry, payment_accounts={CARD},
            applications=[{
                "sage_id": 5, "move_id": 41, "partner_id": 3,
                "control": AP, "amount": 223.01,
            }],
            control=AP,
        )
        by_account = {}
        for line in spec["lines"]:
            by_account.setdefault(line["account"], []).append(line)
        self.assertEqual(set(by_account), {CARD, AP})
        self.assertEqual(by_account[CARD][0]["balance"], -223.01)
        self.assertEqual(by_account[AP][0]["balance"], 223.01)
        self.assertEqual(by_account[AP][0]["reconcile_move_id"], 41)
        self.assertNotEqual(spec["sage_gl_entry_ref"], "tjeh01:320")
        self.assertTrue(spec["sage_gl_entry_ref"].startswith("tjeh01:320"))


class TestCashSettlementGuard(BaseCase):
    """A settlement with no payment line balances the control against itself
    and loses the money: refused, so the caller infers or reports instead."""

    def test_payment_account_absent_from_the_entry_is_refused(self):
        entry = {
            "sage_gl_entry_ref": "tjeh01:5101", "date": "2022-01-11",
            "source": "Mastercard", "comment": "7000123, Supplier A",
            "lines": [
                {"account": EXPENSE, "balance": 3151.88, "label": ""},
                {"account": GST, "balance": 157.59, "label": ""},
                {"account": QST, "balance": 314.40, "label": ""},
                {"account": CARD, "balance": -3623.87, "label": ""},
            ],
        }
        with self.assertRaises(ValueError):
            build_cash_settlement(
                entry, payment_accounts={10200000},
                applications=[{
                    "sage_id": 5, "move_id": 41, "partner_id": 3,
                    "control": AP, "amount": 3623.87,
                }],
                control=AP,
            )


@tagged("post_install", "-at_install")
class TestContraReconcile(AccountTestInvoicingCommon):

    def _document(self, move_type, amount):
        return self._create_invoice(
            move_type=move_type,
            partner_id=self.partner_a.id,
            invoice_date="2022-01-17",
            invoice_line_ids=[Command.create({
                "name": "Goods", "price_unit": amount, "tax_ids": [],
            })],
            post=True,
        )

    def test_credit_note_applied_to_invoice(self):
        invoice = self._document("out_invoice", 100.0)
        credit = self._document("out_refund", 61.5)
        ctx = ETLContext(cr=None, env=self.env, source_config={
            "company_id": self.company_data["company"].id,
        })
        self.env["sage.settlement.importer"]._reconcile_documents(
            ctx, invoice | credit,
        )
        self.assertAlmostEqual(invoice.amount_residual, 38.5)
        self.assertEqual(credit.payment_state, "paid")


class TestInferPaymentAccounts(BaseCase):
    """Which line of a cash entry is the payment, when Sage does not say.

    Acceptance: the payment sits where the control line would have been —
    the non-tax line(s) on the control's side, adding up to exactly the
    document's control amount. Anything ambiguous infers nothing.
    """

    TAXES = {GST, QST}

    def test_card_payment_on_a_bill(self):
        lines = [
            {"account": EXPENSE, "balance": 3151.88},
            {"account": GST, "balance": 157.59},
            {"account": QST, "balance": 314.40},
            {"account": CARD, "balance": -3623.87},
        ]
        self.assertEqual(
            tools.infer_payment_accounts(lines, self.TAXES, -3623.87),
            {CARD},
        )

    def test_bank_receipt_on_a_cash_sale(self):
        lines = [
            {"account": 10100000, "balance": 5.0},
            {"account": 43000000, "balance": -5.0},
        ]
        self.assertEqual(
            tools.infer_payment_accounts(lines, self.TAXES, 5.0), {10100000},
        )

    def test_ambiguous_infers_nothing(self):
        lines = [
            {"account": 10100000, "balance": 3.0},
            {"account": 10200000, "balance": 3.0},
            {"account": 43000000, "balance": -5.0},
            {"account": 43200000, "balance": -1.0},
        ]
        self.assertEqual(
            tools.infer_payment_accounts(lines, self.TAXES, 5.0), set(),
        )
