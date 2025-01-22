import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor

from odoo import models, fields, api
from odoo.modules.registry import Registry
from odoo.sql_db import SQL
from datetime import datetime
import pytz

_logger = logging.getLogger(__name__)
workers = os.cpu_count() - 1


class SapSalePurchaseImporterMixin(models.AbstractModel):
    _name = "sap.sale.purchase.importer.mixin"
    _description = "SAP Sale and Purchase Order Importer Mixin"

    @api.model
    def _get_row_vals(self, row, products_dict, sap_table):
        # Handle text lines from RDR10/POR10
        if "linetext" in row:  # This is a text line
            if row["lineseq"] is None or row["aftlinenum"] is None:
                _logger.debug(f"Invalid line: {row}")
            vals = {
                "display_type": "line_note",
                "name": row["linetext"],
                "product_id": None,
                "product_uom_qty": 0.0,
                "price_unit": 0.0,
                "sap_line_num": None,  # Text lines don't have a line_num
                "sap_aftlinenum": row["aftlinenum"],  # Position to insert after
                "sap_lineseq": row["lineseq"],  # Sequence within position
                "sap_docentry": row["docentry"],
                "sap_table": sap_table.replace("1", "10"),
                "sequence": row["aftlinenum"] * 100 + row["lineseq"],
            }
            return vals

        # Handle product lines (existing code)
        product = products_dict.get(row["itemcode"])
        vals = {
            "product_id": product.id if product else False,
            "product_uom_qty": row["quantity"] if row["quantity"] else 0.0,
            "price_unit": row["price"],
            "discount": row["discprcnt"],
            "sap_line_num": row["linenum"],
            "sap_aftlinenum": None,  # Product lines don't have aftlinenum
            "sap_lineseq": None,  # Product lines don't have lineseq
            "sap_docentry": row["docentry"],
            "sap_table": sap_table,
            "sequence": row["linenum"] * 100,
        }
        return vals

    @api.model
    def _get_products_dict(self):
        products = self.env["product.product"].search(
            [("sap_item_code", "!=", False), ("active", "in", [True, False])]
        )
        return {product.sap_item_code: product for product in products}

    @api.model
    def _get_partner(self, sap_order, contacts_dict, partners_dict):
        if sap_order["cntctcode"]:
            cntctcode = sap_order["cntctcode"]
            return contacts_dict.get(cntctcode)
        else:
            cardcode = sap_order["cardcode"]
            return (
                partners_dict.get(cardcode)
                or partners_dict.get(cardcode.upper())
                or partners_dict.get(cardcode.lower())
            )

    @api.model
    def _get_partners_dict(self):
        partners = self.env["res.partner"].search(
            [
                "|",
                ("sap_card_code", "!=", False),
                ("sap_cntct_code", "!=", False),
                ("active", "in", [False, True]),
            ]
        )
        return {partner.sap_card_code: partner for partner in partners}

    @api.model
    def _get_contacts_dict(self):
        contacts = self.env["res.partner"].search(
            [
                ("sap_cntct_code", "!=", False),
                ("active", "in", [False, True]),
            ]
        )
        return {contact.sap_cntct_code: contact for contact in contacts}

    @api.model
    def _get_payment_terms_dict(self):
        return {
            term.sap_groupnum: term
            for term in self.env["account.payment.term"].search(
                [("sap_groupnum", "!=", False)]
            )
        }

    def _get_imported_docnums_from_table(self, table):
        sql = SQL(
            """
        SELECT distinct(sap_docnum) from %s WHERE sap_docnum is not null
        """,
            SQL.identifier(table),
        )
        cr = self.env.cr
        cr.execute(sql)
        docnums = [order[0] for order in cr.fetchall()]
        return docnums

    def _create_orders(
        self, cr, pager, lines_table, header_model, lines_model, multiproc=True
    ):
        start_method = multiprocessing.get_start_method()
        multiprocessing.set_start_method("fork", force=True)
        chunks = [chunk for chunk in pager]
        try:
            if not multiproc:
                for chunk in chunks:
                    self._sub_create_orders(
                        self._name,
                        self.env.cr.dbname,
                        self.env.uid,
                        dict(self.env.context),
                        header_model,
                        lines_model,
                        chunk,
                        self._get_lines(cr, lines_table, chunk),
                        lines_table,
                    )
            else:
                with ProcessPoolExecutor(max_workers=workers) as executor:
                    futures = [
                        executor.submit(
                            self._sub_create_orders,
                            self._name,
                            self.env.cr.dbname,
                            self.env.uid,
                            dict(self.env.context),
                            header_model,
                            lines_model,
                            chunk,
                            self._get_lines(cr, lines_table, chunk),
                            lines_table,
                        )
                        for chunk in chunks
                    ]
                    for future in futures:
                        future.result()
        except Exception:
            _logger.error("An exception occurred in a subprocess.", exc_info=True)
            raise
        finally:
            multiprocessing.set_start_method(start_method, force=True)

    @staticmethod
    def _get_lines(cr, lines_table, sap_orders):
        docentries = [order["docentry"] for order in sap_orders]
        # Get product lines
        query = SQL(
            "SELECT * FROM %s WHERE docentry in %s",
            SQL.identifier(lines_table),
            tuple(docentries),
        )
        cr.execute(query)
        product_lines = cr.dictfetchall()

        # Get text lines from RDR10/POR10
        text_table = lines_table.replace(
            "1", "10"
        )  # Convert RDR1->RDR10 or POR1->POR10
        query = SQL(
            "SELECT * FROM %s WHERE docentry in %s ORDER BY aftlinenum, lineseq",
            SQL.identifier(text_table),
            tuple(docentries),
        )
        cr.execute(query)
        text_lines = cr.dictfetchall()

        # Merge product and text lines, maintaining order
        merged_lines = product_lines + text_lines
        return merged_lines

    @staticmethod
    def _sub_create_orders(
        importer_model,
        dbname,
        uid,
        context,
        header_model,
        lines_model,
        sap_orders,
        sap_order_rows,
        sap_rows_table,
    ):
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, uid, context)
            self = env[importer_model]
            _logger.info(
                f"Importing {len(sap_orders)} orders with "
                f"{len(sap_order_rows)} rows."
            )
            _logger.info("Getting order vals...")
            order_vals = self._get_order_vals(
                sap_order_rows, sap_orders, sap_rows_table
            )
            _logger.info("Creating objects...")
            env[header_model].create(order_vals)
            _logger.info("Flushing to the database...")
            env[header_model].flush_model()
            env[lines_model].flush_model()
            cr.commit()
        return 0

    @api.model
    def _get_order_vals(self, sap_order_rows, sap_orders, sap_table):
        raise NotImplementedError

    @api.model
    def _confirm_closed_orders_by_table(self, cr, sap_table, odoo_table, odoo_model):
        """Mark confirmed orders that are confirmed and closed in SAP. This does NOT
        create delivery orders as the confirmation is just flagged directly in the DB.
        """
        confirmed_orders = self._get_closed_orders_by_table(cr, sap_table)
        state = getattr(self, "_confirmed_state")
        if confirmed_orders:
            _logger.info(
                f"Marking {len(confirmed_orders)} orders as confirmed and closed "
                f"(no delivery order)."
            )
            sql = """
                    UPDATE %s set state=%s WHERE sap_docnum in %s
                    """
            self.env.flush_all()
            self.env.cr.commit()
            self.env.cr.execute(
                SQL(
                    sql,
                    SQL.identifier(odoo_table),
                    state,
                    tuple(
                        confirmed_orders,
                    ),
                )
            )

    @api.model
    def _get_closed_orders_by_table(self, cr, sap_table):
        """
        Retrieve the list of closed orders for a specific SAP table.

        This method queries the given SAP table to obtain a list of orders that have the
        'confirmed', 'invntsttus', and 'canceled' fields satisfying specific conditions.
        Only orders that are confirmed, closed, and not canceled will be included in the
        returned list.

        :param cr: The cursor for database operations.
        :param sap_table (str): The name of the SAP table to query.

        :returns: A list containing the document numbers of the closed orders
            that meet the specified conditions.
        """
        sql = """
        SELECT docnum from %s
        WHERE confirmed = 'Y' and invntsttus = 'C' and canceled = 'N'
        """
        cr.execute(SQL(sql, SQL.identifier(sap_table)))
        confirmed_orders = [order[0] for order in cr.fetchall()]
        return confirmed_orders

    @api.model
    def _cancel_canceled_orders_and_quotations_by_table(
        self, cr, sap_order_table, sap_quote_table, odoo_table
    ):
        """Mark canceled orders as cancelled directly in the DB.

        Consider than an order is cancelled either if marked canceled or if it's been
        confirmed and closed despite its inventory status being open."""
        sql = """
        SELECT docnum FROM %s
        WHERE canceled = 'Y' OR (confirmed='Y' and docstatus='C' and invntsttus='O')
        UNION
        SELECT docnum FROM %s
        WHERE canceled = 'Y'
        """
        cr.execute(
            SQL(sql, SQL.identifier(sap_order_table), SQL.identifier(sap_quote_table))
        )
        canceled_orders = [order[0] for order in cr.fetchall()]
        if canceled_orders:
            _logger.info(f"Cancelling {len(canceled_orders)} cancelled orders ...")
            sql = """
                UPDATE %s set state='cancel' WHERE sap_docnum in %s
                """
            self.env.cr.execute(
                SQL(sql, SQL.identifier(odoo_table), tuple(canceled_orders))
            )

    @api.model
    def _confirm_open_orders_by_table(self, cr, sap_table, odoo_model, confirm_method):
        """Mark confirmed orders that are open and confirmed in SAP. This is done
        separately due to the long runtime of confirming orders through the ORM."""
        self.env["sale.order"].flush_model()
        sql = """
        SELECT docnum, docdate, createdate  FROM %s
        WHERE canceled='N' and confirmed='Y' and docstatus='O'
        """
        cr.execute(SQL(sql, SQL.identifier(sap_table)))
        sap_orders = cr.fetchall()
        open_orders = [order[0] for order in sap_orders]
        active_automations = self.env["base.automation"].search([("active", "=", True)])
        active_automations.active = False
        self.env["base.automation"].flush_model()
        if open_orders:
            self._sub_confirm_open_orders_by_table(
                odoo_model,
                confirm_method,
                open_orders,
            )
            self.env.cr.commit()
        active_automations.active = True

    def _set_order_dates(self, cr, odoo_table, sap_table):
        cr.execute(
            SQL("SELECT docnum, docdate, createdate FROM %s", SQL.identifier(sap_table))
        )
        sap_orders = cr.fetchall()
        self._set_order_dates_sub(sap_orders, odoo_table)
        self.env.cr.commit()

    def _set_order_dates_sub(self, sap_orders, odoo_table):
        self.env.cr.execute("DROP TABLE IF EXISTS sap_order_dates")
        self.env.cr.execute(
            "CREATE TEMP TABLE sap_order_dates (docnum INT, docdate DATE, createdate DATE)"
        )
        # The dates from SAP are already in UTC midnight, we just need to extract the date part
        values = [
            (
                order[0],
                order[1].date() if order[1] else None,  # Extract just the date part
                order[2].date() if order[2] else None,  # Extract just the date part
            )
            for order in sap_orders
        ]
        insert_query = b",".join(
            self.env.cr.mogrify("(%s, %s, %s)", value) for value in values
        ).decode("utf-8")
        self.env.cr.execute(
            f"INSERT INTO sap_order_dates (docnum, docdate, createdate) VALUES {insert_query}"
        )
        self.env.cr.execute(
            SQL(
                """
            UPDATE %s orders
            SET create_date=temp.createdate, date_order=temp.docdate
            FROM sap_order_dates temp
            WHERE orders.sap_docnum=temp.docnum
            """,
                SQL.identifier(odoo_table),
            ),
        )
        self.env.cr.commit()

    def _sub_confirm_open_orders_by_table(self, odoo_model, confirm_method, sap_orders):
        recs = self.env[odoo_model].search(
            [
                ("sap_docnum", "in", sap_orders),
                ("state", "in", ["draft", "sent"]),
            ],
        )
        _logger.info(f"Confirming {len(recs)} open orders ...")
        method = getattr(recs, confirm_method)
        method()


class PaymentTerms(models.Model):
    _inherit = "account.payment.term"

    sap_groupnum = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "unique_sap_groupnum",
            "UNIQUE(sap_groupnum)",
            "A payment term with this SAP ID already exists.",
        )
    ]
