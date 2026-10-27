"""Tests for the one-pass QBO application-group reconciler.

QBO settles documents through application lines on Payments and BillPayments:
each Line's Amount is the total applied to one linked transaction (cash and
credits combined for invoice-side links).  The old phase-3/4 reconciliation
replayed these in two passes that guessed at each other's state — cash was
applied in QBO line order, then credits were blind-paired against invoices
the cash had already closed — leaving offsetting open pairs on the aged
reports (e.g. a payment covering 3 bills and 2 vendor credits closed the
front bills with cash and orphaned both the credits and the tail bill).

The reconciler treats each (Bill)Payment as ONE group — the payment's own
control line plus every linked document's control line, each capped at its
QBO application amount — and greedily fills credits against debits.  Because
per-document totals are fixed by the caps, any maximal fill reproduces QBO's
residual on every document.

Acceptance criteria:
1.  Group building resolves every LinkedTxn type through its qbo_id map:
    Invoice, Bill, CreditMemo, VendorCredit, JournalEntry, Purchase/Expense
    (both spellings), and Deposit; the payment's own move joins at TotalAmt.
2.  Zero-total payments build a group without a self member.
3.  Unresolvable links are reported, resolved members still group; a group
    with fewer than two members is dropped.
4.  Mixed cash+credit groups settle completely (the Brenntag/Nustream shapes
    that the two-pass design could not).
5.  A Deposit-only BillPayment (vendor refund) clears both control lines
    (the Hauthaway shape).
6.  Partial applications leave exactly QBO's residual on the document.
7.  Caps hold across groups: two payments applying to one invoice each
    consume only their own amount.
8.  The pass is idempotent — re-solving a settled group applies nothing.
9.  Lines on different control accounts never cross-reconcile.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.qbo_to_odoo.models.pipelines.reconcile_pass import (
    build_application_groups,
    solve_group,
)


def _payment(qbo_id, total, *links, date="2021-03-10"):
    """A QBO (Bill)Payment dict with one Line per (amount, type, id)."""
    return {
        "Id": str(qbo_id),
        "TxnDate": date,
        "TotalAmt": total,
        "Line": [
            {"Amount": amt, "LinkedTxn": [{"TxnId": str(tid), "TxnType": ttype}]}
            for (amt, ttype, tid) in links
        ],
    }


@tagged("post_install", "-at_install", "qbo_reconcile_pass")
class TestBuildApplicationGroups(TransactionCase):
    """Pure link-resolution tests — no database records involved."""

    MAPS = {
        "invoice_map": {"555": 10},
        "bill_map": {"2219": 20, "2220": 21},
        "credit_memo_map": {"19934": 30},
        "vendor_credit_map": {"2222": 40},
        "journal_entry_map": {"4400": 50},
        "expense_map": {"19942": 60},
        "deposit_map": {"6785": 70},
        "payment_move_map": {"19943": 80},
        "bill_payment_move_map": {"2224": 90},
    }

    def test_ac1_customer_payment_all_link_types(self):
        groups = build_application_groups(
            [_payment(
                19943, 100.0,
                (25.0, "Invoice", 555),
                (10.0, "CreditMemo", 19934),
                (30.0, "Expense", 19942),
                (35.0, "JournalEntry", 4400),
            )],
            "Payment", self.MAPS,
        )
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["qbo_id"], "19943")
        self.assertEqual(g["kind"], "Payment")
        self.assertEqual(g["date"], "2021-03-10")
        self.assertEqual(
            dict(g["members"]),
            {80: 100.0, 10: 25.0, 30: 10.0, 60: 30.0, 50: 35.0},
        )
        self.assertFalse(g["unresolved"])

    def test_ac1_bill_payment_deposit_and_purchase(self):
        groups = build_application_groups(
            [_payment(
                2224, 50.0,
                (20.0, "Bill", 2219),
                (15.0, "VendorCredit", 2222),
                (10.0, "Purchase", 19942),
                (5.0, "Deposit", 6785),
            )],
            "BillPayment", self.MAPS,
        )
        g = groups[0]
        self.assertEqual(
            dict(g["members"]),
            {90: 50.0, 20: 20.0, 40: 15.0, 60: 10.0, 70: 5.0},
        )

    def test_ac1_purchase_spellings_resolve(self):
        """QBO names a linked Purchase after its PaymentType — every spelling
        must resolve through expense_map (Payment 5491 links 'Check' 5492)."""
        for spelling in ("Purchase", "Expense", "Check", "CreditCardCredit"):
            groups = build_application_groups(
                [_payment(
                    5491, 0,
                    (1100.0, spelling, 19942),
                    (1100.0, "CreditMemo", 19934),
                )],
                "Payment", self.MAPS,
            )
            self.assertEqual(
                dict(groups[0]["members"]), {60: 1100.0, 30: 1100.0},
                f"TxnType {spelling!r} must resolve via expense_map",
            )

    def test_ac1_same_move_caps_merge(self):
        groups = build_application_groups(
            [_payment(
                19943, 0,
                (25.0, "Invoice", 555),
                (10.0, "Invoice", 555),
                (35.0, "CreditMemo", 19934),
            )],
            "Payment", self.MAPS,
        )
        self.assertEqual(dict(groups[0]["members"]), {10: 35.0, 30: 35.0})

    def test_ac2_zero_total_has_no_self_member(self):
        groups = build_application_groups(
            [_payment(
                19943, 0,
                (21279.16, "Expense", 19942),
                (21279.16, "CreditMemo", 19934),
            )],
            "Payment", self.MAPS,
        )
        g = groups[0]
        self.assertEqual(dict(g["members"]), {60: 21279.16, 30: 21279.16})

    def test_ac3_unresolved_links_reported(self):
        groups = build_application_groups(
            [_payment(
                19943, 100.0,
                (60.0, "Invoice", 555),
                (40.0, "Invoice", 999),        # not in map
                (10.0, "SalesReceipt", 123),   # unknown type
            )],
            "Payment", self.MAPS,
        )
        g = groups[0]
        self.assertEqual(dict(g["members"]), {80: 100.0, 10: 60.0})
        self.assertCountEqual(
            g["unresolved"],
            [("Invoice", "999", 40.0), ("SalesReceipt", "123", 10.0)],
        )

    def test_ac3_single_member_group_dropped(self):
        groups = build_application_groups(
            [_payment(1, 100.0, (100.0, "Invoice", 999))],
            "Payment", {"invoice_map": {}, "payment_move_map": {"1": 80}},
        )
        self.assertEqual(groups, [])


@tagged("post_install", "-at_install", "qbo_reconcile_pass")
class TestSolveGroup(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Account = cls.env["account.account"]
        cls.ar = Account.create({
            "name": "AR test", "code": "ARQR1100",
            "account_type": "asset_receivable", "reconcile": True,
        })
        cls.ar2 = Account.create({
            "name": "AR test 2", "code": "ARQR1101",
            "account_type": "asset_receivable", "reconcile": True,
        })
        cls.ap = Account.create({
            "name": "AP test", "code": "APQR2000",
            "account_type": "liability_payable", "reconcile": True,
        })
        cls.other = Account.create({
            "name": "Clearing test", "code": "CLRQR1005",
            "account_type": "asset_current",
        })
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general")], limit=1,
        ) or cls.env["account.journal"].create({
            "name": "Misc test", "code": "MSCQR", "type": "general",
        })

    def _move(self, control_acct, balance):
        """A posted entry whose control line has *balance* (+debit/-credit)."""
        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": self.journal.id,
            "line_ids": [
                (0, 0, {"account_id": control_acct.id,
                        "debit": max(balance, 0.0),
                        "credit": max(-balance, 0.0),
                        "name": "control"}),
                (0, 0, {"account_id": self.other.id,
                        "debit": max(-balance, 0.0),
                        "credit": max(balance, 0.0),
                        "name": "offset"}),
            ],
        })
        move.action_post()
        return move

    def _residual(self, move, acct=None):
        acct = acct or self.ar
        return sum(
            move.line_ids.filtered(
                lambda l, a=acct: l.account_id == a
            ).mapped("amount_residual")
        )

    def _group(self, *members, qbo_id="1", kind="Payment", date="2021-03-10"):
        return {
            "qbo_id": qbo_id, "kind": kind, "date": date,
            "members": [(m.id, cap) for m, cap in members],
            "unresolved": [],
        }

    def test_ac4_brenntag_cash_plus_credits_settles_all(self):
        """BP 2224: cash 3153.75 over 3 bills + 2 VCs — every document must
        close, which the two-pass design could not achieve."""
        bills = [self._move(self.ap, -amt)
                 for amt in (979.88, 1397.61, 1408.62)]
        vcs = [self._move(self.ap, amt) for amt in (373.67, 258.69)]
        pay = self._move(self.ap, 3153.75)  # cash debits AP
        group = self._group(
            (pay, 3153.75),
            (bills[0], 979.88), (bills[1], 1397.61), (bills[2], 1408.62),
            (vcs[0], 373.67), (vcs[1], 258.69),
            kind="BillPayment", qbo_id="2224",
        )
        applied = solve_group(self.env, group)
        self.assertTrue(applied)
        for m in bills + vcs + [pay]:
            self.assertAlmostEqual(self._residual(m, self.ap), 0.0, 2)

    def test_ac4_nustream_multi_invoice_multi_cm(self):
        """Pay 1461: cash 20000 + 5 CMs over 4 invoices — all close."""
        inv_amts = (15242.23, 3276.79, 1149.75, 24365.26)
        cm_amts = (1820.86, 1379.7, 4961.17, 2989.35, 12882.95)
        invs = [self._move(self.ar, amt) for amt in inv_amts]
        cms = [self._move(self.ar, -amt) for amt in cm_amts]
        pay = self._move(self.ar, -20000.0)
        group = self._group(
            (pay, 20000.0),
            *[(m, a) for m, a in zip(invs, inv_amts)],
            *[(m, a) for m, a in zip(cms, cm_amts)],
            qbo_id="1461",
        )
        solve_group(self.env, group)
        for m in invs + cms + [pay]:
            self.assertAlmostEqual(self._residual(m), 0.0, 2)

    def test_ac5_deposit_only_bill_payment(self):
        """BP 6787 (Hauthaway): the payment's only link is a Deposit coded to
        AP — the two control lines must clear each other."""
        deposit = self._move(self.ap, -8046.0)
        pay = self._move(self.ap, 8046.0)
        group = self._group(
            (pay, 8046.0), (deposit, 8046.0),
            kind="BillPayment", qbo_id="6787",
        )
        applied = solve_group(self.env, group)
        self.assertEqual(applied, 1)
        self.assertAlmostEqual(self._residual(deposit, self.ap), 0.0, 2)
        self.assertAlmostEqual(self._residual(pay, self.ap), 0.0, 2)

    def test_ac6_partial_application_leaves_qbo_residual(self):
        inv = self._move(self.ar, 250.0)
        pay = self._move(self.ar, -100.0)
        group = self._group((pay, 100.0), (inv, 100.0))
        solve_group(self.env, group)
        self.assertAlmostEqual(self._residual(inv), 150.0, 2)
        self.assertAlmostEqual(self._residual(pay), 0.0, 2)

    def test_ac7_caps_hold_across_groups(self):
        inv = self._move(self.ar, 250.0)
        pay_a = self._move(self.ar, -100.0)
        pay_b = self._move(self.ar, -150.0)
        solve_group(self.env, self._group((pay_a, 100.0), (inv, 100.0)))
        self.assertAlmostEqual(self._residual(inv), 150.0, 2)
        solve_group(self.env, self._group((pay_b, 150.0), (inv, 150.0)))
        self.assertAlmostEqual(self._residual(inv), 0.0, 2)
        self.assertAlmostEqual(self._residual(pay_a), 0.0, 2)
        self.assertAlmostEqual(self._residual(pay_b), 0.0, 2)

    def test_ac8_idempotent(self):
        inv = self._move(self.ar, 500.0)
        cm = self._move(self.ar, -500.0)
        group = self._group((inv, 500.0), (cm, 500.0))
        self.assertEqual(solve_group(self.env, group), 1)
        self.assertEqual(solve_group(self.env, group), 0)

    def test_ac9_no_cross_account_reconcile(self):
        inv = self._move(self.ar, 300.0)
        cm = self._move(self.ar2, -300.0)
        group = self._group((inv, 300.0), (cm, 300.0))
        self.assertEqual(solve_group(self.env, group), 0)
        self.assertAlmostEqual(self._residual(inv, self.ar), 300.0, 2)
        self.assertAlmostEqual(self._residual(cm, self.ar2), -300.0, 2)
