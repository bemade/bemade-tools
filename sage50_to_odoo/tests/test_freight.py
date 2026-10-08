"""Freight on a document built from Sage's item lines.

Acceptance criteria
-------------------
- Sage keeps freight on the item record's header (`titrec.dFreight`), not as
  an item line. A document built from item lines carries it as a line of its
  own, on the freight account Sage links for that side (`tlinkact.lAcNFrRev`
  for sales, `lAcNFrExp` for purchases), signed the way the document reads.
- Without it the document posts short by the freight: 3,342.78 against a
  Sage invoice of 3,507.78 with 165.00 of freight.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext

FREIGHT_REVENUE, REVENUE = 48000000, 41200000


class FakeSageCursor:
    """Answers the two queries `_item_lines` makes, nothing else."""

    def __init__(self, record, lines):
        self._record, self._lines, self._rows = record, lines, []

    def execute(self, sql, args=None):
        self._rows = [self._record] if "from titrec" in sql else self._lines

    def dictfetchall(self):
        return list(self._rows)


@tagged("post_install", "-at_install")
class TestFreight(TransactionCase):

    def test_freight_becomes_a_line(self):
        cursor = FakeSageCursor(
            {"lId": 1, "dInvAmt": 3507.78, "dFreight": 165.0},
            [{"nLineNum": 1, "lInventId": 7, "lAcctId": REVENUE,
              "dQty": 10.0, "dPrice": 334.278, "dAmt": 3342.78}],
        )
        ctx = ETLContext(cr=cursor, env=self.env, source_config={})
        lines = self.env["sage.open.item.importer"]._item_lines(
            ctx, "19", 83, {}, 3507.78, -1, freight_account=FREIGHT_REVENUE,
        )
        freight = [line for line in lines if line["account"] == FREIGHT_REVENUE]
        self.assertEqual(len(freight), 1)
        # Same sign convention as the item lines: an invoice's revenue lines
        # are credits (negative, debit-positive).
        self.assertEqual(freight[0]["amount"], -165.0)
        self.assertAlmostEqual(
            sum(line["amount"] for line in lines), -3507.78, places=2,
        )

    def test_no_freight_no_line(self):
        cursor = FakeSageCursor(
            {"lId": 1, "dInvAmt": 100.0, "dFreight": 0.0},
            [{"nLineNum": 1, "lInventId": 7, "lAcctId": REVENUE,
              "dQty": 1.0, "dPrice": 100.0, "dAmt": 100.0}],
        )
        ctx = ETLContext(cr=cursor, env=self.env, source_config={})
        lines = self.env["sage.open.item.importer"]._item_lines(
            ctx, "20", 83, {}, 100.0, -1, freight_account=FREIGHT_REVENUE,
        )
        self.assertEqual([line["account"] for line in lines], [REVENUE])
