import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class ETLImportReport(models.Model):
    """Top-level report for an ETL import run."""

    _name = "etl.import.report"
    _description = "ETL Import Report"
    _order = "create_date desc"

    name = fields.Char(compute="_compute_name", store=True)
    state = fields.Selection(
        [
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="running",
        readonly=True,
    )
    start_time = fields.Datetime(default=fields.Datetime.now, readonly=True)
    end_time = fields.Datetime(readonly=True)
    duration = fields.Float(
        compute="_compute_duration", store=True, string="Duration (s)"
    )
    line_ids = fields.One2many(
        "etl.import.report.line", "report_id", string="Pipeline Results"
    )
    total_success = fields.Integer(compute="_compute_totals", store=True)
    total_warning = fields.Integer(compute="_compute_totals", store=True)
    total_failure = fields.Integer(compute="_compute_totals", store=True)

    @api.depends("start_time")
    def _compute_name(self):
        for rec in self:
            if rec.start_time:
                rec.name = _("ETL Import — %s") % fields.Datetime.to_string(
                    rec.start_time
                )
            else:
                rec.name = _("ETL Import (draft)")

    @api.depends("start_time", "end_time")
    def _compute_duration(self):
        for rec in self:
            if rec.start_time and rec.end_time:
                delta = rec.end_time - rec.start_time
                rec.duration = delta.total_seconds()
            else:
                rec.duration = 0.0

    @api.depends("line_ids.success_count", "line_ids.warning_count", "line_ids.failure_count")
    def _compute_totals(self):
        for rec in self:
            rec.total_success = sum(rec.line_ids.mapped("success_count"))
            rec.total_warning = sum(rec.line_ids.mapped("warning_count"))
            rec.total_failure = sum(rec.line_ids.mapped("failure_count"))

    def action_mark_done(self):
        self.write({"state": "done", "end_time": fields.Datetime.now()})

    def action_mark_failed(self):
        self.write({"state": "failed", "end_time": fields.Datetime.now()})


class ETLImportReportLine(models.Model):
    """Per-pipeline results within an import report."""

    _name = "etl.import.report.line"
    _description = "ETL Import Report Line"
    _order = "sequence, id"

    report_id = fields.Many2one(
        "etl.import.report", required=True, ondelete="cascade"
    )
    sequence = fields.Integer(default=10)
    pipeline_name = fields.Char(required=True, string="Pipeline")
    target_model = fields.Char(string="Target Model")
    state = fields.Selection(
        [
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="running",
        readonly=True,
    )
    start_time = fields.Datetime(readonly=True)
    end_time = fields.Datetime(readonly=True)
    duration = fields.Float(
        compute="_compute_duration", store=True, string="Duration (s)"
    )
    extracted_count = fields.Integer(string="Extracted", readonly=True)
    success_count = fields.Integer(string="Successes", readonly=True)
    warning_count = fields.Integer(
        compute="_compute_detail_counts", store=True, string="Warnings"
    )
    failure_count = fields.Integer(
        compute="_compute_detail_counts", store=True, string="Failures"
    )
    detail_ids = fields.One2many(
        "etl.import.report.detail", "line_id", string="Details"
    )

    @api.depends("start_time", "end_time")
    def _compute_duration(self):
        for rec in self:
            if rec.start_time and rec.end_time:
                delta = rec.end_time - rec.start_time
                rec.duration = delta.total_seconds()
            else:
                rec.duration = 0.0

    @api.depends("detail_ids.level")
    def _compute_detail_counts(self):
        for rec in self:
            details = rec.detail_ids
            rec.warning_count = len(details.filtered(lambda d: d.level == "warning"))
            rec.failure_count = len(details.filtered(lambda d: d.level == "failure"))


class ETLImportReportDetail(models.Model):
    """Individual warning or failure detail within a pipeline report line."""

    _name = "etl.import.report.detail"
    _description = "ETL Import Report Detail"
    _order = "id"

    line_id = fields.Many2one(
        "etl.import.report.line", required=True, ondelete="cascade"
    )
    level = fields.Selection(
        [
            ("warning", "Warning"),
            ("failure", "Failure"),
        ],
        required=True,
    )
    source_ref = fields.Char(
        string="Source Record",
        help="Identifier of the source record (e.g. SAP document number)",
    )
    message = fields.Text(required=True)
