"""Tests for perpetual-COGS completion of entity-imported transactions.

Root cause (Verajet TB drift, inventory cluster): QBO books perpetual COGS
(DR Cost of Goods Sold / CR inventory valuation) directly on inventory-item
sales.  The QBO entity pipelines import only the API Line array
(revenue/AR/tax) and the invoice poster skips Odoo's own COGS generation —
which cannot fire on link-less imported invoices anyway — so the COGS /
inventory pair is dropped and inventory is never relieved.  The journal
fallback excludes any entity-imported transaction, so nothing recovers it.

``build_cogs_completion_transactions`` recovers exactly those dropped lines,
scoped to a COGS account or QBO's own inventory account (the one it debits
in "Inventory Starting Value" transactions — Odoo's category valuation
accounts differ and are not where the opening balance sits).

Acceptance criteria:
1. For an entity-imported invoice whose Odoo move booked AR + revenue but
   not the QBO COGS/inventory pair, the helper returns one transaction
   carrying ONLY the missing 9000/1403 pair — and an unrelated FX line QBO
   booked on the revenue side (5900) is excluded, so the completion
   self-balances exactly.
2. If the Odoo move already booked every cache account, nothing is returned.
3. A missing subset with no COGS/inventory account in scope is never plugged.
4. A scoped missing subset that does not self-balance is never plugged.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext
from odoo.addons.qbo_to_odoo.models.pipelines.gl_helpers import (
    build_code_maps,
    build_cogs_completion_transactions,
)


@tagged("post_install", "-at_install", "cogs_completion")
class TestCogsCompletion(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        Account = cls.env["account.account"]

        def acct(code, name, atype):
            return Account.create({
                "code": code, "name": name, "account_type": atype,
            })

        cls.ar = acct("1100", "AR", "asset_receivable")
        cls.inv = acct("1403", "Inventory Asset", "asset_current")
        cls.obe = acct("3050", "Opening Balance Equity", "equity")
        cls.other_asset = acct("1500", "Other Asset", "asset_current")
        cls.rev = acct("4100", "Sales", "income")
        cls.rev2 = acct("4200", "Sales Other", "income")
        cls.fx = acct("5900", "Exchange Gain/Loss", "expense")
        cls.cogs = acct("9000", "Cost of Goods Sold", "expense_direct_cost")

        cls.conn = cls.env["qbo.connection"].create({
            "name": "Test QBO",
            "client_id": "x",
            "client_secret": "y",
        })
        cls.cache = cls.env["qbo.journal.cache"].create({
            "qbo_connection_id": cls.conn.id,
        })
        # An "Inventory Starting Value" txn tells the helper that QBO's
        # inventory account is 1403 (DR 1403 / CR 3050 Opening Balance Equity).
        cls._cache_txn_cls("ISV1", "Inventory Starting Value", [
            ("1403", 5000, 0), ("3050", 0, 5000),
        ])

    @classmethod
    def _cache_txn_cls(cls, qbo_id, ttype, lines):
        return cls.env["qbo.journal.cache.transaction"].create({
            "cache_id": cls.cache.id,
            "qbo_txn_id": qbo_id,
            "txn_type": ttype,
            "txn_date": "2026-02-18",
            "line_ids": [(0, 0, {
                "account_code": c, "debit": d, "credit": cr, "memo": "x",
            }) for (c, d, cr) in lines],
        })

    def _cache_txn(self, qbo_id, lines):
        return self._cache_txn_cls(qbo_id, "Invoice", lines)

    def _odoo_move(self, qbo_invoice_id, lines):
        """Create a draft move booking `lines`: (account, debit, credit)."""
        return self.env["account.move"].create({
            "move_type": "entry",
            "qbo_invoice_id": qbo_invoice_id,
            "line_ids": [(0, 0, {
                "account_id": a.id, "debit": d, "credit": c, "name": "x",
            }) for (a, d, c) in lines],
        })

    def _run(self, imported_ids):
        ctx = ETLContext(cr=None, env=self.env)
        maps = build_code_maps(ctx)
        return build_cogs_completion_transactions(
            ctx, self.cache.id, set(imported_ids),
            maps["code_map"], maps["account_type_map"],
        )

    def test_ac1_recovers_pair_and_excludes_fx_penny(self):
        """The dropped 9000/1403 pair is returned; an unrelated 5900 FX
        line QBO booked on the revenue side is excluded so the pair still
        balances exactly."""
        self._cache_txn("22871", [
            ("1100", 700.00, 0),      # AR
            ("4100", 0, 700.02),      # revenue (0.02 more than Odoo booked)
            ("9000", 700.00, 0),      # COGS (dropped)
            ("1403", 0, 700.00),      # inventory relief (dropped)
            ("5900", 0.02, 0),        # FX rounding on the sale (dropped)
        ])
        # Entity import booked AR + revenue (Odoo's rounding = 700.00).
        self._odoo_move("22871", [(self.ar, 700.00, 0), (self.rev, 0, 700.00)])

        result = self._run(["22871"])
        self.assertEqual(len(result), 1)
        codes = sorted(l["account_code"] for l in result[0]["lines"])
        self.assertEqual(codes, ["1403", "9000"])  # 5900 excluded
        net = sum(l["debit"] - l["credit"] for l in result[0]["lines"])
        self.assertAlmostEqual(net, 0.0, places=2)

    def test_ac2_nothing_when_all_accounts_booked(self):
        """If the move already books every cache account, return nothing."""
        self._cache_txn("30000", [
            ("4100", 0, 700), ("1100", 700, 0),
            ("9000", 700, 0), ("1403", 0, 700),
        ])
        self._odoo_move("30000", [
            (self.ar, 700, 0), (self.rev, 0, 700),
            (self.cogs, 700, 0), (self.inv, 0, 700),
        ])
        self.assertEqual(self._run(["30000"]), [])

    def test_ac3_no_cogs_or_inventory_in_scope_not_plugged(self):
        """A balancing missing subset with no COGS/inventory account is
        skipped (never plug a revenue/other pair)."""
        self._cache_txn("30001", [
            ("4100", 0, 700), ("1100", 700, 0),
            ("1500", 500, 0), ("4200", 0, 500),
        ])
        self._odoo_move("30001", [(self.ar, 700, 0), (self.rev, 0, 700)])
        # missing scoped = {} (1500/4200 not COGS or inventory) -> nothing.
        self.assertEqual(self._run(["30001"]), [])

    def test_ac4_unbalanced_scoped_missing_not_plugged(self):
        """A scoped missing subset that doesn't self-balance is never
        plugged (COGS present but its inventory relief already booked)."""
        self._cache_txn("30002", [
            ("1100", 700, 0), ("4100", 0, 700),
            ("9000", 700, 0), ("1403", 0, 700),
        ])
        # Odoo booked AR + the inventory relief (1403) but not COGS (9000).
        self._odoo_move("30002", [(self.ar, 700, 0), (self.inv, 0, 700)])
        # missing scoped = {9000 dr700} alone -> unbalanced -> skip.
        self.assertEqual(self._run(["30002"]), [])
