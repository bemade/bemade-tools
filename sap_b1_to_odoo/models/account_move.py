from odoo import models, fields, api, Command
from odoo.tools.sql import SQL
import logging
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

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
            "EXCLUDE USING btree (sap_docnum WITH =, sap_table WITH =) WHERE (sap_docnum != 0 AND sap_table IS NOT NULL)",
            "SAP docnum must be unique when set!",
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
        """Get links between move lines and order lines.

        This method finds links between invoice lines and order lines through two paths:
        1. Direct link: Invoice line -> Sales Order line
        2. Through delivery: Invoice line -> Delivery line -> Sales Order line
        """
        config = self._get_order_line_link_config()
        if not config:
            return {}

        cr.execute(
            """
            SELECT 
                {invoice_line_table}.DocEntry AS invoicedocentry,
                {invoice_line_table}.LineNum AS invoicelinenum,
                CASE 
                    WHEN {invoice_line_table}.BaseType = {order_basetype} THEN {invoice_line_table}.BaseEntry  -- Direct from sales order
                    WHEN {invoice_line_table}.BaseType = {picking_basetype} THEN (  -- Through delivery
                        SELECT BaseEntry 
                        FROM {picking_table}
                        WHERE DocEntry = {invoice_line_table}.BaseEntry 
                        AND LineNum = {invoice_line_table}.BaseLine
                    )
                END as orderdocentry,
                CASE 
                    WHEN {invoice_line_table}.BaseType = {order_basetype} THEN {invoice_line_table}.BaseLine  -- Direct from sales order
                    WHEN {invoice_line_table}.BaseType = {picking_basetype} THEN (  -- Through delivery
                        SELECT BaseLine 
                        FROM {picking_table}
                        WHERE DocEntry = {invoice_line_table}.BaseEntry 
                        AND LineNum = {invoice_line_table}.BaseLine
                    )
                END as orderlinenum
            FROM {invoice_line_table}
            WHERE {invoice_line_table}.BaseType IN ({picking_basetype}, {order_basetype})  -- delivery or sales order
            """.format(
                invoice_line_table=config["invoice_line_table"],
                picking_table=config["picking_table"],
                picking_basetype=config["picking_basetype"],
                order_basetype=config["order_basetype"],
            )
        )
        rel_lines = cr.dictfetchall()

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
            # The sap_line_num in order lines already has +2, so we need to subtract it here
            (line["sap_docentry"], line["sap_line_num"] - 2): line["id"]
            for line in order_lines
        }
        return {
            # The invoice line numbers from SAP don't have +2 yet
            (row["invoicedocentry"], row["invoicelinenum"]): order_lines_dict.get(
                (row["orderdocentry"], row["orderlinenum"])
            )
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

        users_dict = self._get_users_dict()
        invoice_user_id = users_dict.get(order.get("slpcode"), False)

        # Get the currency from SAP's DocCur field, default to CAD if not set
        currency_code = order.get("doccur", "CAD")
        currency = self.env["res.currency"].search([("name", "=", currency_code)])
        if not currency:
            currency = self.env.ref("base.CAD")

        # Get the currency rate from SAP's DocRate field
        # SAP stores the rate as foreign currency to base currency
        # Odoo stores it as 1 / that rate
        rate = order.get("docrate", 1.0)
        if rate and rate != 1.0:
            # Create a rate for this specific date if it doesn't exist
            date = fix_tz(order["docdate"])
            existing_rate = self.env["res.currency.rate"].search(
                [
                    ("currency_id", "=", currency.id),
                    ("company_id", "=", self.env.company.id),
                    ("name", "=", date),
                ]
            )
            if not existing_rate:
                self.env["res.currency.rate"].create(
                    {
                        "currency_id": currency.id,
                        "rate": 1.0 / rate,  # Invert the rate for Odoo
                        "name": date,
                        "company_id": self.env.company.id,
                    }
                )

        return {
            "partner_id": partner.id,
            "invoice_date": fix_tz(order["docdate"]),
            "date": fix_tz(order["docdate"]),
            "invoice_date_due": fix_tz(order["docduedate"]),
            "sap_docentry": order["docentry"],
            "sap_docnum": order["docnum"],
            "sap_table": sap_table,
            "ref": order["numatcard"],
            "line_ids": move_lines,
            "invoice_user_id": invoice_user_id,
            "currency_id": currency.id,
        }

    @api.model
    def _get_users_dict(self):
        return {
            user.sap_slpcode: user.id
            for user in self.env["res.users"].search(
                [
                    ("sap_slpcode", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
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
            "header_table": "oinv",
            "line_table": "inv1",
            "move_type": "out_invoice",
        }

    @api.model
    def _get_order_line_link_config(self):
        return {
            "invoice_line_table": "inv1",
            "order_line_table": "rdr1",
            "picking_table": "dln1",
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
            "order_line_table": "por1",
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
