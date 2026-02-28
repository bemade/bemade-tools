"""Tests for ETL Reporter functionality.

These tests verify:
- PipelineReport accumulates successes, warnings, and failures without raising
- ETLReporter creates and persists report records
- ETLContext.report property returns a usable report (or no-op)
- ETLExecutor auto-tracks extracted counts and uncaught exceptions
- End-to-end reporting through pipeline execution
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.etl_framework import (
    ETL,
    ETLContext,
    ETLExecutor,
    PipelineOrchestrator,
)
from odoo.addons.etl_framework.reporter import ETLReporter, PipelineReport


@tagged("post_install", "-at_install")
class TestPipelineReport(TransactionCase):
    """Test the in-memory PipelineReport dataclass."""

    def test_success_increments_count(self):
        """Calling success() increments the counter."""
        report = PipelineReport(pipeline_name="test")
        report.success()
        report.success(5)
        self.assertEqual(report.success_count, 6)

    def test_warning_appends_detail(self):
        """Calling warning() appends a detail and does not raise."""
        report = PipelineReport(pipeline_name="test")
        report.warning(message="Missing category", source_ref="ITEM-001")
        report.warning(message="Duplicate name")
        self.assertEqual(report.warning_count, 2)
        self.assertEqual(report.failure_count, 0)
        self.assertEqual(report.details[0].source_ref, "ITEM-001")
        self.assertEqual(report.details[0].level, "warning")

    def test_failure_appends_detail_without_raising(self):
        """Calling failure() appends a detail and does NOT raise."""
        report = PipelineReport(pipeline_name="test")
        report.failure(message="Constraint violation", source_ref="DOC-123")
        report.failure(message="Another error")
        self.assertEqual(report.failure_count, 2)
        self.assertEqual(report.warning_count, 0)
        self.assertEqual(report.details[0].source_ref, "DOC-123")
        self.assertEqual(report.details[0].level, "failure")

    def test_mixed_details(self):
        """Warnings and failures coexist correctly."""
        report = PipelineReport(pipeline_name="test")
        report.success(10)
        report.warning(message="w1")
        report.failure(message="f1")
        report.warning(message="w2")
        self.assertEqual(report.success_count, 10)
        self.assertEqual(report.warning_count, 2)
        self.assertEqual(report.failure_count, 1)


@tagged("post_install", "-at_install")
class TestETLContextReport(TransactionCase):
    """Test the ctx.report property."""

    def test_report_without_reporter_returns_noop(self):
        """ctx.report returns a detached PipelineReport when no reporter is set."""
        ctx = ETLContext(cr=None, env=self.env)
        report = ctx.report
        self.assertIsNotNone(report)
        self.assertEqual(report.pipeline_name, "_detached")
        # Should not raise
        report.success()
        report.warning(message="test")
        report.failure(message="test")

    def test_report_with_reporter_returns_current(self):
        """ctx.report returns the active PipelineReport from the reporter."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()
            reporter.start_pipeline(
                pipeline_name="test.pipeline", target_model="res.partner"
            )
            ctx = ETLContext(cr=None, env=self.env, _reporter=reporter)
            report = ctx.report
            self.assertEqual(report.pipeline_name, "test.pipeline")
            report.success(3)
            self.assertEqual(report.success_count, 3)
            reporter.end_pipeline(state="done")
            reporter.end_run(state="done")


@tagged("post_install", "-at_install")
class TestETLReporterPersistence(TransactionCase):
    """Test that ETLReporter persists results to Odoo models."""

    def test_start_run_creates_report(self):
        """start_run() creates an etl.import.report record."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            report = reporter.start_run()
            self.assertTrue(report.exists())
            self.assertEqual(report.state, "running")

    def test_end_run_marks_done(self):
        """end_run(state='done') sets state and end_time."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()
            reporter.end_run(state="done")
            report = reporter._report_record
            self.assertEqual(report.state, "done")
            self.assertTrue(report.end_time)

    def test_end_run_marks_failed(self):
        """end_run(state='failed') sets state to failed."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()
            reporter.end_run(state="failed")
            report = reporter._report_record
            self.assertEqual(report.state, "failed")

    def test_pipeline_creates_line(self):
        """A pipeline start/end cycle creates a report line."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()
            reporter.start_pipeline(
                pipeline_name="test.importer", target_model="product.product"
            )
            reporter.current.extracted_count = 100
            reporter.current.success(95)
            reporter.current.warning(message="Missing field", source_ref="ITEM-1")
            reporter.current.failure(message="Constraint error", source_ref="ITEM-2")
            reporter.end_pipeline(state="done")
            reporter.end_run(state="done")

            report = reporter._report_record
            self.assertEqual(len(report.line_ids), 1)

            line = report.line_ids
            self.assertEqual(line.pipeline_name, "test.importer")
            self.assertEqual(line.target_model, "product.product")
            self.assertEqual(line.extracted_count, 100)
            self.assertEqual(line.success_count, 95)
            self.assertEqual(line.warning_count, 1)
            self.assertEqual(line.failure_count, 1)
            self.assertEqual(line.state, "done")

            # Check details
            self.assertEqual(len(line.detail_ids), 2)
            warning_detail = line.detail_ids.filtered(lambda d: d.level == "warning")
            failure_detail = line.detail_ids.filtered(lambda d: d.level == "failure")
            self.assertEqual(warning_detail.source_ref, "ITEM-1")
            self.assertEqual(failure_detail.source_ref, "ITEM-2")

    def test_multiple_pipelines(self):
        """Multiple pipelines create multiple lines with correct totals."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()

            reporter.start_pipeline(pipeline_name="pipeline.a", target_model="model.a")
            reporter.current.success(10)
            reporter.current.warning(message="w1")
            reporter.end_pipeline(state="done")

            reporter.start_pipeline(pipeline_name="pipeline.b", target_model="model.b")
            reporter.current.success(20)
            reporter.current.failure(message="f1")
            reporter.current.failure(message="f2")
            reporter.end_pipeline(state="done")

            reporter.end_run(state="done")

            report = reporter._report_record
            self.assertEqual(len(report.line_ids), 2)
            self.assertEqual(report.total_success, 30)
            self.assertEqual(report.total_warning, 1)
            self.assertEqual(report.total_failure, 2)


@tagged("post_install", "-at_install")
class TestExecutorReporting(TransactionCase):
    """Test that ETLExecutor auto-tracks reporting."""

    def test_executor_auto_tracks_extracted_count(self):
        """ETLExecutor records extracted_count on the pipeline report."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()

            pipeline = ETL.get_pipeline("test.simple.importer")
            ctx = ETLContext(cr=None, env=self.env, _reporter=reporter)
            importer = self.env["test.simple.importer"]

            executor = ETLExecutor(pipeline, ctx, importer)
            executor.execute()

            reporter.end_run(state="done")

            report = reporter._report_record
            self.assertEqual(len(report.line_ids), 1)
            line = report.line_ids
            self.assertEqual(line.extracted_count, 3)
            self.assertEqual(line.state, "done")

    @mute_logger("odoo.addons.etl_framework.framework")
    def test_executor_records_uncaught_exception(self):
        """ETLExecutor records uncaught exceptions as failures.

        We capture the in-memory PipelineReport via a patched end_pipeline
        because TransactionCase rolls back the savepoint on exception,
        which undoes DB records created in the executor's finally block.
        """
        captured_reports = []
        original_end = ETLReporter.end_pipeline

        def capturing_end(reporter_self, state="done"):
            captured_reports.append((reporter_self.current, state))
            original_end(reporter_self, state=state)

        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()

            pipeline = ETL.get_pipeline("test.failing.importer")
            ctx = ETLContext(cr=None, env=self.env, _reporter=reporter)
            importer = self.env["test.failing.importer"]

            executor = ETLExecutor(pipeline, ctx, importer)
            with patch.object(ETLReporter, "end_pipeline", capturing_end):
                with self.assertRaises(RuntimeError):
                    executor.execute()

            # Verify the in-memory report captured the failure
            self.assertEqual(len(captured_reports), 1)
            in_memory_report, state = captured_reports[0]
            self.assertEqual(state, "failed")
            self.assertEqual(in_memory_report.failure_count, 1)
            self.assertIn(
                "Intentional test failure", in_memory_report.details[0].message
            )

    def test_pipeline_reports_via_ctx(self):
        """Pipeline code can use ctx.report to log successes/warnings/failures."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()

            pipeline = ETL.get_pipeline("test.reporting.importer")
            ctx = ETLContext(cr=None, env=self.env, _reporter=reporter)
            importer = self.env["test.reporting.importer"]

            executor = ETLExecutor(pipeline, ctx, importer)
            executor.execute()

            reporter.end_run(state="done")

            report = reporter._report_record
            line = report.line_ids
            self.assertEqual(line.success_count, 2)
            self.assertEqual(line.warning_count, 1)
            self.assertEqual(line.failure_count, 1)
            # Pipeline should NOT have raised despite the failure
            self.assertEqual(line.state, "done")

    @mute_logger("odoo.addons.test_etl_framework.models.test_importers")
    def test_logged_warnings_and_errors_auto_captured(self):
        """_logger.warning() and _logger.error() in pipelines are auto-captured."""
        with patch.object(self.env.cr, "commit", lambda: None):
            reporter = ETLReporter(self.env)
            reporter.start_run()

            pipeline = ETL.get_pipeline("test.logging.importer")
            ctx = ETLContext(cr=None, env=self.env, _reporter=reporter)
            importer = self.env["test.logging.importer"]

            executor = ETLExecutor(pipeline, ctx, importer)
            executor.execute()

            reporter.end_run(state="done")

            report = reporter._report_record
            line = report.line_ids
            # 1 warning from _logger.warning, 1 failure from _logger.error
            self.assertEqual(line.warning_count, 1)
            self.assertEqual(line.failure_count, 1)

            warning_detail = line.detail_ids.filtered(lambda d: d.level == "warning")
            failure_detail = line.detail_ids.filtered(lambda d: d.level == "failure")
            self.assertIn("Suspicious record", warning_detail.message)
            self.assertIn("(logged:", warning_detail.source_ref)
            self.assertIn("Bad record", failure_detail.message)
            self.assertIn("(logged:", failure_detail.source_ref)
