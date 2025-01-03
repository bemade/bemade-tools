from odoo import models, fields, api, Command
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator
from odoo.tools.sql import SQL
import logging

_logger = logging.getLogger(__name__)


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
    _products_dict = None
    _carriers_dict = None

    @api.model
    def _get_carrier(self, trnspcode: str):
        carriers_dict = self.__class__._carriers_dict
        if not carriers_dict:
            sap_transporters = self.env["sap.transporter"].search([])
            carriers_dict = self.__class__._carriers_dict = {
                transporter.sap_trnspcode: transporter.delivery_carrier_id
                for transporter in sap_transporters
            }
        return carriers_dict.get(trnspcode)

    @api.model
    def _get_product(self, itemcode: str):
        products_dict = self.__class__._products_dict
        if not products_dict:
            products = self.env["product.product"].search(
                [["sap_item_code", "!=", False], ["active", "in", [True, False]]]
            )
            products_dict = self.__class__._products_dict = {
                product.sap_item_code: product for product in products
            }
        return products_dict.get(itemcode)

    @api.model
    def _get_sale_order(self, sap_docentry: int):
        sales_dict = self.__class__._sale_orders_dict
        if not sales_dict:
            orders = self.env["sale.order"].search([("sap_docentry", "!=", False)])
            sales_dict = self.__class__._sale_orders_dict = {
                order.sap_docentry: order for order in orders
            }
        return sales_dict.get(sap_docentry)

    @api.model
    def import_sale_pickings(self, cr):
        where = "WHERE canceled = 'N'"
        delivery_pager = PagingIterator(
            cr,
            fetch_query=f"SELECT * FROM odln {where}",
            count_query=f"SELECT count(*) FROM odln {where}",
            limit=1000,
            orderby="docentry",
            logger=_logger,
        )
        picking_vals = []
        for deliveries in delivery_pager:
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
            lines_dict = {}
            for line in delivery_lines:
                lines_dict.setdefault(line["docentry"], []).append(line)
            for delivery in deliveries:
                new_vals = self._get_picking_vals(
                    delivery,
                    lines_dict.get(delivery["docentry"], []),
                )
                if new_vals:
                    picking_vals.append(new_vals)

    @api.model
    def _get_picking_vals(self, delivery, lines):
        if not lines:
            return None
        name = f"SAP/OUT/{delivery['docnum']}"
        sale_order = self._get_sale_order(delivery["docentry"])
        warehouse = self.env["stock.warehouse"].search([], limit=1)
        type = warehouse.out_type_id
        sale_id = sale_order.id
        origin = sale_order.name
        sap_docentry = delivery["docentry"]
        sap_docnum = delivery["docnum"]
        date = delivery["docdate"]
        ship_date = delivery["docduedate"]
        carrier = self._get_carrier(delivery["trnspcode"])
        picking_vals = {
            "name": name,
            "origin": origin,
            "type": type.name,
            "location_id": type.default_location_dest_id.id,
            "location_dest_id": type.default_location_src_id.id,
            "partner_id": sale_order.partner_id.id,
            "sale_id": sale_id,
            "move_ids": [Command.create(self._get_move_vals(line)) for line in lines],
            "carrier_id": carrier.id if carrier else False,
        }

    @api.model
    def _get_move_vals(self, line):
        product_qty = line["quantity"]
        qty_open = line["openqty"]
        product_uom_qty = product_qty + qty_open
        product = self._get_product(line["itemcode"])
        if not product:
            raise Exception(
                f"Product {line['itemcode']} not found in Odoo. "
                "Please import products from SAP B1."
            )
        date = line["shipdate"]
        state = "done" if line["linestatus"] == "C" else "confirmed"

        def _get_move_line_vals():
            return {
                "product_id": product.id,
                "quantity": product_qty,
                "date": date,
            }

        return {
            "product_id": product.id,
            "product_uom_qty": product_uom_qty,
            "date": date,
            "picking_id": 1,
            "move_line_ids": [Command.create(_get_move_line_vals())],
            "state": state,
        }

    @api.model
    def import_puchase_pickings(self, cr):
        pass
