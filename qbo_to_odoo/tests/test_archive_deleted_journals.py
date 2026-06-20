"""Tests for the end-of-import 'deleted' journal archival step.

Acceptance criteria (task 3818):
1. At the end of the QBO import, every account.journal whose name contains
   "deleted" is archived (active=False). Matching is case-insensitive, so
   journals named with any casing of "deleted" / "(Deleted)" are archived,
   while a normally-named journal is left untouched.
2. The step runs automatically as part of the import — it is invoked from the
   bank-journal pipeline's load step (QboBankJournalProcessor.load_journals),
   which the orchestrated import runs. The test exercises the cleanup method
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

    def test_ac2_runs_from_load_journals(self):
        """AC2: the cleanup runs automatically via the pipeline load step.

        load_journals with no journals to create still triggers the archival
        cleanup, so an orchestrated import that creates no new bank journals
        still cleans up QBO 'deleted' journals.
        """
        ctx = _make_ctx(self.env)

        self.deleted_lower.with_context(active_test=False).write({"active": True})

        # No 'transform_journals' key -> no creation, but cleanup must still run.
        self.processor.load_journals(ctx, {})

        self.deleted_lower.invalidate_recordset()
        self.assertFalse(
            self.deleted_lower.with_context(active_test=False).active,
            "load_journals must invoke the 'deleted' cleanup even with no new journals",
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
