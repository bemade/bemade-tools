import logging
import os
from typing import Optional

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.sql_db import db_connect

from odoo.addons.sap_b1_to_odoo.etl_framework import (
    ETL,
    ETLContext,
    ETLExecutor,
    PipelineOrchestrator,
)

_logger = logging.getLogger(__name__)

PAGE_SIZE = 1000


class SapDatabase(models.Model):
    """Model to manage SAP Business One database connections and data import."""

    _name = "sap.database"
    _description = "SAP Database"

    database_host = fields.Char(required=True)
    database_name = fields.Char(required=True)
    database_username = fields.Char(required=True)
    database_password = fields.Char()
    database_port = fields.Integer(required=True)
    database_schema = fields.Char(required=True)
    filestore_path = fields.Char()

    ##################################################################
    # Constraints and Computed Fields
    ##################################################################

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
        """
        Setup SAP database from environment variables.
        Called from XML data file on module install/upgrade.

        Environment variables:
        - SAP_DB_HOST: Database host
        - SAP_DB_NAME: Database name
        - SAP_DB_USER: Database username
        - SAP_DB_PASSWORD: Database password (optional)
        - SAP_DB_PORT: Database port (default: 5432)
        - SAP_DB_SCHEMA: Database schema
        - SAP_FILESTORE_PATH: Filestore path (optional)
        - SAP_AUTO_IMPORT: Set to '1' or 'true' to auto-run import_all()
        """
        # Check if we should run the setup
        if not os.getenv("SAP_DB_HOST"):
            _logger.info("SAP_DB_HOST not set, skipping SAP database setup")
            return

        # Gather environment variables
        db_host = os.getenv("SAP_DB_HOST")
        db_name = os.getenv("SAP_DB_NAME")
        db_user = os.getenv("SAP_DB_USER")
        db_password = os.getenv("SAP_DB_PASSWORD", "")
        db_port = int(os.getenv("SAP_DB_PORT", "5432"))
        db_schema = os.getenv("SAP_DB_SCHEMA")
        filestore_path = os.getenv("SAP_FILESTORE_PATH", "")
        auto_import = os.getenv("SAP_AUTO_IMPORT", "").lower() in ("1", "true")

        # Validate required fields
        if not all([db_host, db_name, db_user, db_schema]):
            _logger.warning(
                "Missing required SAP database environment variables. "
                "Required: SAP_DB_HOST, SAP_DB_NAME, SAP_DB_USER, SAP_DB_SCHEMA"
            )
            return

        _logger.info(f"Creating sap.database record for {db_host}/{db_name}")

        # Check if a record already exists
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
            _logger.info(f"Updating existing sap.database record (ID: {existing.id})")
            existing.write(vals)
            sap_db = existing
        else:
            _logger.info("Creating new sap.database record")
            sap_db = self.create(vals)

        if not auto_import:
            _logger.info(
                "SAP database record created. Set SAP_AUTO_IMPORT=1 to auto-run import_all()"
            )

    ##################################################################
    # Public Action Methods (Called from UI)
    ##################################################################

    def _execute_pipeline(self, pipeline_name: str) -> dict:
        """Execute a single ETL pipeline by name."""
        with self.get_cursor() as cr:
            pipeline = ETL.get_pipeline(pipeline_name)
            if not pipeline:
                raise UserError(_(f"{pipeline_name} ETL pipeline not found"))

            importer = self.env[pipeline_name].with_company(self.env.company)
            ctx = ETLContext(cr=cr, env=self.env)
            executor = ETLExecutor(pipeline, ctx, importer)
            executor.execute()
            self.env.cr.commit()

        return self._success_notification()

    def _execute_pipelines(self, pipeline_names: list) -> dict:
        """Execute multiple ETL pipelines using the orchestrator."""
        with self.get_cursor() as cr:
            orchestrator = PipelineOrchestrator(self.env)
            orchestrator.execute_pipelines(cr, pipeline_names)
        return self._success_notification()

    def action_init_pricelists(self) -> dict:
        """Initialize default pricelists for all active currencies."""
        pipeline = ETL.get_pipeline("product.pricelist.importer")
        if not pipeline:
            raise UserError(_("product.pricelist.importer ETL pipeline not found"))

        importer = self.env["product.pricelist.importer"].with_company(self.env.company)
        ctx = ETLContext(cr=None, env=self.env)
        executor = ETLExecutor(pipeline, ctx, importer)
        executor.execute()
        self.env.cr.commit()

        return self._success_notification()

    def action_import_users(self) -> dict:
        """Import SAP salespeople as Odoo users."""
        return self._execute_pipeline("res.users.importer")

    def action_import_partners(self) -> dict:
        """Import SAP business partners (companies, contacts, addresses)."""
        return self._execute_pipelines(
            [
                "res.partner.company.importer",
                "res.partner.contact.importer",
                "res.partner.address.importer",
            ]
        )

    def action_import_carrier_accounts(self) -> dict:
        """Import SAP delivery carriers and carrier accounts."""
        return self._execute_pipeline("delivery.carrier.importer")

    def action_import_products(self) -> dict:
        """Import SAP products and product categories."""
        return self._execute_pipelines(
            [
                "product.category.importer",
                "product.product.importer",
            ]
        )

    def action_import_boms(self) -> dict:
        """Import SAP bills of materials and manufacturing orders."""
        return self._execute_pipelines(
            [
                "mrp.workcenter.importer",
                "mrp.production.importer",
                "mrp.consumption.importer",
                "mrp.workorder.time.updater",
                "mrp.production.postprocess",
            ]
        )

    def action_import_payment_terms(self) -> dict:
        """Import SAP payment terms."""
        return self._execute_pipeline("account.payment.term.importer")

    def action_import_sales_orders(self) -> dict:
        """Import SAP sales orders."""
        return self._execute_pipelines(
            [
                "sale.order.header.importer",
                "sale.order.line.importer",
                "sale.order.text.line.importer",
                "sale.order.post.processor",
            ]
        )

    def action_import_quotations(self) -> dict:
        """Import SAP quotations."""
        return self._execute_pipelines(
            [
                "sale.quotation.header.importer",
                "sale.quotation.line.importer",
                "sale.quotation.text.line.importer",
            ]
        )

    def action_import_purchase_orders(self) -> dict:
        """Import SAP purchase orders."""
        return self._execute_pipelines(
            [
                "purchase.order.header.importer",
                "purchase.order.line.importer",
                "purchase.order.text.line.importer",
                "purchase.order.post.processor",
            ]
        )

    def action_import_purchase_requisitions(self) -> dict:
        """Import SAP purchase requisitions (blanket agreements)."""
        return self._execute_pipeline("purchase.requisition.importer")

    def action_import_product_pricelist(self) -> dict:
        """Import SAP product pricelists."""
        return self._execute_pipeline("product.pricelist.item.importer")

    def action_import_invoices(self) -> dict:
        """Import SAP customer invoices."""
        return self._execute_pipelines(
            [
                "account.move.invoice.importer",
                "account.move.invoice.post.processor",
            ]
        )

    def action_import_bills(self) -> dict:
        """Import SAP vendor bills."""
        return self._execute_pipeline("account.move.bill.importer")

    def action_import_inventory(self) -> dict:
        """Import SAP inventory valuations and stock quantities."""
        return self._execute_pipelines(
            [
                "stock.quant.importer",
                "stock.valuation.layer.importer",
            ]
        )

    def action_import_accounts(self) -> dict:
        """Import SAP chart of accounts."""
        return self._execute_pipelines(
            [
                "account.account.importer",
                "account.journal.setup",
            ]
        )

    def action_import_taxes(self) -> dict:
        """Import SAP tax codes."""
        return self._execute_pipeline("account.tax.importer")

    def action_reconcile_payments(self) -> dict:
        """Reconcile SAP payments with invoices/bills."""
        return self._execute_pipelines(
            [
                "account.payment.reconciliation",
                "account.credit.memo.reconciliation",
                "account.internal.reconciliation",
            ]
        )

    # Legacy methods - no ETL pipeline yet
    def action_import_customer_product_codes(self) -> None:
        """Import SAP customer-specific product codes."""
        with self.get_cursor() as cr:
            self.env["sap.customer.product.code.importer"].with_company(
                self.env.company
            ).import_customer_product_codes(cr)

    def action_import_attachments(self) -> None:
        """Import SAP file attachments."""
        if not self.filestore_path:
            raise UserError(
                _("No filestore path specified. Cannot import attachments.")
            )
        with self.get_cursor() as cr:
            self.env["sap.ir.attachment.importer"].with_company(
                self.env.company
            ).import_attachments(cr, self.filestore_path)

    def action_import_all(self) -> dict:
        """Import all SAP data in the correct order."""
        self._import_all()
        return self._success_notification()

    def action_delete_all(self) -> dict:
        """Delete all SAP-imported records from Odoo.

        Warning: This is a destructive operation.
        """
        self._delete_all()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Deletion Successful"),
                "message": _("The SAP records were successfully deleted."),
                "sticky": False,
                "type": "success",
            },
        }

    ##################################################################
    # Utility Methods
    ##################################################################

    def get_cursor(self):
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
                "postgresql://{user}@/{database}?options=-c%20search_path%3D{schema}"
            ).format(
                user=self.database_username,
                database=self.database_name,
                schema=self.database_schema,
            )

        return db_connect(uri, allow_uri=True).cursor()

    @api.model
    def _success_notification(self) -> dict:
        """Return a success notification action."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Import Successful"),
                "message": _("The SAP records were successfully imported."),
                "sticky": False,
                "type": "success",
            },
        }

    def _import_all(self) -> None:
        """Internal method to import all SAP data using ETL framework.

        This method uses the PipelineOrchestrator to automatically:
        1. Resolve dependencies between models
        2. Execute pipelines in the correct order
        3. Handle multiprocessing based on data volume
        4. Commit after each pipeline
        """
        self.ensure_one()
        _logger.info("Beginning SAP record import using ETL framework.")

        with self.get_cursor() as cr:
            orchestrator = PipelineOrchestrator(self.env)
            try:
                orchestrator.execute_all(cr)
            except Exception as e:
                _logger.error(f"ETL pipeline execution failed: {e}", exc_info=True)
                raise

        _logger.info("Successfully completed SAP record import.")

    def _delete_all(self) -> None:
        """Internal method to delete all SAP-imported records.

        Warning: This is a destructive operation.
        TODO: Implement proper deletion using ETL framework metadata.
        """
        self.ensure_one()
        _logger.info("Deleting all SAP records.")

        # Delete in reverse dependency order
        models_to_delete = [
            "stock.valuation.layer",
            "stock.quant",
            "account.move",
            "sale.order",
            "purchase.order",
            "mrp.production",
            "product.product",
            "product.category",
            "res.partner",
            "res.users",
        ]

        for model_name in models_to_delete:
            try:
                model = self.env[model_name]
                # Find records with SAP identifiers
                sap_field = self._get_sap_identifier_field(model_name)
                if sap_field and sap_field in model._fields:
                    records = model.search([(sap_field, "!=", False)])
                    if records:
                        _logger.info(f"Deleting {len(records)} {model_name} records")
                        records.unlink()
            except Exception as e:
                _logger.warning(f"Could not delete {model_name}: {e}")

    def _get_sap_identifier_field(self, model_name: str) -> Optional[str]:
        """Get the SAP identifier field name for a model."""
        field_mapping = {
            "res.partner": "sap_card_code",
            "res.users": "sap_slpcode",
            "product.product": "sap_item_code",
            "product.category": "sap_itms_grp_cod",
            "sale.order": "sap_doc_entry",
            "purchase.order": "sap_doc_entry",
            "account.move": "sap_doc_entry",
            "mrp.production": "sap_doc_entry",
            "stock.quant": None,  # No direct SAP field
            "stock.valuation.layer": None,  # No direct SAP field
        }
        return field_mapping.get(model_name)
