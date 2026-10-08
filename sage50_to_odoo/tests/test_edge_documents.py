"""Documents with nothing but tax, and documents worth nothing.

Acceptance criteria
-------------------
- A bill whose GL entry has only tax lines and the payable (an import-duty or
  customs bill) is imported with those tax lines as its lines — untaxed, on
  the tax accounts — instead of failing for want of a base line. The ledger
  is Sage's either way; there is no base to attach a tax to.
- A document worth 0.00 with no lines to import (Sage keeps voided and empty
  invoices) is skipped quietly: nothing to import is not a failure, and
  reporting it as one buries the real failures.
"""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEdgeDocuments(TransactionCase):

    def setUp(self):
        super().setUp()
        self.importer = self.env["sage.open.item.importer"]

    def test_tax_only_document_uses_its_tax_lines(self):
        base, tax = self.importer._tax_only_as_base([], [
            {"account": 22150000, "amount": 132.50, "tax": "gst"},
            {"account": 22450000, "amount": 264.34, "tax": "qst"},
        ])
        self.assertEqual(tax, [])
        self.assertEqual(
            [(line["account"], line["amount"]) for line in base],
            [(22150000, 132.50), (22450000, 264.34)],
        )
        self.assertTrue(all(line["taxable"] is False for line in base))

    def test_document_with_a_base_is_left_alone(self):
        base_in = [{"account": 51000000, "amount": 100.0, "label": None}]
        tax_in = [{"account": 22150000, "amount": 5.0, "tax": "gst"}]
        base, tax = self.importer._tax_only_as_base(base_in, tax_in)
        self.assertIs(base, base_in)
        self.assertIs(tax, tax_in)

    def test_empty_zero_document_is_not_a_failure(self):
        document = {
            "side": "customer", "sage_doc_id": 999001, "number": "3316",
            "sage_partner_id": 1, "partner_name": "x", "date": "2024-03-20",
            "move_type": "out_invoice", "original": 0.0, "residual": 0.0,
            "base_lines": [], "tax_lines": [],
        }
        self.assertTrue(self.importer._nothing_to_import(document))
        document["original"] = 396.84
        self.assertFalse(self.importer._nothing_to_import(document))

    def test_document_posts_on_its_ledger_date(self):
        # The document says 2023-08-29; Sage posted its entry on 2023-09-02,
        # in the next fiscal year. The ledger date is the accounting date.
        self.assertEqual(
            self.importer._accounting_date(
                {"date": "2023-08-29", "gl_date": "2023-09-02"},
            ),
            "2023-09-02",
        )
        self.assertEqual(
            self.importer._accounting_date({"date": "2023-08-29"}),
            "2023-08-29",
        )

    def test_item_lines_follow_the_ledger_accounts(self):
        # Sage's item lines put both lines on revenue; its ledger put the
        # first on the deposit account. The ledger wins, line by line.
        items = [
            {"account": 41000000, "amount": -4293.12, "sage_product_id": 801},
            {"account": 41000000, "amount": -99.84, "sage_product_id": 801},
        ]
        gl = [
            {"account": 14500000, "amount": -4293.12},
            {"account": 41000000, "amount": -99.84},
        ]
        self.importer._align_item_accounts(items, gl)
        self.assertEqual(
            [line["account"] for line in items], [14500000, 41000000],
        )

    def test_item_lines_left_alone_when_they_agree(self):
        items = [{"account": 41000000, "amount": -50.0}]
        self.importer._align_item_accounts(
            items, [{"account": 41000000, "amount": -50.0}],
        )
        self.assertEqual(items[0]["account"], 41000000)

    def test_alignment_reports_when_the_ledger_cannot_be_matched(self):
        # Sage split this invoice between the deposit and other revenue in
        # amounts no item line carries.
        items = [
            {"account": 41600000, "amount": -465.0},
            {"account": 41800000, "amount": -1490.0},
        ]
        gl = [
            {"account": 14500000, "amount": -479.9},
            {"account": 48000000, "amount": -1475.1},
        ]
        self.assertFalse(self.importer._align_item_accounts(items, gl))
        self.assertTrue(self.importer._align_item_accounts(
            [{"account": 41000000, "amount": -4293.12},
             {"account": 41000000, "amount": -99.84}],
            [{"account": 14500000, "amount": -4293.12},
             {"account": 41000000, "amount": -99.84}],
        ))
