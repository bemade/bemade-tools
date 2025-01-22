from docutils.nodes import bullet_list

from odoo import models, fields, api, Command
from odoo.tools.sql import SQL
import logging

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    sap_docentry = fields.Integer(index="btree", string="SAP Document Entry")
    sap_docnum = fields.Integer(index="btree", string="SAP Document Number")
    sap_table = fields.Char(index="btree")
    sap_atcentry = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "sap_docnum_unique",
            "UNIQUE(sap_docnum, sap_table)",
            "SAP docnum must be unique!",
        )
    ]


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    sap_line_num = fields.Integer(index="btree")
    sap_aftlinenum = fields.Integer(index="btree")
    sap_lineseq = fields.Integer(index="btree")
    sap_docentry = fields.Integer(
        related="move_id.sap_docentry",
        store=True,
        index="btree",
    )
    sap_table = fields.Char(
        index="btree",
    )

    _sql_constraints = [
        (
            "sap_line_type_check",
            """CHECK(
                (sap_line_num != 0 AND sap_lineseq = 0 AND sap_aftlinenum = 0) OR  -- 0 replaces null since Odoo doesn't insert null into Integer fields
                (sap_line_num = 0 AND sap_lineseq != 0 AND sap_aftlinenum !=0)
            )""",
            "A line must have either a line_num (for product lines) or an aftlinenum (for text lines), but not both.",
        ),
        (
            "sap_line_docentry_table_unique",
            "UNIQUE(sap_line_num, sap_aftlinenum, sap_lineseq, sap_docentry, sap_table)",
            "Another line with this line number and docentry already exists for this SAP table.",
        ),
    ]


class AccountMoveCommon(models.AbstractModel):
    _name = "sap.account.move.importer.mixin"
    _description = "Common functionality for SAP invoice and bill importers"

    @api.model
    def _get_row_vals(self, row, products_dict, sap_table, order_lines_dict):
        # Handle text lines from INV10/PCH10
        if "linetext" in row:  # This is a text line
            vals = {
                "display_type": "line_note",
                "name": row["linetext"] or " ",
                "quantity": 0.0,
                "price_unit": 0.0,
                "sap_line_num": 0,  # Text lines don't have a line_num, use 0 as null
                "sap_aftlinenum": (row["aftlinenum"] or 0)
                + 2,  # Increment by 2 to avoid 0
                "sap_lineseq": (row["lineseq"] or 0) + 2,  # Increment by 2 to avoid 0
                "sap_table": sap_table.replace(
                    "1", "10"
                ),  # Use INV10/PCH10 for text lines
                "sequence": (
                    row["aftlinenum"] * 100 + row["lineseq"]
                    if row["aftlinenum"] and row["lineseq"]
                    else 0
                ),
            }
            return vals

        # Handle product lines
        product = products_dict.get(row["itemcode"])
        vals = {
            "product_id": product.id if product else False,
            "quantity": row["quantity"] if row["quantity"] else 0.0,
            "price_unit": row["price"],
            "discount": row["discprcnt"],
            "sap_line_num": (row["linenum"] or 0) + 2,  # Increment by 2 to avoid 0
            "sap_aftlinenum": 0,  # Product lines don't have aftlinenum, use 0 as null
            "sap_lineseq": 0,  # Product lines don't have lineseq, use 0 as null
            "sap_table": sap_table,  # Use INV1/PCH1 for product lines
            "sequence": row["linenum"] * 100 if row["linenum"] else 0,
        }
        if not vals["product_id"]:
            vals["name"] = row["dscription"] or ""
            vals["product_uom_id"] = self.env.ref("uom.product_uom_unit").id

        # Link to order line if available
        order_line_id = order_lines_dict.get((row["docentry"], row["linenum"]))
        if order_line_id:
            vals.update(self._get_order_line_link_vals(order_line_id))

        return vals

    @api.model
    def _get_order_line_links(self, cr):
        """Get links between move lines and order lines."""
        config = self._get_order_line_link_config()
        if not config:
            return {}

        cr.execute(
            """
            SELECT 
                {invoice_line_table}.DocEntry AS InvoiceDocEntry,
                {invoice_line_table}.LineNum AS InvoiceLineNum,
                {order_line_table}.DocEntry AS OrderDocEntry,
                {order_line_table}.LineNum AS OrderLineNum
            FROM 
                {invoice_line_table}
            INNER JOIN 
                {picking_table}
                ON {invoice_line_table}.BaseEntry = {picking_table}.DocEntry 
                AND {invoice_line_table}.BaseLine = {picking_table}.LineNum 
                AND {invoice_line_table}.BaseType = {picking_basetype}
            INNER JOIN 
                {order_line_table}
                ON {picking_table}.BaseEntry = {order_line_table}.DocEntry 
                AND {picking_table}.BaseLine = {order_line_table}.LineNum 
                AND {picking_table}.BaseType = {order_basetype}
            """.format(
                invoice_line_table=config["invoice_line_table"],
                order_line_table=config["order_line_table"],
                picking_table=config["picking_table"],
                picking_basetype=config["picking_basetype"],
                order_basetype=config["order_basetype"],
            )
        )
        rel_lines = cr.fetchall()

        # Only get product lines (where sap_line_num is set)
        order_lines = self.env[config["order_line_model"]].search_read(
            [
                ("sap_docentry", "!=", False),
                ("sap_line_num", "!=", False),
                ("sap_table", "=", config["order_line_table"].lower()),
            ],
            ["id", "sap_docentry", "sap_line_num"],
        )
        order_lines_dict = {
            (line["sap_docentry"], line["sap_line_num"]): line["id"]
            for line in order_lines
        }
        return {
            (row[0], row[1]): order_lines_dict.get((row[2], row[3]))
            for row in rel_lines
        }

    def _get_order_line_link_config(self):
        """Get configuration for linking to order lines. Override in child classes."""
        return None

    def _get_order_line_link_vals(self, order_line_id):
        """Get the values to link to an order line. Override in child classes."""
        return {}

    @api.model
    def _get_lines(self, cr, lines_table, sap_orders):
        docentries = [order["docentry"] for order in sap_orders]
        # Get product lines
        query = SQL(
            "SELECT *, 'product' as line_type FROM %s WHERE docentry in %s ORDER BY docentry, linenum",
            SQL.identifier(lines_table),
            tuple(docentries),
        )
        cr.execute(query)
        product_lines = cr.dictfetchall()

        # Get text lines from INV10/PCH10
        text_table = lines_table.replace(
            "1", "10"
        )  # Convert INV1->INV10 or PCH1->PCH10
        query = SQL(
            """
            SELECT *, 'text' as line_type 
            FROM %s 
            WHERE docentry in %s 
                AND linetext IS NOT NULL 
                AND linetext <> '' 
            ORDER BY aftlinenum, lineseq
            """,
            SQL.identifier(text_table),
            tuple(docentries),
        )
        cr.execute(query)
        text_lines = cr.dictfetchall()

        # Merge and return all lines
        return product_lines + text_lines

    @api.model
    def _get_move_vals(self, order, partner, lines, sap_table, order_lines_dict):
        """Get common values for both invoices and bills"""
        if order["docentry"] in lines:
            move_lines = [
                Command.create(
                    self._get_row_vals(
                        line,
                        self._get_products_dict(),
                        sap_table,
                        order_lines_dict,
                    )
                )
                for line in lines[order["docentry"]]
            ]
        else:
            move_lines = []

        return {
            "partner_id": partner.id,
            "invoice_date": order["docdate"],
            "date": order["docdate"],
            "invoice_date_due": order["docduedate"],
            "sap_docentry": order["docentry"],
            "sap_docnum": order["docnum"],
            "sap_table": sap_table,
            "ref": order["numatcard"],
            "line_ids": move_lines,
        }

    @api.model
    def _get_products_dict(self):
        return {
            product.sap_item_code: product
            for product in self.env["product.product"].search(
                [("sap_item_code", "!=", False)]
            )
        }

    @api.model
    def _get_partners_dict(self):
        return {
            partner.sap_card_code: partner
            for partner in self.env["res.partner"].search(
                [("sap_card_code", "!=", False)]
            )
        }

    @api.model
    def import_moves(self, cr):
        """Import moves from SAP with configurable parameters."""
        config = self._get_import_config()
        if not config:
            return

        # Filter out already imported documents
        already_imported = self.env["account.move"].search(
            [
                ("sap_docnum", "!=", False),
                ("sap_table", "=", config["header_table"].lower()),
            ]
        )

        where = "WHERE docstatus='O'"
        args = []
        if already_imported:
            where += " AND docentry not in %s"
            args = [tuple(already_imported.mapped("sap_docentry"))]

        # Get open documents from SAP
        cr.execute(SQL(f"SELECT * FROM {config['header_table']} {where}", *args))
        open_docs = cr.dictfetchall()

        if not open_docs:
            _logger.info("No new documents to import")
            return

        # Get all lines (product and text)
        lines = self._get_lines(cr, config["line_table"], open_docs)
        lines_dict = {}
        for line in lines:
            lines_dict.setdefault(line["docentry"], []).append(line)

        partners_dict = self._get_partners_dict()
        order_lines_dict = self._get_order_line_links(cr)

        moves = self.env["account.move"]
        _logger.info(f"Creating {len(open_docs)} {config['move_type']} moves...")
        for doc in open_docs:
            partner = partners_dict.get(doc["cardcode"])
            if not partner:
                _logger.warning(
                    "Could not find partner with cardcode %s", doc["cardcode"]
                )
                continue

            vals = self._get_move_vals(
                doc, partner, lines_dict, config["line_table"], order_lines_dict
            )
            vals.update(
                {
                    "move_type": config["move_type"],
                }
            )
            moves |= self.env["account.move"].create(vals)

        _logger.info(
            f"Created {len(moves)} moves with {len(moves.mapped('line_ids'))} lines"
        )
        moves.action_post()
        return moves


class InvoiceImporter(models.AbstractModel):
    _name = "sap.invoice.importer"
    _description = "SAP Invoice Importer"
    _inherit = "sap.account.move.importer.mixin"

    @api.model
    def _get_import_config(self):
        return {
            "header_table": "OINV",
            "line_table": "inv1",
            "move_type": "out_invoice",
        }

    @api.model
    def _get_order_line_link_config(self):
        return {
            "invoice_line_table": "INV1",
            "order_line_table": "RDR1",
            "picking_table": "DLN1",
            "picking_basetype": 15,  # Deliveries have BaseType = 15
            "order_basetype": 17,  # Sales Orders have BaseType = 17
            "order_line_model": "sale.order.line",
        }

    def _get_order_line_link_vals(self, order_line_id):
        return {"sale_line_ids": [Command.link(order_line_id)]}

    @api.model
    def import_invoices(self, cr):
        """Import customer invoices from SAP."""
        return self.import_moves(cr)


class VendorBillsImporter(models.AbstractModel):
    _name = "sap.vendor.bill.importer"
    _description = "SAP Vendor Bill Importer"
    _inherit = "sap.account.move.importer.mixin"

    @api.model
    def _get_import_config(self):
        return {
            "header_table": "OPCH",
            "line_table": "pch1",
            "move_type": "in_invoice",
        }

    @api.model
    def _get_order_line_link_config(self):
        return {
            "invoice_line_table": "PCH1",
            "order_line_table": "POR1",
            "picking_table": "PDN1",
            "picking_basetype": 20,  # Goods Receipt POs have BaseType = 20
            "order_basetype": 22,  # Purchase Orders have BaseType = 22
            "order_line_model": "purchase.order.line",
        }

    @api.model
    def _get_order_line_link_vals(self, order_line_id):
        return {"purchase_line_id": order_line_id}

    @api.model
    def import_bills(self, cr):
        """Import vendor bills from SAP."""
        return self.import_moves(cr)
