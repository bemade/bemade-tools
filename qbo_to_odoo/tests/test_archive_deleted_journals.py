"""Tests for the end-of-import 'deleted' journal archival step.

Acceptance criteria (task 3818, AC2 revised when the archival moved to the
terminal ``qbo.journal.finalizer`` pipeline):
1. At the end of the QBO import, every account.journal whose name contains
   "deleted" is archived (active=False). Matching is case-insensitive, so
   journals named with any casing of "deleted" / "(Deleted)" are archived,
   while a normally-named journal is left untouched.
2. The archival runs from the terminal ``qbo.journal.finalizer`` load step —
   and must NOT run from the bank-journal pipeline's ``load_journals``: that
   pipeline runs before the move-posting pipelines, and archiving there makes
   posts into the deleted journals fail ("cannot post an entry in an archived
   journal"), stranding moves in draft. The tests exercise both load steps
   directly (no live QBO connection needed), as the existing pipeline tests do.
3. Idempotent — invoking the cleanup a second time does not error and leaves the
   deleted-named journals archived.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


@tagged("post_install", "-at_install", "archive_deleted_journals")
class TestArchiveDeletedJournals(TransactionCase):
    """Unit tests for QboBankJournalProcessor._archive_deleted_journals."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.processor = cls.env["qbo.bank.journal.processor"]

        Journal = cls.env["account.journal"]
        # Two "deleted"-named journals with differing case + a normal one.
        cls.deleted_lower = Journal.create({
            "name": "Old Chequing (deleted)",
            "type": "bank",
            "code": "DEL01",
        })
        cls.deleted_upper = Journal.create({
            "name": "Legacy Savings DELETED",
            "type": "bank",
            "code": "DEL02",
        })
        cls.normal = Journal.create({
            "name": "Operating Bank",
            "type": "bank",
            "code": "OPER1",
        })

    def test_ac1_archives_deleted_named_journals_case_insensitive(self):
        """AC1: 'deleted' journals (any case) archived; normal journal untouched."""
        ctx = _make_ctx(self.env)

        self.assertTrue(self.deleted_lower.active)
        self.assertTrue(self.deleted_upper.active)
        self.assertTrue(self.normal.active)

        self.processor._archive_deleted_journals(ctx)

        for rec in (self.deleted_lower, self.deleted_upper, self.normal):
            rec.invalidate_recordset()

        self.assertFalse(
            self.deleted_lower.with_context(active_test=False).active,
            "lowercase '(deleted)' journal must be archived",
        )
        self.assertFalse(
            self.deleted_upper.with_context(active_test=False).active,
            "uppercase 'DELETED' journal must be archived (case-insensitive match)",
        )
        self.assertTrue(
            self.normal.active,
            "a normally-named journal must NOT be archived",
        )

    def test_ac2_runs_from_finalizer_not_load_journals(self):
        """AC2: archival fires from the terminal finalizer, NOT journal creation.

        The bank-journal pipeline runs before the move-posting pipelines, so
        archiving there breaks posting into the deleted journals (moves left
        in draft). The cleanup must therefore be a no-op in load_journals and
        run from qbo.journal.finalizer's load step instead.
        """
        ctx = _make_ctx(self.env)

        self.deleted_lower.with_context(active_test=False).write({"active": True})

        # Journal creation must NOT archive — downstream pipelines still need
        # to post into the deleted journals.
        self.processor.load_journals(ctx, {})
        self.deleted_lower.invalidate_recordset()
        self.assertTrue(
            self.deleted_lower.with_context(active_test=False).active,
            "load_journals must NOT archive 'deleted' journals (posting into "
            "them happens later in the import)",
        )

        # The terminal finalizer is what archives.
        self.env["qbo.journal.finalizer"].archive_deleted_journals(ctx, {})
        self.deleted_lower.invalidate_recordset()
        self.assertFalse(
            self.deleted_lower.with_context(active_test=False).active,
            "qbo.journal.finalizer must archive 'deleted' journals",
        )

    def test_ac3_idempotent_on_rerun(self):
        """AC3: running the cleanup twice does not error and stays archived."""
        ctx = _make_ctx(self.env)

        # First run archives.
        self.processor._archive_deleted_journals(ctx)
        # Second run must be a no-op (already archived are excluded by the domain).
        self.processor._archive_deleted_journals(ctx)

        for rec in (self.deleted_lower, self.deleted_upper):
            rec.invalidate_recordset()

        self.assertFalse(
            self.deleted_lower.with_context(active_test=False).active,
            "deleted journal must remain archived after a re-run",
        )
        self.assertFalse(
            self.deleted_upper.with_context(active_test=False).active,
            "deleted journal must remain archived after a re-run",
        )
        self.assertTrue(
            self.normal.active,
            "normal journal must remain active across re-runs",
        )
