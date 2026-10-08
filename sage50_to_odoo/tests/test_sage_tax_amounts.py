"""Documents carry the tax Sage recorded, not the tax Odoo recomputes.

Acceptance criteria
-------------------
- Sage lets a bookkeeper type the tax on a bill (bill 5005 on one file:
  7,059.58 of base with GST and QST worth exactly 5 % and 9.975 % of
  8,615.00). Odoo recomputes tax from the base, lands elsewhere, and the
  bill, the payable and the tax receivable stop tying to Sage.
- Before posting, each tax group is set to the tax Sage recorded on that
  group's account(s) — the same override a user makes on the invoice form —
  so the document's tax and total are Sage's.
- A document whose tax already agrees is left untouched.
"""

from odoo import Command
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestSageTaxAmounts(AccountTestInvoicingCommon):

    def _draft_bill(self, amount):
        return self._create_invoice(
            move_type="in_invoice",
            partner_id=self.partner_a.id,
            invoice_date="2023-01-10",
            invoice_line_ids=[Command.create({
                "name": "Fees", "price_unit": amount,
                "tax_ids": [Command.set(
                    self.company_data["default_tax_purchase"].ids
                )],
            })],
        )

    def test_recorded_tax_replaces_computed_tax(self):
        bill = self._draft_bill(7059.58)
        tax_account = bill.line_ids.filtered("tax_line_id").account_id
        self.env["sage.open.item.importer"]._force_sage_tax(
            bill, {tax_account.id: 1290.10},
        )
        bill.action_post()
        self.assertAlmostEqual(bill.amount_tax, 1290.10)
        self.assertAlmostEqual(bill.amount_total, 8349.68)

    def test_agreeing_tax_is_untouched(self):
        bill = self._draft_bill(100.0)
        tax_account = bill.line_ids.filtered("tax_line_id").account_id
        before = bill.amount_tax
        self.env["sage.open.item.importer"]._force_sage_tax(
            bill, {tax_account.id: before},
        )
        self.assertAlmostEqual(bill.amount_tax, before)
