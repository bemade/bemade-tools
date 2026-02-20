"""ETL Pipeline Reporter API.

Provides a lightweight API for ETL pipelines to report successes, warnings,
and failures during execution. The reporter accumulates results in memory
and flushes them to persistent Odoo models (etl.import.report.*) at the
end of each pipeline or orchestration run.

Usage in pipelines::

    @ETL.load()
    def load_products(self, ctx, transformed):
        for vals in transformed['transform_products']:
            try:
                ctx.env['product.product'].create(vals)
                ctx.report.success()
            except Exception as e:
                ctx.report.failure(
                    source_ref=vals.get('sap_item_code'),
                    message=str(e),
                )

    # Warnings for non-fatal issues:
    ctx.report.warning(
        source_ref='ITEM-001',
        message='Missing category, using default',
    )
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

_logger = logging.getLogger(__name__)


@dataclass
class ReportDetail:
    """A single warning or failure detail."""

    level: str  # 'warning' or 'failure'
    source_ref: Optional[str] = None
    message: str = ""


@dataclass
class PipelineReport:
    """Accumulated results for a single pipeline execution."""

    pipeline_name: str
    target_model: str = ""
    extracted_count: int = 0
    success_count: int = 0
    details: List[ReportDetail] = field(default_factory=list)
    _start_time: float = field(default_factory=time.time)

    def success(self, count: int = 1):
        """Record successful record(s).

        Args:
            count: Number of records successfully processed.
        """
        self.success_count += count

    def warning(self, message: str, source_ref: Optional[str] = None):
        """Record a warning for a record.

        Args:
            message: Description of the warning.
            source_ref: Identifier of the source record (e.g. SAP doc number).
        """
        self.details.append(
            ReportDetail(level="warning", source_ref=source_ref, message=message)
        )

    def failure(self, message: str, source_ref: Optional[str] = None):
        """Record a failure for a record.

        Args:
            message: Description of the failure.
            source_ref: Identifier of the source record.
        """
        self.details.append(
            ReportDetail(level="failure", source_ref=source_ref, message=message)
        )

    @property
    def warning_count(self) -> int:
        return sum(1 for d in self.details if d.level == "warning")

    @property
    def failure_count(self) -> int:
        return sum(1 for d in self.details if d.level == "failure")


class ReportLogHandler(logging.Handler):
    """Logging handler that captures WARNING+ messages into a PipelineReport.

    Installed temporarily during pipeline execution to auto-capture logged
    warnings and errors without requiring any changes to existing pipelines.

    WARNING-level log messages become report warnings.
    ERROR/CRITICAL-level log messages become report failures.
    """

    def __init__(self, report: PipelineReport):
        super().__init__(level=logging.WARNING)
        self._report = report

    _FRAMEWORK_PREFIX = "odoo.addons.etl_framework"

    def emit(self, record: logging.LogRecord):
        # Skip all etl_framework-internal log messages (reporter, executor, retries…)
        if record.name.startswith(self._FRAMEWORK_PREFIX):
            return
        msg = self.format(record)
        if record.levelno >= logging.ERROR:
            self._report.failure(message=msg, source_ref=f"(logged:{record.name})")
        else:
            self._report.warning(message=msg, source_ref=f"(logged:{record.name})")


class ETLReporter:
    """Manages reporting for an entire ETL run (one or more pipelines).

    Created by the orchestrator or executor, attached to ETLContext.
    Persists results to etl.import.report models.
    """

    def __init__(self, env: Any):
        """Initialize the reporter.

        Args:
            env: Odoo environment for creating report records.
        """
        self.env = env
        self._report_record = None
        self._current_pipeline: Optional[PipelineReport] = None
        self._pipeline_sequence = 0

    def start_run(self):
        """Start a new import run. Creates the top-level report record."""
        self._report_record = self.env["etl.import.report"].create({})
        self.env.cr.commit()
        _logger.info("ETL Report created: ID %s", self._report_record.id)
        return self._report_record

    def start_pipeline(self, pipeline_name: str, target_model: str = ""):
        """Start tracking a new pipeline within the current run.

        Args:
            pipeline_name: Importer model name.
            target_model: Target Odoo model name.
        """
        self._pipeline_sequence += 10
        self._current_pipeline = PipelineReport(
            pipeline_name=pipeline_name,
            target_model=target_model,
        )

    def end_pipeline(self, state: str = "done"):
        """Finalize the current pipeline and persist its results.

        Args:
            state: Final state ('done' or 'failed').
        """
        if not self._current_pipeline or not self._report_record:
            return

        pr = self._current_pipeline
        from odoo import fields as odoo_fields

        line_vals = {
            "report_id": self._report_record.id,
            "sequence": self._pipeline_sequence,
            "pipeline_name": pr.pipeline_name,
            "target_model": pr.target_model,
            "state": state,
            "start_time": odoo_fields.Datetime.to_string(
                odoo_fields.Datetime.from_string(
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(pr._start_time))
                )
            ),
            "end_time": odoo_fields.Datetime.now(),
            "extracted_count": pr.extracted_count,
            "success_count": pr.success_count,
        }

        line = self.env["etl.import.report.line"].create(line_vals)

        # Create detail records in batch
        if pr.details:
            detail_vals = [
                {
                    "line_id": line.id,
                    "level": d.level,
                    "source_ref": d.source_ref or False,
                    "message": d.message,
                }
                for d in pr.details
            ]
            self.env["etl.import.report.detail"].create(detail_vals)

        self.env.cr.commit()
        self._current_pipeline = None

        _logger.info(
            "Pipeline %s: %d success, %d warning, %d failure",
            pr.pipeline_name,
            pr.success_count,
            pr.warning_count,
            pr.failure_count,
        )

    def end_run(self, state: str = "done"):
        """Finalize the import run.

        Args:
            state: Final state ('done' or 'failed').
        """
        if not self._report_record:
            return

        if state == "done":
            self._report_record.action_mark_done()
        else:
            self._report_record.action_mark_failed()
        self.env.cr.commit()

    @property
    def current(self) -> Optional[PipelineReport]:
        """Get the current pipeline report for use by pipeline code."""
        return self._current_pipeline
