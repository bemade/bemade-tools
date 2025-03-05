from odoo import models, fields, api, Command
from odoo.tools.sql import SQL
from odoo.tools import mute_logger
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from odoo.modules.registry import Registry
from odoo.addons.sap_b1_to_odoo.tools import fix_tz, PagingIterator
from psycopg2.errors import SerializationFailure, DeadlockDetected, LockNotAvailable
import time

_logger = logging.getLogger(__name__)
workers = os.cpu_count() - 1
chunk_size = 500


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
                ("sap_table", "=", config["order_line_table"]),
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
    def _get_move_vals(self, order, partner_id, lines, sap_table, order_lines_dict):
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

        return {
            "partner_id": partner_id,
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
            partner.sap_card_code: partner.id
            for partner in self.env["res.partner"].search(
                [("sap_card_code", "!=", False)]
            )
        }

    @api.model
    def _pre_create_currency_rates(self, moves):
        """Pre-create all currency rates needed for the moves to avoid duplication in multi-processing."""
        rates_to_create = []
        processed_rates = (
            set()
        )  # Track (currency_id, date) pairs we've already processed

        # Get all existing rates for the company
        existing_rates = self.env["res.currency.rate"].search(
            [("company_id", "=", self.env.company.id)]
        )

        # Create a set of (currency_id, date) tuples for existing rates
        existing_rate_keys = {
            (rate.currency_id.id, rate.name) for rate in existing_rates
        }

        for order in moves:
            # Skip if no currency or rate information
            if (
                not order.get("doccur")
                or not order.get("docrate")
                or order.get("docrate") == 1.0
            ):
                continue

            # Get the currency
            currency_code = order.get("doccur", "CAD")
            currency = self.env["res.currency"].search([("name", "=", currency_code)])
            if not currency:
                currency = self.env.ref("base.CAD")

            # Get the date
            date = fix_tz(order["docdate"]).date()

            # Check if we've already processed this currency/date pair
            rate_key = (currency.id, date)
            if rate_key in processed_rates or rate_key in existing_rate_keys:
                continue

            # Add to rates to create
            rate = order.get("docrate", 1.0)
            rates_to_create.append(
                {
                    "currency_id": currency.id,
                    "rate": 1.0 / rate,  # Invert the rate for Odoo
                    "name": date,
                    "company_id": self.env.company.id,
                }
            )

            # Mark as processed
            processed_rates.add(rate_key)

        if rates_to_create:
            _logger.info(f"Pre-creating {len(rates_to_create)} currency rates")
            self.env["res.currency.rate"].create(rates_to_create)
            self.env.flush_all()
            self.env.cr.commit()

    @api.model
    def import_moves(self, cr):
        """Import moves from SAP with configurable parameters."""
        config = self._get_import_config()
        if not config:
            return

        # Filter out already imported documents
        already_imported = self.env["account.move"].search_read(
            [
                ("sap_docnum", "!=", False),
                ("sap_table", "=", config["header_table"]),
            ],
            ["sap_docnum"],
        )

        where = ""
        args = []
        if already_imported:
            where += " WHERE docnum not in %s"
            args = [tuple([move["sap_docnum"] for move in already_imported])]

        # Get all new documents
        _logger.info(f"Fetching new account moves from {config['header_table']}")
        sql = SQL(f"SELECT * FROM {config['header_table']} {where}", *args)
        cr.execute(sql)
        moves = cr.dictfetchall()

        # Pre-create all currency rates before processing in parallel
        self._pre_create_currency_rates(moves)

        chunks = [moves[i : i + chunk_size] for i in range(0, len(moves), chunk_size)]

        # Import moves in chunks with multi-processing
        return self._import_moves_chunked(cr, chunks, config)

    @api.model
    def _reorganize_chunks_by_order(self, cr, chunks, order_lines_dict):
        _logger.info("Reorganizing chunks based on order references...")
        config = self._get_import_config()

        order_line_model = self._get_order_line_link_config()["order_line_model"]
        # Flatten all moves from all chunks
        all_moves = [move for chunk in chunks for move in chunk]

        # Get all invoice lines for these moves
        all_lines = self._get_lines(cr, config["line_table"], all_moves)

        # Group lines by docentry
        lines_by_docentry = {}
        for line in all_lines:
            docentry = line["docentry"]
            if docentry not in lines_by_docentry:
                lines_by_docentry[docentry] = []
            lines_by_docentry[docentry].append(line)

        # Use the existing order line links to get the SAP order document entries
        # This avoids duplicating SQL queries
        order_line_links = self._get_order_line_links(cr)

        # Group invoices by their SAP order document entries
        invoice_to_orders = {}
        for (
            invoice_docentry,
            invoice_linenum,
        ), order_line_id in order_line_links.items():
            # We need to extract the SAP order docentry from the keys in order_lines_dict
            # The order_lines_dict maps (order_docentry, order_linenum) to order_line_id
            for (order_docentry, order_linenum), line_id in order_lines_dict.items():
                if line_id == order_line_id:
                    if invoice_docentry not in invoice_to_orders:
                        invoice_to_orders[invoice_docentry] = set()
                    invoice_to_orders[invoice_docentry].add(order_docentry)
                    break

        # Group invoices by SAP order document entry
        order_to_invoices = {}
        for invoice_docentry, order_docentries in invoice_to_orders.items():
            for order_docentry in order_docentries:
                if order_docentry not in order_to_invoices:
                    order_to_invoices[order_docentry] = set()
                order_to_invoices[order_docentry].add(invoice_docentry)

        # Handle the case where a bill is linked to multiple purchase orders
        # We need to merge groups that share bills
        # First, build a graph of connected orders
        connected_orders = {}
        for order_docentry, invoice_docentries in order_to_invoices.items():
            if order_docentry not in connected_orders:
                connected_orders[order_docentry] = set()

            # Find all other orders that share invoices with this order
            for invoice_docentry in invoice_docentries:
                for other_order_docentry in invoice_to_orders.get(invoice_docentry, []):
                    if other_order_docentry != order_docentry:
                        connected_orders[order_docentry].add(other_order_docentry)

        # Now use a graph traversal to find connected components (groups of related orders)
        visited = set()
        order_groups_map = {}
        group_id = 0

        def dfs(order, group):
            visited.add(order)
            order_groups_map[order] = group
            for connected_order in connected_orders.get(order, []):
                if connected_order not in visited:
                    dfs(connected_order, group)

        # Run DFS to find all connected components
        for order in connected_orders:
            if order not in visited:
                dfs(order, group_id)
                group_id += 1

        # Now rebuild order_to_invoices based on the connected components
        merged_order_to_invoices = {}
        for group in range(group_id):
            merged_order_to_invoices[group] = set()
            for order, order_group in order_groups_map.items():
                if order_group == group:
                    merged_order_to_invoices[group].update(
                        order_to_invoices.get(order, set())
                    )

        # Replace the original mapping with the merged one
        order_to_invoices = merged_order_to_invoices

        # Create a mapping of docentry to move
        docentry_to_move = {move["docentry"]: move for move in all_moves}

        # Create new chunks based on SAP baseentry references
        new_chunks = []
        processed_docentries = set()

        # Group invoices by connected component (related purchase orders)
        order_groups = []
        for group_id, invoice_docentries in order_to_invoices.items():
            group = []
            for docentry in invoice_docentries:
                if (
                    docentry in docentry_to_move
                    and docentry not in processed_docentries
                ):
                    group.append(docentry_to_move[docentry])
                    processed_docentries.add(docentry)
            if group:
                order_groups.append(group)

        # Sort groups by size (descending) for better packing
        order_groups.sort(key=len, reverse=True)

        # Now create chunks by packing groups into chunks up to chunk_size
        current_chunk = []
        current_chunk_size = 0

        for group in order_groups:
            group_size = len(group)

            # Always keep groups together, even if they exceed chunk_size
            # If we already have items in the current chunk and adding this group would exceed chunk_size,
            # finish the current chunk and start a new one with this group
            if current_chunk_size > 0 and current_chunk_size + group_size > chunk_size:
                new_chunks.append(current_chunk)
                current_chunk = []
                current_chunk_size = 0

            # Add this group to the current chunk
            # This will happen even if the group itself is larger than chunk_size
            current_chunk.extend(group)
            current_chunk_size += group_size

            # If we've reached or exceeded chunk_size, finish this chunk
            if current_chunk_size >= chunk_size and not (
                current_chunk_size == group_size
            ):
                # Only finish the chunk if it's not just a single large group
                # This ensures we don't split large groups
                new_chunks.append(current_chunk)
                current_chunk = []
                current_chunk_size = 0

        # Don't forget the last chunk if it has items
        if current_chunk:
            new_chunks.append(current_chunk)

        # Then, create chunks for remaining invoices without order references
        remaining_moves = [
            move for move in all_moves if move["docentry"] not in processed_docentries
        ]

        if remaining_moves:
            # Try to add remaining moves to existing chunks if they have space
            if new_chunks:
                last_chunk = new_chunks[-1]
                space_left = chunk_size - len(last_chunk)

                if space_left > 0:
                    # Add as many remaining moves as will fit
                    moves_to_add = remaining_moves[:space_left]
                    last_chunk.extend(moves_to_add)
                    remaining_moves = remaining_moves[space_left:]

            # Create new chunks for any remaining moves
            if remaining_moves:
                remaining_chunks = [
                    remaining_moves[i : i + chunk_size]
                    for i in range(0, len(remaining_moves), chunk_size)
                ]
                new_chunks.extend(remaining_chunks)
        return new_chunks

    @api.model
    def _import_moves_chunked(self, cr, chunks, config, multiproc=True):
        """Import moves from SAP in chunks with multi-processing."""
        total_chunks = len(chunks)

        if not total_chunks:
            _logger.info("No new documents to import")
            return

        _logger.info(f"Starting import of {total_chunks} chunks...")

        # Get order line links in the main process
        order_lines_dict = self._get_order_line_links(cr)

        # Get the partners in the main process
        partners_dict = self._get_partners_dict()

        # Reorganize chunks based on order references to prevent serialization conflicts
        # if multiproc and order_lines_dict:
        #     chunks = self._reorganize_chunks_by_order(cr, chunks, order_lines_dict)
        #     total_chunks = len(chunks)

        # Save the current constraint date parameter
        IrConfigParam = self.env["ir.config_parameter"].sudo()
        original_constraint_date = IrConfigParam.get_param(
            "sequence.mixin.constraint_start_date", "1970-01-01"
        )

        # Set the constraint date to tomorrow to allow importing historical documents
        # without sequence validation errors
        tomorrow = (fields.Date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        IrConfigParam.set_param("sequence.mixin.constraint_start_date", tomorrow)
        self.env.flush_all()
        self.env.cr.commit()

        total_moves_count = 0
        total_lines_count = 0
        if not multiproc:
            # Process chunks sequentially
            for i, chunk in enumerate(chunks, 1):
                # Get lines for this chunk before processing
                chunk_lines = self._get_lines(cr, config["line_table"], chunk)

                result = self._process_move_chunk(
                    config,
                    chunk,
                    chunk_lines,
                    order_lines_dict,
                    partners_dict,
                )
                total_moves_count += result.get("moves_count", 0)
                total_lines_count += result.get("lines_count", 0)
                _logger.info(f"Completed chunk {i}/{total_chunks}")
                self.env.flush_all()
                self.env.cr.commit()

        else:
            # Save the current start method and set it to 'fork' for multiprocessing
            start_method = multiprocessing.get_start_method()
            multiprocessing.set_start_method("fork", force=True)

            try:
                # Process chunks in parallel using ProcessPoolExecutor
                with ProcessPoolExecutor(max_workers=workers) as executor:
                    futures = [
                        executor.submit(
                            self._process_move_chunk_static,
                            self._name,
                            self.env.cr.dbname,
                            self.env.uid,
                            dict(self.env.context),
                            chunk,
                            config,
                            self._get_lines(cr, config["line_table"], chunk),
                            order_lines_dict,
                            partners_dict,
                        )
                        for chunk in chunks
                    ]

                    # Wait for all futures to complete
                    for i, future in enumerate(futures, 1):
                        result = future.result()
                        total_moves_count += result.get("moves_count", 0)
                        total_lines_count += result.get("lines_count", 0)
                        _logger.info(f"Completed chunk {i}/{total_chunks}")
            except Exception as e:
                _logger.error("An exception occurred in a subprocess.", exc_info=True)
                raise
            finally:
                # Restore the original multiprocessing start method
                multiprocessing.set_start_method(start_method, force=True)
                # Restore the original constraint date parameter
                self.env["ir.config_parameter"].sudo().set_param(
                    "sequence.mixin.constraint_start_date", original_constraint_date
                )

        if total_moves_count > 0:
            _logger.info(
                f"Created and posted {total_moves_count} moves with {total_lines_count} lines"
            )

        _logger.info("Import completed.")
        return

    @api.model
    def _process_move_chunk(
        self,
        config,
        chunk,
        lines,
        order_lines_dict,
        partners_dict,
    ):
        """Process a chunk of moves."""
        return self._process_chunk_with_env(
            self.env, chunk, config, lines, order_lines_dict, partners_dict
        )

    @api.model
    def _process_chunk_with_env(
        self,
        env,
        chunk,
        config,
        lines,
        order_lines_dict,
        partners_dict,
    ):
        """Import a chunk of moves with the given environment."""
        importer = env[self._name]

        # Organize lines by docentry
        lines_dict = {}
        for line in lines:
            lines_dict.setdefault(line["docentry"], []).append(line)

        vals_list = []
        _logger.info(f"Processing {len(chunk)} account moves in process {os.getpid()}")
        for doc in chunk:
            partner_id = partners_dict.get(doc["cardcode"])
            if not partner_id:
                _logger.warning(
                    "Could not find partner with cardcode %s, skipping docentry %s",
                    doc["cardcode"],
                    doc["docentry"],
                )
                continue

            vals = importer._get_move_vals(
                doc,
                partner_id,
                lines_dict,
                config["header_table"],
                order_lines_dict,
            )
            # Don't import empty invoices
            if not vals.get("line_ids"):
                continue
            vals.update(
                {
                    "move_type": config["move_type"],
                }
            )
            vals_list.append(vals)

        if vals_list:
            importer._lock_orders_and_lines(vals_list)
            moves = env["account.move"].create(vals_list)
            _logger.info(f"Created {len(moves)} account moves in process {os.getpid()}")
            moves.filtered(lambda m: m.amount_total < 0).action_switch_move_type()
            _logger.info(f"Posting {len(moves)} moves in chunk")
            moves.action_post()
        else:
            _logger.info("No new documents to create in this chunk")

        # Get the count of lines for reporting
        lines_count = len(moves.mapped("line_ids")) if moves else 0
        return {"moves_count": len(moves), "lines_count": lines_count}

    def _lock_orders_and_lines(self, vals_list):
        """Proactively lock orders and lines that will be affected by this chunk.
        This helps prevent serialization failures by ensuring consistent lock ordering.
        """
        link_config = self._get_order_line_link_config()
        line_model = link_config["order_line_model"]
        order_model = line_model.replace("_line", "")
        line_table = line_model.replace(".", "_")
        order_table = order_model.replace(".", "_")

        order_ids = []
        for vals in vals_list:
            move_lines = vals["line_ids"]
            for line in move_lines:
                # Each line is a Command.create, so the 3rd tuple element is its values dict
                order_link = line[2].get(self._get_order_link_field(), False)
                if not order_link:
                    continue
                if type(order_link) is int:
                    order_ids.append(order_link)
                else:
                    # First item in list, second tuple element - see Command.link
                    order_ids.append(order_link[0][1])
        if not order_ids:
            return

        query = SQL(
            "LOCK TABLE %s, %s IN exclusive MODE NOWAIT",
            SQL.identifier(line_table),
            SQL.identifier(order_table),
        )

        tries = 0
        while True:
            try:
                with mute_logger("odoo.sql_db"):
                    self.env.cr.execute(query)
                return
            except (SerializationFailure, DeadlockDetected, LockNotAvailable):
                tries += 1
                self.env.cr.rollback()
                time.sleep(2)

    @staticmethod
    def _process_move_chunk_static(
        importer_model,
        dbname,
        uid,
        context,
        chunk,
        config,
        lines,
        order_lines_dict,
        partners_dict,
    ):
        """Static method for processing a chunk of moves in a separate process."""
        max_tries = 5
        tries = 0
        while tries < max_tries:
            tries += 1
            try:
                with Registry(dbname).cursor() as cr, mute_logger("odoo.sql_db"):
                    env = api.Environment(cr, uid, context)
                    importer = env[importer_model]
                    result = importer._process_chunk_with_env(
                        env,
                        chunk,
                        config,
                        lines,
                        order_lines_dict,
                        partners_dict,
                    )
                    cr.commit()
                return result
            except SerializationFailure:
                if tries == max_tries:
                    raise
                _logger.info(
                    f"Serialization failure in {os.getpid()}. Retrying (attempt {tries+1})...",
                )
            except Exception as e:
                _logger.error(f"Error processing chunk: {str(e)}", exc_info=True)
                raise

    def _get_order_link_field(self):
        raise NotImplementedError


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
            "credit_type": "out_refund",
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

    @api.model
    def import_invoices(self, cr):
        """Import customer invoices from SAP."""
        return self.import_moves(cr)

    def _get_order_link_field(self):
        return "sale_line_ids"

    def _get_order_line_link_vals(self, order_line_id):
        return {self._get_order_link_field(): [Command.link(order_line_id)]}


class VendorBillsImporter(models.AbstractModel):
    _name = "sap.vendor.bill.importer"
    _description = "SAP Vendor Bill Importer"
    _inherit = "sap.account.move.importer.mixin"

    @api.model
    def _get_import_config(self):
        return {
            "header_table": "opch",
            "line_table": "pch1",
            "move_type": "in_invoice",
            "credit_type": "in_refund",
        }

    @api.model
    def _get_order_line_link_config(self):
        return {
            "invoice_line_table": "pch1",
            "order_line_table": "por1",
            "picking_table": "pdn1",
            "picking_basetype": 20,  # Goods Receipt POs have BaseType = 20
            "order_basetype": 22,  # Purchase Orders have BaseType = 22
            "order_line_model": "purchase.order.line",
        }

    @api.model
    def import_bills(self, cr):
        """Import vendor bills from SAP."""
        return self.import_moves(cr)

    def _get_order_link_field(self):
        return "purchase_line_id"

    def _get_order_line_link_vals(self, order_line_id):
        return {self._get_order_link_field(): order_line_id}
