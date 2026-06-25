"""Tests for the Clearing (1005) suspense-account configuration step.

Acceptance criteria (task 3817):
1. The Clearing account is RESOLVED, not blindly created: an existing 1005
   (or a "Clearing" asset_current) is reused; the "Bank Suspense" account
   (343 / "111312 Bank Suspense Account") is NEVER selected or created. When
   no Clearing account exists at all, one is created (code 1005, asset_current,
   reconcile).
2. The Clearing account is set as the company default suspense account and as
   suspense_account_id on every active, non-"(deleted)" bank journal. Non-bank
   journals and "(deleted)"-named journals are left untouched.
3. The inbound/outbound payment.method.lines of those bank journals are
   repointed to Clearing — including a blanket OVERWRITE of lines that were
   pointing at the bad 343 account. "(deleted)"-journal lines are untouched.
4. "(deleted)"-named bank journals are excluded by name from all of the above.

Plus: idempotency (run twice -> still one 1005 account, values stable) and a
343 -> 1005 overwrite regression.

The configuration step is invoked directly with a minimal ETLContext (no live
QBO feed), exactly as the sibling pipeline tests do.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


@tagged("post_install", "-at_install", "qbo_bank_suspense")
class TestBankJournalSuspense(TransactionCase):
    """Unit tests for QboBankJournalProcessor suspense configuration."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.processor = cls.env["qbo.bank.journal.processor"]
        cls.company = cls.env.company

        Account = cls.env["account.account"]
        Journal = cls.env["account.journal"]
        PMLine = cls.env["account.payment.method.line"]

        # BAD account: the "Bank Suspense" account the migration mistakenly used.
        cls.bad_account = Account.create({
            "name": "111312 Bank Suspense Account",
            "code": "343BAD",
            "account_type": "asset_current",
            "company_ids": [(6, 0, [cls.company.id])],
        })

        # GOOD account: Clearing (1005) — what suspense should resolve to.
        cls.good_account = Account.create({
            "name": "Clearing",
            "code": "1005",
            "account_type": "asset_current",
            "reconcile": True,
            "company_ids": [(6, 0, [cls.company.id])],
        })

        # Two active bank journals, one "(deleted)" bank journal, one non-bank.
        cls.bank_a = Journal.create({
            "name": "Operating Bank A",
            "type": "bank",
            "code": "SUSPA",
            "company_id": cls.company.id,
        })
        cls.bank_b = Journal.create({
            "name": "Operating Bank B",
            "type": "bank",
            "code": "SUSPB",
            "company_id": cls.company.id,
        })
        cls.bank_deleted = Journal.create({
            "name": "Legacy Bank (deleted)",
            "type": "bank",
            "code": "SUSPD",
            "company_id": cls.company.id,
        })
        cls.cash_journal = Journal.create({
            "name": "Petty Cash",
            "type": "cash",
            "code": "SUSPC",
            "company_id": cls.company.id,
        })

        # Pre-point some in/out method lines at the BAD account to exercise the
        # overwrite path. Bank journals auto-create inbound/outbound manual
        # method lines on creation; grab and corrupt them.
        cls.bank_a_lines = PMLine.search([
            ("journal_id", "=", cls.bank_a.id),
            ("payment_type", "in", ("inbound", "outbound")),
        ])
        cls.bank_b_lines = PMLine.search([
            ("journal_id", "=", cls.bank_b.id),
            ("payment_type", "in", ("inbound", "outbound")),
        ])
        cls.deleted_lines = PMLine.search([
            ("journal_id", "=", cls.bank_deleted.id),
            ("payment_type", "in", ("inbound", "outbound")),
        ])

        # Corrupt bank_a's lines: point them at the bad 343-style account.
        if cls.bank_a_lines:
            cls.bank_a_lines.write({"payment_account_id": cls.bad_account.id})
        # Corrupt the deleted journal's lines too, to prove they stay untouched.
        if cls.deleted_lines:
            cls.deleted_lines.write({"payment_account_id": cls.bad_account.id})

    # ---- AC1: resolve-not-create, never 343 ---------------------------------

    def test_ac1_resolves_existing_clearing_not_343(self):
        """AC1: existing 1005 is reused; bad 343 account is never chosen."""
        ctx = _make_ctx(self.env)
        resolved = self.processor._ensure_clearing_suspense_account(ctx)

        self.assertEqual(
            resolved, self.good_account,
            "must resolve to the existing Clearing (1005) account",
        )
        self.assertNotEqual(
            resolved, self.bad_account,
            "must NEVER resolve to the 111312 Bank Suspense account",
        )

    def test_ac1_creates_clearing_when_absent(self):
        """AC1: when no Clearing/1005 exists, a new one is created (not 343)."""
        ctx = _make_ctx(self.env)
        Account = self.env["account.account"].with_context(active_test=False)

        # Rename the good account out of the way so nothing matches.
        self.good_account.write({"code": "1005X", "name": "Renamed"})

        before = Account.search_count([
            ("code", "=", "1005"),
            ("company_ids", "in", [self.company.id]),
        ])
        self.assertEqual(before, 0, "precondition: no 1005 account present")

        resolved = self.processor._ensure_clearing_suspense_account(ctx)

        self.assertEqual(resolved.code, "1005")
        self.assertEqual(resolved.account_type, "asset_current")
        self.assertTrue(resolved.reconcile)
        self.assertNotEqual(
            resolved, self.bad_account,
            "must not adopt the 111312 Bank Suspense account",
        )
        self.assertIn(self.company.id, resolved.company_ids.ids)
        # No restore needed: TransactionCase rolls back to the class savepoint
        # after each test method, so the renamed good_account is reverted.

    # ---- AC2: company default + bank journal suspense ------------------------

    def test_ac2_company_default_and_bank_journals_set(self):
        """AC2: company default + active bank journals set; cash/deleted untouched."""
        ctx = _make_ctx(self.env)

        self.processor._configure_suspense_accounts(ctx)

        for rec in (self.company, self.bank_a, self.bank_b,
                    self.bank_deleted, self.cash_journal):
            rec.invalidate_recordset()

        self.assertEqual(
            self.company.account_journal_suspense_account_id, self.good_account,
            "company default suspense account must be Clearing (1005)",
        )
        self.assertEqual(
            self.bank_a.suspense_account_id, self.good_account,
            "active bank journal A must point at Clearing",
        )
        self.assertEqual(
            self.bank_b.suspense_account_id, self.good_account,
            "active bank journal B must point at Clearing",
        )
        self.assertNotEqual(
            self.bank_deleted.suspense_account_id, self.good_account,
            "'(deleted)' bank journal must NOT be touched",
        )

    # ---- AC3: in/out method lines repointed, incl. 343 overwrite -----------

    def test_ac3_method_lines_repointed_overwriting_343(self):
        """AC3: in/out lines (incl. those on 343) go to 1005; deleted untouched."""
        ctx = _make_ctx(self.env)

        # Sanity: bank_a lines start on the bad account.
        if self.bank_a_lines:
            self.assertTrue(
                all(l.payment_account_id == self.bad_account
                    for l in self.bank_a_lines),
                "precondition: bank_a in/out lines point at the bad 343 account",
            )

        self.processor._configure_suspense_accounts(ctx)

        for line in (self.bank_a_lines | self.bank_b_lines
                     | self.deleted_lines):
            line.invalidate_recordset()

        for line in self.bank_a_lines:
            self.assertEqual(
                line.payment_account_id, self.good_account,
                "bad-343 in/out line must be OVERWRITTEN to Clearing",
            )
        for line in self.bank_b_lines:
            self.assertEqual(
                line.payment_account_id, self.good_account,
                "empty/other in/out line must be set to Clearing",
            )
        for line in self.deleted_lines:
            self.assertNotEqual(
                line.payment_account_id, self.good_account,
                "'(deleted)' journal's in/out lines must NOT be touched",
            )

    # ---- AC4: "(deleted)" exclusion -----------------------------------------

    def test_ac4_deleted_named_journals_excluded(self):
        """AC4: a "(deleted)" bank journal is excluded from suspense config."""
        ctx = _make_ctx(self.env)

        self.processor._configure_suspense_accounts(ctx)
        self.bank_deleted.invalidate_recordset()

        self.assertNotEqual(
            self.bank_deleted.suspense_account_id, self.good_account,
            "the '(deleted)' journal must be excluded by name",
        )

    # ---- Idempotency --------------------------------------------------------

    def test_idempotent_on_rerun(self):
        """Running twice keeps a single 1005 account and stable values."""
        ctx = _make_ctx(self.env)
        Account = self.env["account.account"].with_context(active_test=False)

        self.processor._configure_suspense_accounts(ctx)
        self.processor._configure_suspense_accounts(ctx)

        count = Account.search_count([
            ("code", "=", "1005"),
            ("company_ids", "in", [self.company.id]),
        ])
        self.assertEqual(count, 1, "re-run must not create a second 1005 account")

        for rec in (self.company, self.bank_a, self.bank_b):
            rec.invalidate_recordset()
        self.assertEqual(
            self.company.account_journal_suspense_account_id, self.good_account)
        self.assertEqual(self.bank_a.suspense_account_id, self.good_account)
        self.assertEqual(self.bank_b.suspense_account_id, self.good_account)

    # ---- 343 -> 1005 overwrite regression -----------------------------------

    def test_343_overwrite_regression(self):
        """Regression: NO in/out line on an active bank journal keeps 343."""
        ctx = _make_ctx(self.env)

        self.processor._configure_suspense_accounts(ctx)

        PMLine = self.env["account.payment.method.line"]
        active_bank_ids = (self.bank_a | self.bank_b).ids
        still_bad = PMLine.search([
            ("journal_id", "in", active_bank_ids),
            ("payment_type", "in", ("inbound", "outbound")),
            ("payment_account_id", "=", self.bad_account.id),
        ])
        self.assertFalse(
            still_bad,
            "no active bank journal in/out line may still point at 343",
        )
