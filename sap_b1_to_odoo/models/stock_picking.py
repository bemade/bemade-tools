import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
import psycopg2

from odoo import models, fields, api, Command
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator
from odoo.addons.stock.models.stock_picking import Picking
from odoo.addons.mrp.models.stock_quant import StockQuant
from odoo.tools.sql import SQL
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

workers = 8


def _dummy_set_scheduled_date(self):
    for picking in self:
        picking.move_ids.write({"date": picking.scheduled_date})


def _dummy_check_kits(self):
    pass


class StockPicking(models.Model):
    _inherit = "stock.picking"

    sap_odln_docentry = fields.Integer(index="btree")
    sap_opdn_docentry = fields.Integer(index="btree")
    sap_docnum = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "sap_odln_docentry_unique",
            "UNIQUE(sap_odln_docentry)",
            "sap_odln_docentry must be unique",
        ),
        (
            "sap_opdn_docentry_unique",
            "UNIQUE(sap_opdn_docentry)",
            "sap_opdn_docentry must be unique",
        ),
        (
            "sap_docnum_odln_unique",
            "UNIQUE(sap_docnum, sap_odln_docentry)",
            "SAP docnum must be unique for each document type.",
        ),
        (
            "sap_docnum_opdn_unique",
            "UNIQUE(sap_docnum, sap_opdn_docentry)",
            "SAP docnum must be unique for each document type.",
        ),
    ]


class StockPickingImporter(models.AbstractModel):
    _name = "sap.stock.picking.importer"
    _description = "SAP Stock Picking Importer"

    _sale_orders_dict = None

    @api.model
    def _get_carriers_dict(self):
        return {
            transporter.sap_trnspcode: transporter.delivery_carrier_id.id
            for transporter in self.env["sap.transporter"].search([])
        }

    @api.model
    def _get_products_dict(self):
        return {
            product["sap_item_code"]: product["id"]
            for product in self.env["product.product"].search_read(
                [["sap_item_code", "!=", False], ["active", "in", [True, False]]],
                ["id", "sap_item_code"],
            )
        }

    @api.model
    def _get_sales_dict(self):
        return {
            order.sap_docentry: order
            for order in self.env["sale.order"].search([("sap_docentry", "!=", False)])
        }

    @api.model
    def _get_po_lines_dict(self, lines):
        """Relate the baseentry to the purchase line id for a Goods Receipt PO line"""
        arg = tuple(line["baseentry"] for line in lines if line["basetype"] == 22)
        sql = SQL(
            """
        SELECT id, po.sap_docentry
        FROM sale_order_line sol
        INNER JOIN po on sol.purchase_order_id = po.id
        WHERE sol.id in %s
        """,
            arg,
        )
        self.env.cr.execute(sql)
        return {
            line["sap_docentry"]: line["id"]
            for line in self.env.cr.dictfetchall()
            if line["sap_docentry"]
        }

    @api.model
    def import_sale_pickings(self, cr):
        _logger.info(f"Checking for already imported pickings.")
        self.env.cr.execute(
            "SELECT sap_docnum FROM stock_picking WHERE sap_docnum IS NOT NULL"
        )
        imported_pickings = tuple(row[0] for row in self.env.cr.fetchall())
        _logger.info(f"Found {len(imported_pickings)} imported pickings.")
        where = "WHERE canceled = 'N'"
        args = []
        if imported_pickings:
            where += " AND docnum not in %s"
            args = [imported_pickings]

        chunk_size = 500
        delivery_pager = PagingIterator(
            cr,
            fetch_query=f"SELECT * FROM odln {where}",
            fetch_args=args,
            count_query=f"SELECT count(*) FROM odln {where}",
            count_args=args,
            limit=chunk_size,
            orderby="docentry",
            logger=_logger,
        )
        _logger.info(f"Delivery pager with {delivery_pager.count} entries ...")
        carriers_dict = self._get_carriers_dict()
        products_dict = self._get_products_dict()
        start_method = multiprocessing.get_start_method()
        chunks = [
            [chunk, self._get_sales_delivery_lines(cr, chunk)]
            for chunk in delivery_pager
        ]
        multiprocessing.set_start_method("fork", force=True)
        real_func = Picking._set_scheduled_date
        Picking._set_scheduled_date = _dummy_set_scheduled_date
        real_check_kits = StockQuant._check_kits
        StockQuant._check_kits = _dummy_check_kits
        active_automations = self.env["base.automation"].search([("active", "=", True)])
        active_automations.active = False
        self.env["base.automation"].flush_model()
        processed_chunks = 0
        total_chunks = len(chunks)

        try:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        self._import_sales_pickings_sub,
                        self._cr.dbname,
                        self._uid,
                        dict(self._context),
                        carriers_dict,
                        products_dict,
                        chunk[0],
                        chunk[1],
                    )
                    for chunk in chunks
                ]
                for future in futures:
                    future.result()
                    processed_chunks += 1
                    _logger.info(
                        f"Processed {processed_chunks * chunk_size} pickings so far."
                        f" {total_chunks - (processed_chunks)} chunks to go."
                    )
            # for chunk in chunks:
            #     self._import_sales_pickings_sub_single_process(
            #         carriers_dict,
            #         products_dict,
            #         chunk[0],
            #         chunk[1],
            #     )
        finally:
            multiprocessing.set_start_method(start_method, force=True)
            Picking._set_scheduled_date = real_func
            StockQuant._check_kits = real_check_kits
            active_automations.active = True

    @api.model
    def _import_sales_pickings_sub_single_process(
        self, carriers_dict, products_dict, deliveries, delivery_lines
    ):
        env = self.env
        sales_dict = self._get_sales_dict()
        warehouse = self.env["stock.warehouse"].search([])
        picking_vals = []
        lines_dict = {}
        for line in delivery_lines:
            lines_dict.setdefault(line["docentry"], []).append(line)
        for delivery in deliveries:
            new_vals = self._get_sales_picking_vals(
                delivery,
                lines_dict.get(delivery["docentry"], []),
                carriers_dict,
                products_dict,
                sales_dict,
                warehouse,
            )
            if new_vals:
                picking_vals.append(new_vals)
        _logger.info(f"Creating {len(picking_vals)} picking records.")
        env["stock.picking"].create(picking_vals)
        _logger.info(f"Flushing models.")
        env["stock.picking"].flush_model()
        env["stock.move"].flush_model()
        env["stock.move.line"].flush_model()
        _logger.info(f"Picking records successfully created.")

    @staticmethod
    def _import_sales_pickings_sub(
        dbname,
        uid,
        context,
        carriers_dict,
        products_dict,
        deliveries,
        delivery_lines,
    ):
        try:
            pid = os.getpid()
            _logger.info(
                f"Subprocess {pid} is processing {len(deliveries)} deliveries."
            )
            with Registry(dbname).cursor() as cr:
                retry_limit = 3
                retry_count = 0
                while retry_count < retry_limit:
                    try:
                        env = api.Environment(cr, uid, context)
                        self = env["sap.stock.picking.importer"]
                        sales_dict = self._get_sales_dict()
                        warehouse = self.env["stock.warehouse"].search([])
                        picking_vals = []
                        lines_dict = {}
                        for line in delivery_lines:
                            lines_dict.setdefault(line["docentry"], []).append(line)
                        for delivery in deliveries:
                            new_vals = self._get_sales_picking_vals(
                                delivery,
                                lines_dict.get(delivery["docentry"], []),
                                carriers_dict,
                                products_dict,
                                sales_dict,
                                warehouse,
                            )
                            if new_vals:
                                picking_vals.append(new_vals)
                        _logger.info(
                            f"[PID {pid}] Creating {len(picking_vals)} picking records."
                        )
                        env["stock.picking"].create(picking_vals)
                        _logger.info(f"[PID {pid}] Flushing models.")
                        env["stock.picking"].flush_model()
                        env["stock.move"].flush_model()
                        env["stock.move.line"].flush_model()
                        _logger.info(
                            f"[PID {pid}] Picking records successfully created."
                        )
                    except psycopg2.errors.SerializationFailure as e:
                        retry_count += 1
                        _logger.warning(
                            f"[PID {pid}] Serialization failure encountered. Retrying {retry_count}/{retry_limit}. Exception: {e}"
                        )
                        cr.rollback()
                        if retry_count >= retry_limit:
                            _logger.error(
                                f"[PID {pid}] Exceeded maximum retry attempts for serialization failure."
                            )
                            raise
                    break  # Exit retry loop on success
        except Exception as e:
            _logger.error("Subprocess threw an exception.", exc_info=e)
            raise e

    @api.model
    def _get_sales_picking_vals(
        self, delivery, lines, carriers_dict, products_dict, sales_dict, warehouse
    ):
        vals = self._get_picking_vals(
            delivery,
            lines,
            carriers_dict,
            products_dict,
            warehouse.out_type_id,
        )
        if not vals:
            return None
        sale = sales_dict.get(delivery["docentry"])
        vals.update(
            name=f"SAP/OUT/{delivery['docnum']}",
            group_id=sale.procurement_group_id.id,
            partner_id=sale.partner_id.id,
            origin=sale.name,
            picking_type_id=warehouse.out_type_id.id,
            sap_odln_docentry=delivery["docentry"],
        )
        return vals

    @api.model
    def _get_purchase_picking_vals(
        self, delivery, lines, carriers_dict, products_dict, purchase_dict, warehouse
    ):
        vals = self._get_picking_vals(
            delivery,
            lines,
            carriers_dict,
            products_dict,
            warehouse.in_type_id,
        )
        if not vals:
            return None
        purchase = purchase_dict.get(delivery["docentry"])
        vals.update(
            name=f"SAP/IN/{purchase.name}",
            purchase_id=purchase.id,
            partner_id=purchase.partner_id.id,
            origin=purchase.name,
            sap_opdn_docentry=delivery["docentry"],
        )
        return vals

    @api.model
    def _get_picking_vals(
        self,
        delivery,
        lines,
        carriers_dict,
        products_dict,
        operation_type,
        purchases_dict=None,
    ):
        if not lines:
            return None
        sap_docnum = delivery["docnum"]
        scheduled_date = delivery["docdate"].replace(tzinfo=None)
        date_done = delivery["docduedate"].replace(tzinfo=None)
        carrier = carriers_dict.get(delivery["trnspcode"], False)
        src = operation_type.default_location_src_id or self.env.ref(
            "stock.stock_location_suppliers"
        )
        dest = operation_type.default_location_dest_id or self.env.ref(
            "stock.stock_location_customers"
        )
        picking_vals = {
            "sap_docnum": sap_docnum,
            "move_ids": [
                Command.create(
                    self._get_move_vals(line, products_dict, src, dest, purchases_dict)
                )
                for line in lines
            ],
            "carrier_id": carrier,
            "scheduled_date": scheduled_date,
            "date_done": date_done,
            "picking_type_id": operation_type.id,
        }
        return picking_vals

    @api.model
    def _get_move_vals(self, line, products_dict, src, dest, purchases_dict=None):
        product_qty = line["quantity"]
        qty_open = line["openqty"]
        product_uom_qty = product_qty + qty_open
        product = products_dict.get(line["itemcode"], False)
        if not product:
            raise Exception(
                f"Product {line['itemcode']} not found in Odoo. "
                "Please import products from SAP B1."
            )
        date = (
            line["shipdate"]
            and line["shipdate"].replace(tzinfo=None)
            or fields.Datetime.now()
        )
        state = "done" if line["linestatus"] == "C" else "confirmed"

        def _get_move_line_vals():
            if not product_qty:
                return None
            if purchases_dict:
                purchase_line_id = purchases_dict.get(line["docentry"]).get(
                    line["linenum"]
                )
            return {
                "product_id": product,
                "quantity": product_qty,
                "date": date,
            }

        move_line_vals = _get_move_line_vals()
        return {
            "name": f"SAP Delivery Line {line['linenum']} for product {product}, docentry {line['docentry']}",
            "product_id": product,
            "product_uom_qty": product_uom_qty,
            "quantity": product_uom_qty,
            "date": date,
            "picking_id": 1,
            "move_line_ids": (
                [Command.create(move_line_vals)] if move_line_vals else False
            ),
            "state": state,
            "location_id": src.id,
            "location_dest_id": dest.id,
        }

    @api.model
    def import_purchase_pickings(self, cr):
        _logger.info(f"Checking for already imported purchase pickings.")
        self.env.cr.execute(
            "SELECT sap_docnum FROM stock_picking WHERE sap_docnum IS NOT NULL"
        )
        imported_pickings = tuple(row[0] for row in self.env.cr.fetchall())
        _logger.info(f"Found {len(imported_pickings)} imported pickings.")
        where = "WHERE canceled = 'N'"
        args = []
        if imported_pickings:
            where += " AND docnum not in %s"
            args = [imported_pickings]

        chunk_size = 500
        delivery_pager = PagingIterator(
            cr,
            fetch_query=f"SELECT * FROM opdn {where}",
            fetch_args=args,
            count_query=f"SELECT count(*) FROM opdn {where}",
            count_args=args,
            limit=chunk_size,
            orderby="docentry",
            logger=_logger,
        )
        _logger.info(f"Purchase pager with {delivery_pager.count} entries ...")
        carriers_dict = self._get_carriers_dict()
        products_dict = self._get_products_dict()
        start_method = multiprocessing.get_start_method()
        chunks = [
            [chunk, self._get_purchase_delivery_lines(cr, chunk)]
            for chunk in delivery_pager
        ]
        multiprocessing.set_start_method("fork", force=True)
        active_automations = self.env["base.automation"].search([("active", "=", True)])
        active_automations.active = False
        self.env["base.automation"].flush_model()
        processed_chunks = 0
        total_chunks = len(chunks)

        try:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        self._import_purchase_pickings_sub,
                        self._cr.dbname,
                        self._uid,
                        dict(self._context),
                        chunk[0],
                        carriers_dict,
                        products_dict,
                        chunk[1],
                    )
                    for chunk in chunks
                ]
                for future in futures:
                    future.result()
                    processed_chunks += 1
                    _logger.info(
                        f"Processed {processed_chunks * chunk_size} pickings so far."
                        f" {total_chunks - (processed_chunks)} chunks to go."
                    )
        finally:
            multiprocessing.set_start_method(start_method, force=True)
            active_automations.active = True

    @staticmethod
    def _import_purchase_pickings_sub(
        dbname, uid, context, deliveries, carriers_dict, products_dict, delivery_lines
    ):
        try:
            pid = os.getpid()
            _logger.info(
                f"Subprocess {pid} is processing {len(deliveries)} deliveries."
            )
            with Registry(dbname).cursor() as cr:
                retry_limit = 3
                retry_count = 0
                while retry_count < retry_limit:
                    try:
                        env = api.Environment(cr, uid, context)
                        self = env["sap.stock.picking.importer"]
                        purchase_dict = self._get_purchases_dict()
                        warehouse = env["stock.warehouse"].search([])
                        picking_vals = []
                        lines_dict = {}
                        for line in delivery_lines:
                            lines_dict.setdefault(line["docentry"], []).append(line)
                        for delivery in deliveries:
                            new_vals = self._get_purchase_picking_vals(
                                delivery,
                                lines_dict.get(delivery["docentry"], []),
                                carriers_dict,
                                products_dict,
                                purchase_dict,
                                warehouse,
                            )
                            if new_vals:
                                picking_vals.append(new_vals)
                        _logger.info(
                            f"[PID {pid}] Creating {len(picking_vals)} picking records."
                        )
                        env["stock.picking"].create(picking_vals)
                        _logger.info(f"[PID {pid}] Flushing models.")
                        env["stock.picking"].flush_model()
                        env["stock.move"].flush_model()
                        env["stock.move.line"].flush_model()
                        _logger.info(
                            f"[PID {pid}] Picking records successfully created."
                        )
                    except psycopg2.errors.SerializationFailure as e:
                        retry_count += 1
                        _logger.warning(
                            f"[PID {pid}] Serialization failure encountered. Retrying {retry_count}/{retry_limit}. Exception: {e}"
                        )
                        cr.rollback()
                        if retry_count >= retry_limit:
                            _logger.error(
                                f"[PID {pid}] Exceeded maximum retry attempts for serialization failure."
                            )
                            raise
                    break  # Exit retry loop on success
        except Exception as e:
            _logger.error("Subprocess threw an exception.", exc_info=e)
            raise e

    @staticmethod
    def _get_sales_delivery_lines(cr, deliveries):
        sql = """
            WITH RECURSIVE sale_delivery_lines AS (
                SELECT * FROM dln1 WHERE basetype=17
                
                UNION
                
                SELECT dln1.* FROM dln1
                JOIN sale_delivery_lines ON dln1.baseentry = sale_delivery_lines.docentry
                WHERE dln1.basetype=15
            )
            SELECT * FROM DLN1 WHERE 
                docentry IN (SELECT docentry FROM sale_delivery_lines)
                AND docentry IN %s
            ORDER BY docentry, linenum
            """
        cr.execute(
            SQL(
                sql,
                tuple([delivery["docentry"] for delivery in deliveries]),
            )
        )
        delivery_lines = cr.dictfetchall()
        return delivery_lines

    @staticmethod
    def _get_purchase_delivery_lines(cr, deliveries):
        sql = """
            SELECT * FROM pdn1 WHERE 
                docentry IN (SELECT docentry FROM pdn1 WHERE basetype in (20, 22))
                AND docentry IN %s
            ORDER BY docentry, linenum
            """
        cr.execute(
            SQL(
                sql,
                tuple([delivery["docentry"] for delivery in deliveries]),
            )
        )
        delivery_lines = cr.dictfetchall()
        return delivery_lines
