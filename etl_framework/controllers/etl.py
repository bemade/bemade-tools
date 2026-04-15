import base64
import logging
import pickle
import traceback

from odoo import http
from odoo.http import request

from ..framework import ETLContext, ETLPhase
from ..reporter import ReportLogHandler

_logger = logging.getLogger(__name__)


def _discover_etl_methods(importer_class, phase):
    """Discover ETL methods for a given phase by scanning the model class."""
    methods = []
    for attr_name in dir(importer_class):
        attr = getattr(importer_class, attr_name, None)
        if attr is None:
            continue
        etl_method = getattr(attr, "_etl_method", None)
        if etl_method and etl_method.phase == phase:
            methods.append(attr_name)
    return methods


class ETLController(http.Controller):
    @http.route(
        "/etl/process_chunk",
        type="json2",
        auth="bearer",
        methods=["POST"],
        readonly=False,
    )
    def process_chunk(self, importer_name, chunk, source_config=None):
        """Process a single ETL chunk (transform + load) in this worker.

        Called by the orchestrator's ChunkDispatcher to distribute work
        across Odoo HTTP worker processes.  Each worker has its own DB
        connection and transaction — no fork, no pickle boundary.

        Args:
            importer_name: Odoo model name of the ETL importer.
            chunk: Dict of extracted data for this chunk.
            source_config: Optional source configuration dict.

        Returns:
            Dict with status, success_count, warnings, and failures.
            On error: returns a 500 Response with structured error info.
        """
        try:
            env = request.env
            importer = env[importer_name]
            importer_class = type(importer)

            chunk = pickle.loads(base64.b64decode(chunk))
            ctx = ETLContext(cr=None, env=env, source_config=source_config)

            # Install log handler to capture WARNING+ messages in worker
            log_handler = ReportLogHandler(ctx.report)
            logging.getLogger().addHandler(log_handler)

            transform_methods = _discover_etl_methods(
                importer_class, ETLPhase.TRANSFORM
            )
            load_methods = _discover_etl_methods(importer_class, ETLPhase.LOAD)

            try:
                # Run transform
                transformed_data = {}
                for method_name in transform_methods:
                    bound = getattr(importer, method_name)
                    result = bound(ctx, chunk)
                    transformed_data[method_name] = result

                # Run load
                for method_name in load_methods:
                    bound = getattr(importer, method_name)
                    bound(ctx, transformed_data)
            finally:
                logging.getLogger().removeHandler(log_handler)

            # Collect report details from the detached PipelineReport
            report = ctx.report
            warnings = []
            failures = []
            for detail in report.details:
                entry = {
                    "source_ref": detail.source_ref,
                    "message": detail.message,
                }
                if detail.level == "warning":
                    warnings.append(entry)
                elif detail.level == "failure":
                    failures.append(entry)

            # Odoo auto-commits on successful response
            return {
                "status": "ok",
                "success_count": report.success_count,
                "warnings": warnings,
                "failures": failures,
            }
        except Exception as e:
            _logger.error(
                "ETL chunk processing failed for %s: %s",
                importer_name,
                e,
                exc_info=True,
            )
            # Rollback so the corrupt transaction is not committed.
            request.env.cr.rollback()
            # Return a 500 response with the full traceback.
            # Returning a Response object bypasses json2 dispatcher wrapping.
            return request.make_json_response(
                {
                    "status": "error",
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "traceback": traceback.format_exc(),
                },
                status=500,
            )
