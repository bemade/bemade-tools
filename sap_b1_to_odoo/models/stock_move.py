import logging
import os
from concurrent.futures import ProcessPoolExecutor

import psycopg2.errors

from odoo import fields, models, api, Command
from odoo.modules.registry import Registry
from odoo.tools.sql import SQL

workers = os.cpu_count() - 1
_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = "stock.move"

    sap_docentry = fields.Integer(index="btree")
    sap_linenum = fields.Integer(index="btree")
    sap_source_table = fields.Char(index="btree")

    _sql_constraints = [
        (
            "sap_docentry_linenum_source_table_unique",
            "UNIQUE (sap_docentry, sap_linenum, sap_source_table)",
            "sap_docentry, sap_linenum, sap_source_table must be unique",
        )
    ]


class SapStockMoveImporter(models.AbstractModel):
    _name = "sap.stock.move.importer"
    _description = "SAP Stock Move Importer"

    @api.model
    def import_deliveries(self, cr):
        existing_deliveries = self._get_existing_deliveries()
        sap_deliveries = self._get_sap_deliveries(cr, existing_deliveries)
        products_dict = self._get_products_dict()
        order_lines_dict = self._get_order_lines_dict()
        self._multiprocess_deliveries(
            cr,
            self.env.uid,
            dict(self._context),
            products_dict,
            order_lines_dict,
            sap_deliveries,
        )

    @api.model
    def _multiprocess_deliveries(self, products_dict, order_lines_dict, sap_deliveries):
        chunksize = 500
        chunks = [
            sap_deliveries[i : i + chunksize]
            for i in range(0, len(sap_deliveries), chunksize)
        ]
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    self._process_deliveries,
                    self.env.cr.dbname,
                    self.env.uid,
                    dict(self.env.context),
                    products_dict,
                    order_lines_dict,
                    chunk,
                )
                for chunk in chunks
            ]
            for future in futures:
                future.result()

    @staticmethod
    def _process_deliveries(
        dbname, uid, context, products_dict, order_lines_dict, chunk
    ):
        tries = 1
        max_retries = 3
        while tries < max_retries:
            try:
                with Registry(dbname).cursor() as cr:
                    env = api.Environment(cr, uid, context)
                    self = env["sap.stock.move.importer"]
                    vals = self._get_delivery_vals(
                        chunk, products_dict, order_lines_dict
                    )
                    env["stock.move"].create(vals)
                    env.cr.commit()
                return
            except psycopg2.errors.SerializationFailure:
                if tries < max_retries:
                    tries += 1
                    continue
            except Exception as e:
                _logger.error(f"Subprocess failed with error {e}:", exc_info=True)
                raise e

    @api.model
    def _get_delivery_vals(self, chunk, products_dict, order_lines_dict):
        vals = []
        src = self.env["stock.warehouse"].browse(1).lot_stock_id.id
        dest = self.env.ref("stock.stock_location_customers").id
        for delivery in chunk:
            sale_line_id = order_lines_dict.get(delivery["docentry"]).get(
                delivery["linenum"]
            )
            product_id = products_dict.get(delivery["itemcode"])
            # Since these are completed moves we use the same quantity for both
            product_uom_qty = delivery["quantity"]
            product_qty = delivery["quantity"]
            vals.append(
                {
                    "name": "/".join([delivery["docentry"], delivery["linenum"]]),
                    "date": delivery["shipdate"],
                    "location_id": src,
                    "location_dest_id": dest,
                    "sale_line_id": sale_line_id,
                    "product_id": product_id,
                    "state": "done",
                    "sap_docentry": delivery["docentry"],
                    "sap_linenum": delivery["linenum"],
                    "sap_source_table": "dln1",
                    "move_line_ids": [
                        Command.create(
                            self._get_move_line_vals(
                                delivery,
                                src,
                                dest,
                                product_id,
                                product_uom_qty,
                                product_qty,
                            )
                        )
                    ],
                    "product_uom_qty": product_uom_qty,
                }
            )
        return vals

    @api.model
    def _get_move_line_vals(
        self, delivery, src, dest, product_id, product_uom_qty, product_qty
    ):
        return {
            "name": "/".join(
                [str(delivery.get("docentry")), str(delivery.get("linenum"))]
            ),
            "product_id": product_id,
            "location_id": src,
            "location_dest_id": dest,
            "product_uom_qty": product_uom_qty,
            "product_qty": product_qty,
            "state": "done",
        }

    @api.model
    def _get_sap_deliveries(self, cr, existing_deliveries):
        """
        Fetches completed SAP deliveries from the `dln1` database table.

        :param cr: Database cursor for executing SQL queries.
        :param existing_deliveries: List of `docentry` values to exclude from the query.
        :return: A list of dictionaries representing the SAP delivery records.
        """
        sql = """
            SELECT * 
            FROM dln1 
            WHERE basetype = 17 
              AND linestatus = 'C' 
        """
        if existing_deliveries:
            sql += " AND docentry NOT IN %s"
            cr.execute(SQL(sql, existing_deliveries))
        else:
            cr.execute(SQL(sql))
        return cr.dictfetchall()

    @api.model
    def _get_existing_deliveries(self):
        """
        Fetches existing delivery records from the `stock.move` model.

        :return: A list of dictionaries containing the delivery records with their `sap_docentry` field.
        """
        return self.env["stock.move"].search_read(
            [
                ("sap_docentry", "!=", False),
                ("sap_source_table", "=", "dln1"),
            ],
            ["sap_docentry"],
        )

    @api.model
    def _get_products_dict(self):
        return {
            row["sap_item_code"]: row["id"]
            for row in self.env["product.product"].search_read(
                [("sap_item_code", "!=", False)],
                ["sap_item_code", "id"],
            )
        }

    @api.model
    def _get_order_lines_dict(self):
        lines = self.env["sale.order.line"].search_read(
            [("sap_docentry", "!=", False)],
            ["sap_docentry", "sap_linenum", "id"],
        )
        lines_dict = {}
        for line in lines:
            lines_dict.setdefault(line["sap_docentry"], {}).update(
                {line["sap_linenum"]: line["id"]}
            )
        return lines_dict
