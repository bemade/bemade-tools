from odoo import models, fields, api, Command
from odoo.tools.sql import SQL
import logging

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    sap_docnum = fields.Integer(index="btree")
    sap_docentry = fields.Integer(index="btree")
    sap_table = fields.Char(index="btree")


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    sap_line_num = fields.Integer(index="btree")


class InvoiceImporter(models.AbstractModel):
    _name = "sap.invoice.importer"
    _description = "SAP Account Move Importer"

    _products_dict = None
    _accounts_dict = None

    def import_invoices(self, cr):
        already_imported = self.env["account.move"].search(
            [("sap_docnum", "!=", False), ("sap_table", "=", "oinv")]
        )
        currencies_dict = {cur.name: cur for cur in self.env["res.currency"].search([])}

        def _get_currency_id(sap_currency_code):
            return currencies_dict.get(sap_currency_code, currencies_dict["CAD"]).id

        where = ""
        args = []
        if already_imported:
            where = "WHERE docentry not in %s"
            args = [tuple(already_imported.mapped("sap_docentry"))]
        cr.execute(SQL(f"SELECT * FROM oinv WHERE docstatus='O' {where}", *args))
        open_invoices = cr.dictfetchall()

        cr.execute(
            """
        SELECT * FROM inv1 
        WHERE inv1.docentry IN (SELECT docentry FROM oinv WHERE docstatus='O')
        """
        )
        open_lines = cr.dictfetchall()
        partners_dict = {
            partner["sap_card_code"]: partner
            for partner in self.env["res.partner"].search(
                [("sap_card_code", "!=", False)]
            )
        }
        invoice_lines_dict = {}
        for line in open_lines:
            invoice_lines_dict.setdefault(line["docentry"], []).append(line)
        # We are ignoring the paid_to_date field because only one invoice seems to have
        # a partial payment. This can easily be entered manually.

        cr.execute(
            """
            SELECT 
                INV1.DocEntry AS InvoiceDocEntry,
                INV1.LineNum AS InvoiceLineNum,
                RDR1.DocEntry AS SalesOrderDocEntry,
                RDR1.LineNum AS SalesOrderLineNum
            FROM 
                INV1
            INNER JOIN 
                DLN1 
                ON INV1.BaseEntry = DLN1.DocEntry 
                AND INV1.BaseLine = DLN1.LineNum 
                AND INV1.BaseType = 15 -- Deliveries have BaseType = 15
            INNER JOIN 
                RDR1 
                ON DLN1.BaseEntry = RDR1.DocEntry 
                AND DLN1.BaseLine = RDR1.LineNum 
                AND DLN1.BaseType = 17 -- Sales Orders have BaseType = 17
            """
        )
        invoice_sale_rel_lines = cr.fetchall()
        so_lines = self.env["sale.order.line"].search_read(
            [("sap_docentry", "!=", False)], ["id", "sap_docentry", "sap_linenum"]
        )
        so_lines_dict = {
            (line["sap_docentry"], line["sap_linenum"]): line["id"] for line in so_lines
        }
        invoice_line_to_so_id_dict = {
            (row[0], row[1]): so_lines_dict.get((row[2], row[3]))
            for row in invoice_sale_rel_lines
        }
        invoice_vals = []
        _logger.info(f"Creating values for {len(open_invoices)} invoices...")
        for order in open_invoices:
            lines = []
            if order["docentry"] in invoice_lines_dict:
                lines = [
                    Command.create(vals)
                    for vals in self._get_line_vals(
                        invoice_lines_dict[order["docentry"]],
                        invoice_line_to_so_id_dict,
                        order["docduedate"],
                    )
                ]

            vals = {
                "sap_docentry": order["docentry"],
                "sap_docnum": order["docnum"],
                "sap_table": "oinv",
                "invoice_date": order["docdate"],
                "invoice_date_due": order["docduedate"],
                "partner_id": partners_dict[order["cardcode"]].id,
                "currency_id": _get_currency_id(order["doccur"]),
                "line_ids": lines,
                "move_type": "out_invoice",
            }
            invoice_vals.append(vals)
        _logger.info(f"Creating {len(invoice_vals)} invoices...")
        invoices = self.env["account.move"].create(invoice_vals)
        _logger.info(
            f"Created {len(invoices)} invoices with {len(invoices.mapped("line_ids"))}."
        )
        invoices.action_post()

    @api.model
    def _get_line_vals(self, lines, order_lines_dict, due_date):
        vals = []
        for line in lines:
            order_line_id = order_lines_dict.get((line["docentry"], line["linenum"]))
            vals.append(
                {
                    "sap_line_num": line["linenum"],
                    "sequence": line["linenum"],
                    "product_id": self._get_product(line["itemcode"]),  # always set
                    "quantity": line["quantity"],
                    "sale_line_ids": [Command.link(order_line_id)],
                    "price_unit": line["price"],
                    "account_id": self._get_account(line["acctcode"]),
                }
            )
        return vals

    @api.model
    def _get_product(self, itemcode):
        if not self.__class__._products_dict:
            self.__class__._products_dict = {
                product.sap_item_code: product.id
                for product in self.env["product.product"].search(
                    [("sap_item_code", "!=", False)]
                )
            }
        return self.__class__._products_dict.get(itemcode)

    @api.model
    def _get_account(self, itemcode):
        if not self.__class__._accounts_dict:
            self.__class__._accounts_dict = {
                account.code: account.id
                for account in self.env["account.account"].search([])
            }
        return self.__class__._accounts_dict.get(itemcode)
