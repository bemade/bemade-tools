import logging
import os
from typing import Optional

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.sql_db import db_connect

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
        existing = self.search([
            ("database_host", "=", db_host),
            ("database_name", "=", db_name),
        ], limit=1)

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

        # Run import_all if requested
        if auto_import:
            _logger.info("SAP_AUTO_IMPORT is enabled, running import_all()")
            try:
                sap_db._import_all()
                _logger.info("Successfully completed SAP import")
            except Exception as e:
                _logger.error(f"Error during SAP import: {e}", exc_info=True)
        else:
            _logger.info(
                "SAP database record created. Set SAP_AUTO_IMPORT=1 to auto-run import_all()"
            )

    ##################################################################
    # Public Action Methods (Called from UI)
    ##################################################################

    def action_init_pricelists(self) -> None:
        """Initialize default pricelists for all active currencies."""
        self.env["sap.sale.order.importer"].with_company(
            self.env.company
        ).init_pricelists()

    def action_import_users(self) -> dict:
        """Import SAP salespeople as Odoo users."""
        with self.get_cursor() as cr:
            self.env["res.users.importer"].with_company(
                self.env.company
            ).import_salespeople(cr)
        return self._success_notification()

    def action_import_partners(self) -> dict:
        """Import SAP business partners (companies, contacts, addresses)."""
        with self.get_cursor() as cr:
            self.env["sap.res.partner.importer"].with_company(
                self.env.company
            ).import_partners_concurrent(cr)
        return self._success_notification()

    def action_import_carrier_accounts(self) -> dict:
        """Import SAP delivery carriers and carrier accounts."""
        with self.get_cursor() as cr:
            self.env["delivery.carrier.account.importer"].with_company(
                self.env.company
            ).import_all(cr)
        return self._success_notification()

    def action_import_products(self) -> dict:
        """Import SAP products and product categories."""
        with self.get_cursor() as cr:
            self.env["sap.product.importer"].with_company(
                self.env.company
            ).import_products(cr)
        return self._success_notification()

    def action_import_boms(self) -> dict:
        """Import SAP bills of materials."""
        with self.get_cursor() as cr:
            self.env["sap.bom.importer"].with_company(self.env.company).import_boms(cr)
        return self._success_notification()

    def action_import_payment_terms(self) -> dict:
        """Import SAP payment terms."""
        with self.get_cursor() as cr:
            self.env["sap.res.partner.importer"].with_company(
                self.env.company
            ).import_payment_terms(cr)
        return self._success_notification()

    def action_import_sales_orders(self) -> dict:
        """Import SAP sales orders."""
        with self.get_cursor() as cr:
            self.env["sap.sale.order.importer"].with_company(
                self.env.company
            ).import_sales_orders(cr)
        return self._success_notification()

    def action_import_quotations(self) -> dict:
        """Import SAP quotations."""
        with self.get_cursor() as cr:
            self.env["sap.sale.quotation.importer"].with_company(
                self.env.company
            ).import_quotations(cr)
        return self._success_notification()

    def action_import_purchase_orders(self) -> dict:
        """Import SAP purchase orders."""
        with self.get_cursor() as cr:
            self.env["sap.purchase.order.importer"].with_company(
                self.env.company
            ).import_purchase_orders(cr)
        return self._success_notification()

    def action_import_customer_product_codes(self) -> None:
        """Import SAP customer-specific product codes."""
        with self.get_cursor() as cr:
            self.env["sap.customer.product.code.importer"].with_company(
                self.env.company
            ).import_customer_product_codes(cr)

    def action_import_product_pricelist(self) -> dict:
        """Import SAP product pricelists."""
        with self.get_cursor() as cr:
            self.env["sap.product.pricelist.importer"].with_company(
                self.env.company
            ).import_all(cr)
        return self._success_notification()

    def action_import_orderpoints(self) -> dict:
        """Import SAP stock reordering rules."""
        with self.get_cursor() as cr:
            self.env["sap.product.importer"].with_company(
                self.env.company
            ).import_orderpoints(cr)
        return self._success_notification()

    def action_import_invoices(self) -> None:
        """Import SAP customer invoices."""
        with self.get_cursor() as cr:
            self.env["sap.invoice.importer"].with_company(
                self.env.company
            ).import_invoices(cr)

    def action_import_bills(self) -> None:
        """Import SAP vendor bills."""
        with self.get_cursor() as cr:
            self.env["sap.vendor.bill.importer"].with_company(
                self.env.company
            ).import_bills(cr)

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

    def action_import_inventory(self) -> dict:
        """Import SAP inventory valuations and stock quantities."""
        with self.get_cursor() as cr:
            self.env["sap.product.importer"].with_company(
                self.env.company
            ).import_inventory(cr)
        return self._success_notification()

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
        """Internal method to import all SAP data in the correct sequence."""
        self.ensure_one()
        _logger.info("Beginning SAP record import.")
        self.action_import_users()
        self.env.cr.commit()
        self.action_init_pricelists()
        self.env.cr.commit()
        self.action_import_partners()
        self.env.cr.commit()
        self.action_import_products()
        self.env.cr.commit()
        self.action_import_boms()
        self.env.cr.commit()
        self.action_import_carrier_accounts()
        self.env.cr.commit()
        self.action_import_inventory()
        self.env.cr.commit()
        self.action_import_product_pricelist()
        self.env.cr.commit()
        self.action_import_payment_terms()
        self.env.cr.commit()
        self.action_import_sales_orders()
        self.env.cr.commit()
        self.action_import_purchase_orders()
        self.env.cr.commit()
        _logger.info("Successfully completed SAP record import.")

    def _delete_all(self) -> None:
        """Internal method to delete all SAP-imported records.
        
        Warning: This is a destructive operation.
        """
        self.ensure_one()
        _logger.info("Deleting all SAP records.")
        self.env["sap.res.partner.importer"]._delete_all()
        self.env["sap.product.importer"]._delete_all()
        self.env["sap.bom.importer"]._delete_all()
        # self.env["sap.sale.order.importer"]._delete_all()
