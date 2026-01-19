import logging
import os
from typing import Optional

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.sql_db import db_connect

from odoo.addons.etl_framework import (
    ETL,
    ETLContext,
    ETLExecutor,
    PipelineOrchestrator,
)

_logger = logging.getLogger(__name__)

PAGE_SIZE = 1000


class XtupleDatabase(models.Model):
    _name = "xtuple.database"
    _description = "xTuple Database"

    database_host = fields.Char(
        required=True, default=lambda self: os.environ.get("XTUPLE_HOST", "")
    )
    database_name = fields.Char(
        required=True, default=lambda self: os.environ.get("XTUPLE_DBNAME", "")
    )
    database_username = fields.Char(
        required=True, default=lambda self: os.environ.get("XTUPLE_USER", "")
    )
    database_password = fields.Char(
        default=lambda self: os.environ.get("XTUPLE_PASSWORD", "")
    )
    database_port = fields.Integer(
        required=True, default=lambda self: int(os.environ.get("XTUPLE_PORT", "5432"))
    )
    database_schema = fields.Char(
        required=True, default=lambda self: os.environ.get("XTUPLE_SCHEMA", "public")
    )
    filestore_path = fields.Char()

    @api.constrains("filestore_path")
    def _constrain_filestore_path(self):
        for record in self:
            if record.filestore_path:
                try:
                    if not os.access(record.filestore_path, os.R_OK):
                        raise ValidationError(
                            _("The provided filestore path is not readable: %s")
                            % record.filestore_path
                        )
                except Exception as e:
                    raise ValidationError(
                        _("Unable to access the filestore path: %s. Error: %s")
                        % (record.filestore_path, str(e))
                    )

    @api.depends("database_host", "database_name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.database_host}/{rec.database_name}"

    @api.model
    def setup_from_env(self):
        """Setup xTuple database from environment variables.

        Called from XML data file on module install/upgrade.

        Environment variables:
        - XTUPLE_HOST: Database host
        - XTUPLE_DBNAME: Database name
        - XTUPLE_USER: Database username
        - XTUPLE_PASSWORD: Database password (optional)
        - XTUPLE_PORT: Database port (default: 5432)
        - XTUPLE_SCHEMA: Database schema (default: public)
        - XTUPLE_FILESTORE_PATH: Filestore path (optional)
        """
        if not os.getenv("XTUPLE_HOST"):
            _logger.info("XTUPLE_HOST not set, skipping xTuple database setup")
            return

        db_host = os.getenv("XTUPLE_HOST")
        db_name = os.getenv("XTUPLE_DBNAME")
        db_user = os.getenv("XTUPLE_USER")
        db_password = os.getenv("XTUPLE_PASSWORD", "")
        db_port = int(os.getenv("XTUPLE_PORT", "5432"))
        db_schema = os.getenv("XTUPLE_SCHEMA", "public")
        filestore_path = os.getenv("XTUPLE_FILESTORE_PATH", "")

        if not all([db_host, db_name, db_user]):
            _logger.warning(
                "Missing required xTuple database environment variables. "
                "Required: XTUPLE_HOST, XTUPLE_DBNAME, XTUPLE_USER"
            )
            return

        _logger.info(f"Creating xtuple.database record for {db_host}/{db_name}")

        existing = self.search(
            [
                ("database_host", "=", db_host),
                ("database_name", "=", db_name),
            ],
            limit=1,
        )

        vals = {
            "database_host": db_host,
            "database_name": db_name,
            "database_username": db_user,
            "database_password": db_password,
            "database_port": db_port,
            "database_schema": db_schema,
            "filestore_path": filestore_path,
        }

        if existing:
            _logger.info(
                f"Updating existing xtuple.database record (ID: {existing.id})"
            )
            existing.write(vals)
            xtuple_db = existing
        else:
            _logger.info("Creating new xtuple.database record")
            xtuple_db = self.create(vals)

        # Run import if XTUPLE_AUTO_IMPORT is enabled
        auto_import = os.getenv("XTUPLE_AUTO_IMPORT", "").lower() in ("1", "true")
        if auto_import:
            _logger.info("XTUPLE_AUTO_IMPORT is enabled, running import_all()")
            xtuple_db._import_all()
            _logger.info("Successfully completed xTuple import")
        else:
            _logger.info(
                "XTUPLE_AUTO_IMPORT not set, skipping auto-import. "
                "Set XTUPLE_AUTO_IMPORT=1 to enable."
            )

    ##################################################################
    # Utility Methods
    ##################################################################

    def get_cursor(self):
        """Get a database cursor for the xTuple database.

        Returns a cursor with the search_path set to the configured schema.
        """
        self.ensure_one()
        if self.database_password:
            uri = (
                "postgresql://{user}:{password}@{host}:{port}/{database}?"
                "options=-c%20search_path%3D{schema}"
            ).format(
                user=self.database_username,
                password=self.database_password,
                host=self.database_host,
                port=self.database_port,
                database=self.database_name,
                schema=self.database_schema,
            )
        else:
            uri = (
                "postgresql://{user}@{host}:{port}/{database}?"
                "options=-c%20search_path%3D{schema}"
            ).format(
                user=self.database_username,
                host=self.database_host,
                port=self.database_port,
                database=self.database_name,
                schema=self.database_schema,
            )

        return db_connect(uri, allow_uri=True).cursor()

    def _get_source_config(self) -> dict:
        """Build source configuration dictionary for ETL framework.

        Returns:
            Dictionary with source-specific configuration values.
        """
        self.ensure_one()
        return {
            "source_id": self.id,
            "source_model": "xtuple.database",
            "filestore_path": self.filestore_path,
        }

    @api.model
    def _success_notification(self) -> dict:
        """Return a success notification action."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Import Successful"),
                "message": _("The xTuple records were successfully imported."),
                "sticky": False,
                "type": "success",
            },
        }

    ##################################################################
    # ETL Pipeline Execution Helpers
    ##################################################################

    def _execute_pipeline(self, pipeline_name: str) -> dict:
        """Execute a single ETL pipeline by name."""
        with self.get_cursor() as cr:
            pipeline = ETL.get_pipeline(pipeline_name)
            if not pipeline:
                raise UserError(_(f"{pipeline_name} ETL pipeline not found"))

            importer = self.env[pipeline_name].with_company(self.env.company)
            ctx = ETLContext(
                cr=cr, env=self.env, source_config=self._get_source_config()
            )
            executor = ETLExecutor(pipeline, ctx, importer)
            executor.execute()
            self.env.cr.commit()

        return self._success_notification()

    def _execute_pipelines(self, pipeline_names: list) -> dict:
        """Execute multiple ETL pipelines using the orchestrator."""
        with self.get_cursor() as cr:
            orchestrator = PipelineOrchestrator(
                self.env, source_config=self._get_source_config()
            )
            orchestrator.execute_pipelines(cr, pipeline_names)
        return self._success_notification()

    ##################################################################
    # Public Action Methods (Called from UI)
    ##################################################################

    def action_import_partners(self) -> dict:
        """Import xTuple partners (customers, vendors, contacts, ship-tos)."""
        return self._execute_pipelines(
            [
                "xtuple.partner.customer.importer",
                "xtuple.partner.vendor.importer",
                "xtuple.partner.standalone.importer",
                "xtuple.partner.contact.importer",
                "xtuple.partner.shipto.importer",
                "xtuple.partner.postprocessor",
            ]
        )

    def action_import_products(self) -> dict:
        """Import xTuple products and product categories."""
        return self._execute_pipelines(
            [
                "xtuple.product.category.importer",
                "xtuple.product.importer",
                "xtuple.product.supplierinfo.importer",
            ]
        )

    def action_import_boms(self) -> dict:
        """Import xTuple bills of materials."""
        return self._execute_pipelines(
            [
                "xtuple.mrp.bom.importer",
                "xtuple.mrp.bom.line.importer",
                "xtuple.mrp.bom.postprocessor",
            ]
        )

    def action_import_all(self) -> dict:
        """Import all xTuple data in the correct order."""
        self._import_all()
        return self._success_notification()

    @api.model
    def import_all(self):
        """Create a database record and import all data."""
        return self.create({}).action_import_all()

    ##################################################################
    # Internal Import Methods
    ##################################################################

    def _import_all(self) -> None:
        """Internal method to import all xTuple data using ETL framework.

        This method uses the PipelineOrchestrator to automatically:
        1. Resolve dependencies between models
        2. Execute pipelines in the correct order
        3. Handle multiprocessing based on data volume
        4. Commit after each pipeline
        """
        self.ensure_one()
        _logger.info("Beginning xTuple record import using ETL framework.")

        with self.get_cursor() as cr:
            orchestrator = PipelineOrchestrator(
                self.env,
                source_config=self._get_source_config(),
                module_filter="xtuple_to_odoo",
            )
            try:
                orchestrator.execute_all(cr)
            except Exception as e:
                _logger.error(f"ETL pipeline execution failed: {e}", exc_info=True)
                raise

        _logger.info("Successfully completed xTuple record import.")
