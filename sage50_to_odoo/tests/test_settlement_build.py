"""Turning one Sage receipt/payment entry into one settlement move.

Acceptance criteria
-------------------
- The move is Sage's entry, line for line: every non-control line (bank,
  discount, anything else) is carried over at its own amount, unchanged.
- The control line is replaced by one line per application against an
  imported document, carrying that document's partner, the exact applied
  amount, and the document to reconcile it with.
- Whatever part of the control amount no imported document accounts for stays
  on a remainder line with the entry's own partner and nothing to reconcile —
  so the move still balances and the ledger still equals Sage's.
- An application whose control account the entry does not touch is refused
  rather than forced in.
"""

from odoo.tests import BaseCase

from odoo.addons.sage50_to_odoo.models.pipelines.account_move_settlement_etl \
    import build_settlement, match_applications

AR, BANK, DISCOUNT = 11000000, 10200000, 41400000


def _entry(lines):
    return {
        "sage_gl_entry_ref": "tjently:77",
        "date": "2022-03-01",
        "source": "R-1001",
        "comment": "Receipt",
        "lines": [
            {"account": account, "balance": balance, "label": ""}
            for account, balance in lines
        ],
    }


def _application(move_id, amount, partner_id=7, control=AR, sage_id=None):
    return {
        "sage_id": sage_id or move_id,
        "move_id": move_id,
        "partner_id": partner_id,
        "control": control,
        "amount": amount,
    }


class TestBuildSettlement(BaseCase):

    def test_one_receipt_settling_two_invoices(self):
        spec = build_settlement(
            _entry([(BANK, 1000.0), (AR, -1000.0)]),
            [_application(1, -600.0), _application(2, -400.0)],
            control_accounts={AR},
            remainder_partner_id=7,
        )
        bank = [line for line in spec["lines"] if line["account"] == BANK]
        control = [line for line in spec["lines"] if line["account"] == AR]
        self.assertEqual([line["balance"] for line in bank], [1000.0])
        self.assertEqual(
            sorted((line["reconcile_move_id"], line["balance"])
                   for line in control),
            [(1, -600.0), (2, -400.0)],
        )
        self.assertTrue(all(line["partner_id"] == 7 for line in control))
        self.assertAlmostEqual(
            sum(line["balance"] for line in spec["lines"]), 0.0, places=2,
        )
        self.assertEqual(spec["sage_gl_entry_ref"], "tjently:77")

    def test_discount_line_is_carried_over(self):
        # 100 invoice settled by 98 of money and 2 of early-payment discount:
        # two applications against the same document, one entry.
        spec = build_settlement(
            _entry([(BANK, 98.0), (DISCOUNT, 2.0), (AR, -100.0)]),
            [_application(1, -98.0, sage_id=11),
             _application(1, -2.0, sage_id=12)],
            control_accounts={AR},
            remainder_partner_id=7,
        )
        by_account = {}
        for line in spec["lines"]:
            by_account.setdefault(line["account"], []).append(line["balance"])
        self.assertEqual(by_account[BANK], [98.0])
        self.assertEqual(by_account[DISCOUNT], [2.0])
        self.assertEqual(sorted(by_account[AR]), [-98.0, -2.0])

    def test_unaccounted_part_stays_on_a_remainder_line(self):
        spec = build_settlement(
            _entry([(BANK, 1000.0), (AR, -1000.0)]),
            [_application(1, -600.0)],
            control_accounts={AR},
            remainder_partner_id=9,
        )
        remainder = [
            line for line in spec["lines"]
            if line["account"] == AR and not line["reconcile_move_id"]
        ]
        self.assertEqual(len(remainder), 1)
        self.assertEqual(remainder[0]["balance"], -400.0)
        self.assertEqual(remainder[0]["partner_id"], 9)

    def test_application_on_an_untouched_control_is_refused(self):
        with self.assertRaises(ValueError):
            build_settlement(
                _entry([(BANK, 100.0), (AR, -100.0)]),
                [_application(1, -100.0, control=20500000)],
                control_accounts={AR, 20500000},
                remainder_partner_id=7,
            )


def _candidate(entry_id, control_amount):
    return {
        "sage_gl_entry_ref": f"tjeh01:{entry_id}",
        "entry_id": entry_id,
        "date": "2021-10-16",
        "source": "Comptant",
        "comment": "",
        "lines": [
            {"account": BANK, "balance": -control_amount, "label": ""},
            {"account": AR, "balance": control_amount, "label": ""},
        ],
    }


class TestMatchApplications(BaseCase):
    """Several payments to one partner on one day under one number.

    Acceptance: the day's applications are matched to the day's entries —
    the whole group to one entry when that works, else each cheque's
    applications to the entry of that amount, else each application alone —
    and only what still matches nothing is left over.
    """

    def test_whole_group_to_one_entry(self):
        matched, left = match_applications(
            [_candidate(1, 1000.0)],
            [_application(1, 600.0, sage_id=1), _application(2, 400.0, sage_id=2)],
            AR, set(),
        )
        self.assertEqual([(e["entry_id"], len(a)) for e, a in matched], [(1, 2)])
        self.assertEqual(left, [])

    def test_split_by_cheque(self):
        apps = [
            dict(_application(1, 1216.0, sage_id=1), cheque=100),
            dict(_application(2, 906.5, sage_id=2), cheque=101),
            dict(_application(3, 300.0, sage_id=3), cheque=101),
        ]
        matched, left = match_applications(
            [_candidate(7, 1206.5), _candidate(8, 1216.0)], apps, AR, set(),
        )
        self.assertEqual(
            sorted((e["entry_id"], sorted(a["sage_id"] for a in apps_))
                   for e, apps_ in matched),
            [(7, [2, 3]), (8, [1])],
        )
        self.assertEqual(left, [])

    def test_single_applications_when_no_cheque(self):
        matched, left = match_applications(
            [_candidate(7, 906.5), _candidate(8, 1216.0)],
            [_application(1, 1216.0, sage_id=1),
             _application(2, 906.5, sage_id=2),
             _application(3, 5.0, sage_id=3)],
            AR, {"tjeh01:99"},
        )
        self.assertEqual(
            sorted((e["entry_id"], [a["sage_id"] for a in apps_])
                   for e, apps_ in matched),
            [(7, [2]), (8, [1])],
        )
        self.assertEqual([a["sage_id"] for a in left], [3])

    def test_claimed_entries_are_not_reused(self):
        matched, left = match_applications(
            [_candidate(8, 1216.0)], [_application(1, 1216.0)],
            AR, {"tjeh01:8"},
        )
        self.assertEqual(matched, [])
        self.assertEqual(len(left), 1)
