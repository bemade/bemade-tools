from odoo import models, fields, api, _
from odoo.sql_db import db_connect
from odoo.exceptions import ValidationError, UserError
import os
import logging

_logger = logging.getLogger(__name__)
PAGE_SIZE = 1000


class SapDatabase(models.Model):
    _name = "sap.database"
    _description = "SAP Database"

    database_host = fields.Char(required=True)
    database_name = fields.Char(required=True)
    database_username = fields.Char(required=True)
    database_password = fields.Char()
    database_port = fields.Integer(required=True)
    database_schema = fields.Char(required=True)
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
    def _success_notification(self):
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

    def action_init_pricelists(self):
        self.env["sap.sale.order.importer"].with_company(
            self.env.company
        ).init_pricelists()

    def action_import_users(self):
        with self.get_cursor() as cr:
            self.env["res.users.importer"].with_company(
                self.env.company
            ).import_salespeople(cr)
        return self._success_notification()

    def action_import_partners(self):
        with self.get_cursor() as cr:
            self.env["sap.res.partner.importer"].with_company(
                self.env.company
            ).import_partners_concurrent(cr)
        return self._success_notification()

    def action_import_carrier_accounts(self):
        with self.get_cursor() as cr:
            self.env["delivery.carrier.account.importer"].with_company(
                self.env.company
            ).import_all(cr)
        return self._success_notification()

    def action_import_products(self):
        with self.get_cursor() as cr:
            self.env["sap.product.importer"].with_company(
                self.env.company
            ).import_products(cr)
        return self._success_notification()

    def action_import_boms(self):
        with self.get_cursor() as cr:
            self.env["sap.bom.importer"].with_company(self.env.company).import_boms(cr)
        return self._success_notification()

    def action_import_payment_terms(self):
        with self.get_cursor() as cr:
            self.env["sap.res.partner.importer"].with_company(
                self.env.company
            ).import_payment_terms(cr)
        return self._success_notification()

    def action_import_sales_orders(self):
        with self.get_cursor() as cr:
            self.env["sap.sale.order.importer"].with_company(
                self.env.company
            ).import_sales_orders(cr)
        return self._success_notification()

    def action_import_purchase_orders(self):
        with self.get_cursor() as cr:
            self.env["sap.purchase.order.importer"].with_company(
                self.env.company
            ).import_purchase_orders(cr)
        return self._success_notification()

    def action_import_customer_product_codes(self):
        with self.get_cursor() as cr:
            self.env["sap.customer.product.code.importer"].with_company(
                self.env.company
            ).import_customer_product_codes(cr)

    def action_import_product_pricelist(self):
        with self.get_cursor() as cr:
            self.env["sap.product.pricelist.importer"].with_company(
                self.env.company
            ).import_all(cr)
        return self._success_notification()

    def action_import_orderpoints(self):
        with self.get_cursor() as cr:
            self.env["sap.product.importer"].with_company(
                self.env.company
            ).import_orderpoints(cr)
        return self._success_notification()

    def action_import_invoices(self):
        with self.get_cursor() as cr:
            self.env["sap.invoice.importer"].with_company(
                self.env.company
            ).import_invoices(cr)

    def action_import_bills(self):
        with self.get_cursor() as cr:
            self.env["sap.vendor.bill.importer"].with_company(
                self.env.company
            ).import_bills(cr)

    def action_import_attachments(self):
        if not self.filestore_path:
            raise UserError(
                _("No filestore path specified. Cannot import attachments.")
            )
        with self.get_cursor() as cr:
            self.env["sap.ir.attachment.importer"].with_company(
                self.env.company
            ).import_attachments(cr, self.filestore_path)

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

    def action_import_all(self):
        self._import_all()
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

    def action_import_inventory(self):
        with self.get_cursor() as cr:
            self.env["sap.product.importer"].with_company(
                self.env.company
            ).import_inventory(cr)
        return self._success_notification()

    def _import_all(self):
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

    def action_delete_all(self):
        self._delete_all()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Import Successful"),
                "message": _("The SAP records were successfully deleted."),
                "sticky": False,
                "type": "success",
            },
        }

    def _delete_all(self):
        self.ensure_one()
        _logger.info("Deleting all SAP records.")
        self.env["sap.res.partner.importer"]._delete_all()
        self.env["sap.product.importer"]._delete_all()
        self.env["sap.bom.importer"]._delete_all()
        # self.env["sap.sale.order.importer"]._delete_all()
