from odoo import models, fields, api, _
from odoo.sql_db import db_connect
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
            self.env["sap.sale.purchase.importer.mixin"].with_company(
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

    def action_import_sale_stock_pickings(self):
        with self.get_cursor() as cr:
            self.env["sap.stock.picking.importer"].with_company(
                self.env.company
            ).import_sale_pickings(cr)
        return self._success_notification()

    def action_import_purchase_stock_pickings(self):
        with self.get_cursor() as cr:
            self.env["sap.stock.picking.importer"].with_company(
                self.env.company
            ).import_puchase_pickings(cr)
        return self._success_notification()

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

    def _import_all(self):
        self.ensure_one()
        with self.get_cursor() as cr:
            _logger.info("Beginning SAP record import.")
            self.action_import_users()
            self.action_import_partners()
            self.action_import_carrier_accounts()
            self.action_import_products()
            self.action_import_boms()
            self.action_import_payment_terms()
            self.action_import_sales_orders()
            self.action_import_purchase_orders()
            self.action_import_sale_stock_pickings()
            self.action_import_purchase_stock_pickings()
            self.action_import_orderpoints()
            self.action_import_product_pricelist()
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
