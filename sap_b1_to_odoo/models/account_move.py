from docutils.nodes import bullet_list

from odoo import models, fields, api, Command
from odoo.tools.sql import SQL
import logging

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    sap_docnum = fields.Integer(index="btree")
    sap_docentry = fields.Integer(index="btree")
    sap_table = fields.Char(index="btree")
    sap_atcentry = fields.Integer(index="btree")


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    sap_line_num = fields.Integer(index="btree")
    sap_aftlinenum = fields.Integer(index="btree")
    sap_lineseq = fields.Integer(index="btree")


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
        ORDER BY inv1.docentry, inv1.linenum
        """
        )
        open_lines = cr.dictfetchall()

        # Fetch text lines from INV10
        cr.execute(
            """
            SELECT 
                docentry,
                aftlinenum,
                lineseq,
                ordernum,
                linetext
            FROM inv10
            WHERE inv10.docentry IN (SELECT docentry FROM oinv WHERE docstatus='O')
            ORDER BY aftlinenum, lineseq
            """
        )
        text_lines = cr.dictfetchall()
        # Add text lines to open_lines
        for line in text_lines:
            # INV10 uses aftlinenum to specify which line to insert after
            # and lineseq to order multiple text lines at the same position
            line["sap_line_num"] = None  # Text lines don't have a line_num
            line["sap_aftlinenum"] = line[
                "aftlinenum"
            ]  # Store which line to insert after
            line["sap_lineseq"] = line["lineseq"]  # Store sequence within position
            open_lines.append(line)

        # Sort all lines by their position and sequence
        open_lines.sort(
            key=lambda x: (
                (
                    x.get("linenum")
                    if x.get("linenum") is not None
                    else x.get("aftlinenum")
                ),
                x.get("lineseq") or 0,
            )
        )

        partners_dict = {
            partner["sap_card_code"]: partner
            for partner in self.env["res.partner"].search(
                [("sap_card_code", "!=", False)]
            )
        }
        invoice_lines_dict = {}
        for line in open_lines:
            invoice_lines_dict.setdefault(line["docentry"], []).append(line)

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
        # Only get product lines (where sap_line_num is set)
        so_lines = self.env["sale.order.line"].search_read(
            [
                ("sap_docentry", "!=", False),
                ("sap_line_num", "!=", False),
                ("sap_table", "=", "rdr1"),
            ],
            ["id", "sap_docentry", "sap_line_num"],
        )
        so_lines_dict = {
            (line["sap_docentry"], line["sap_line_num"]): line["id"]
            for line in so_lines
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
                "sap_atcentry": order["atcentry"],
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
        # First sort lines by their position and sequence
        sorted_lines = sorted(
            lines,
            key=lambda x: (
                (
                    x.get("linenum")
                    if x.get("linenum") is not None
                    else x.get("aftlinenum")
                ),
                x.get("lineseq") or 0,
            ),
        )

        for i, line in enumerate(sorted_lines, 1):
            # Only try to link product lines (where linenum is set)
            order_line_id = None
            if line.get("linenum"):
                order_line_id = order_lines_dict.get(
                    (line["docentry"], line["linenum"])
                )

            quantity = line["quantity"]
            unit_price = line["price"]
            line_total = line["linetotal"]
            if line_total and not quantity:
                if unit_price:
                    quantity = round(line_total / unit_price)
                else:
                    quantity = 1

            line_vals = {
                "sequence": i
                * 100,  # Use incremental sequence based on sorted position
                "quantity": quantity,
                "price_unit": line["price"],
            }

            # Handle product lines vs text lines
            if line.get("linenum"):  # Product line
                line_vals.update(
                    {
                        "sap_line_num": line["linenum"],
                        "product_id": self._get_product(line["itemcode"]),
                        "account_id": self._get_account(line["acctcode"]),
                    }
                )
                if order_line_id:
                    line_vals["sale_line_ids"] = [Command.link(order_line_id)]
            else:  # Text line
                line_vals.update(
                    {
                        "display_type": "line_note",
                        "name": line["linetext"],
                        "sap_aftlinenum": line["aftlinenum"],
                        "sap_lineseq": line["lineseq"],
                    }
                )

            vals.append(line_vals)
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


class VendorBillsImporter(models.AbstractModel):
    _name = "sap.purchase.invoice.importer"
    _description = "SAP Vendor Bill Importer"

    _products_dict = None
    _accounts_dict = None

    def import_bills(self, cr):
        already_imported = self.env["account.move"].search(
            [("sap_docnum", "!=", False), ("sap_table", "=", "opch")]
        )
        currencies_dict = {cur.name: cur for cur in self.env["res.currency"].search([])}

        def _get_currency_id(sap_currency_code):
            return currencies_dict.get(sap_currency_code, currencies_dict["CAD"]).id

        where = ""
        args = []
        if already_imported:
            where = "WHERE docentry not in %s"
            args = [tuple(already_imported.mapped("sap_docentry"))]
        cr.execute(SQL(f"SELECT * FROM opch WHERE docstatus='O' {where}", *args))
        open_bills = cr.dictfetchall()

        cr.execute(
            """
        SELECT * FROM pch1 
        WHERE pch1.docentry IN (SELECT docentry FROM opch WHERE docstatus='O')
        ORDER BY pch1.docentry, pch1.linenum
        """
        )
        open_lines = cr.dictfetchall()

        # Fetch text lines from PCH10
        cr.execute(
            """
            SELECT * FROM pch10
            WHERE pch10.docentry IN (SELECT docentry FROM opch WHERE docstatus='O')
            ORDER BY aftlinenum, lineseq
            """
        )
        text_lines = cr.dictfetchall()
        # Add text lines to open_lines
        for line in text_lines:
            # PCH10 uses aftlinenum to specify which line to insert after
            # and lineseq to order multiple text lines at the same position
            line["sap_line_num"] = None  # Text lines don't have a line_num
            line["sap_aftlinenum"] = line[
                "aftlinenum"
            ]  # Store which line to insert after
            line["sap_lineseq"] = line["lineseq"]  # Store sequence within position
            open_lines.append(line)

        # Sort all lines by their position and sequence
        open_lines.sort(
            key=lambda x: (
                (
                    x.get("linenum")
                    if x.get("linenum") is not None
                    else x.get("aftlinenum")
                ),
                x.get("lineseq") or 0,
            )
        )

        partners_dict = {
            partner["sap_card_code"]: partner
            for partner in self.env["res.partner"].search(
                [("sap_card_code", "!=", False)]
            )
        }
        bill_lines_dict = {}
        for line in open_lines:
            bill_lines_dict.setdefault(line["docentry"], []).append(line)
        # We are ignoring the paid_to_date field because only one bill seems to have
        # a partial payment. This can easily be entered manually.

        cr.execute(
            """
            SELECT 
                PCH1.DocEntry AS InvoiceDocEntry,
                PCH1.LineNum AS InvoiceLineNum,
                POR1.DocEntry AS PurchaseOrderDocEntry,
                POR1.LineNum AS PurchaseOrderLineNum
            FROM 
                PCH1
            INNER JOIN 
                PDN1 
                ON PCH1.BaseEntry = PDN1.DocEntry 
                AND PCH1.BaseLine = PDN1.LineNum 
                AND PCH1.BaseType = 20 -- Goods Receipt POs have BaseType = 20
            INNER JOIN 
                POR1 
                ON PDN1.BaseEntry = POR1.DocEntry 
                AND PDN1.BaseLine = POR1.LineNum 
                AND PDN1.BaseType = 22 -- Purchase Orders have BaseType = 22
            """
        )
        invoice_po_rel_lines = cr.fetchall()
        # Only get product lines (where sap_line_num is set)
        po_lines = self.env["purchase.order.line"].search_read(
            [
                ("sap_docentry", "!=", False),
                ("sap_line_num", "!=", False),
                ("sap_table", "=", "por1"),
            ],
            ["id", "sap_docentry", "sap_line_num"],
        )
        po_lines_dict = {
            (line["sap_docentry"], line["sap_line_num"]): line["id"]
            for line in po_lines
        }
        invoice_line_to_po_id_dict = {
            (row[0], row[1]): po_lines_dict.get((row[2], row[3]))
            for row in invoice_po_rel_lines
        }
        bill_vals = []
        _logger.info(f"Creating values for {len(open_bills)} bills...")
        for order in open_bills:
            lines = []
            if order["docentry"] in bill_lines_dict:
                lines = [
                    Command.create(vals)
                    for vals in self._get_line_vals(
                        bill_lines_dict[order["docentry"]],
                        invoice_line_to_po_id_dict,
                        order["docduedate"],
                    )
                ]

            vals = {
                "sap_docentry": order["docentry"],
                "sap_docnum": order["docnum"],
                "sap_table": "opch",
                "sap_atcentry": order["atcentry"],
                "date": order["docdate"],
                "invoice_date": order["docdate"],
                "invoice_date_due": order["docduedate"],
                "partner_id": partners_dict[order["cardcode"]].id,
                "currency_id": _get_currency_id(order["doccur"]),
                "line_ids": lines,
                "move_type": "in_invoice",
            }
            bill_vals.append(vals)
        _logger.info(f"Creating {len(bill_vals)} bills...")
        bills = self.env["account.move"].create(bill_vals)
        _logger.info(
            f"Created {len(bills)} bills with {len(bills.mapped("line_ids"))}."
        )
        bills.action_post()

    @api.model
    def _get_line_vals(self, lines, order_lines_dict, due_date):
        vals = []
        default_account = (
            self.env["account.journal"]
            .search([("type", "=", "purchase")], limit=1)
            .default_account_id.id
        )
        # First sort lines by their position and sequence
        sorted_lines = sorted(
            lines,
            key=lambda x: (
                (
                    x.get("linenum")
                    if x.get("linenum") is not None
                    else x.get("aftlinenum")
                ),
                x.get("lineseq") or 0,
            ),
        )

        for i, line in enumerate(sorted_lines, 1):
            # Only try to link product lines (where linenum is set)
            order_line_id = None
            if line.get("linenum"):
                order_line_id = order_lines_dict.get(
                    (line["docentry"], line["linenum"])
                )

            quantity = line["quantity"]
            unit_price = line["price"]
            line_total = line["linetotal"]
            if line_total and not quantity:
                if unit_price:
                    quantity = round(line_total / unit_price)
                else:
                    quantity = 1

            line_vals = {
                "sequence": i
                * 100,  # Use incremental sequence based on sorted position
                "quantity": quantity,
                "price_unit": line["price"],
            }

            # Handle product lines vs text lines
            if line.get("linenum"):  # Product line
                line_vals.update(
                    {
                        "sap_line_num": line["linenum"],
                        "product_id": self._get_product(line["itemcode"]),
                        "account_id": self._get_account(line["acctcode"])
                        or default_account,
                    }
                )
                if order_line_id:
                    line_vals["purchase_line_id"] = order_line_id
            else:  # Text line
                line_vals.update(
                    {
                        "display_type": "line_note",
                        "name": line["linetext"],
                        "sap_aftlinenum": line["aftlinenum"],
                        "sap_lineseq": line["lineseq"],
                    }
                )

            vals.append(line_vals)
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
