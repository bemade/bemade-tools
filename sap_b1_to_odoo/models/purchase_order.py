from odoo import models, fields, api, Command
from odoo.sql_db import SQL
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator
import logging

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    sap_docentry = fields.Integer(index="btree")
    sap_docnum = fields.Integer(index="btree")

    _sql_constraints = [
        ("sap_docnum_unique", "UNIQUE(sap_docnum)", "Sap docnum must be unique!")
    ]


class SapPurchaseOrderImporter(models.AbstractModel):
    _name = "sap.purchase.order.importer"
    _description = "SAP Purchase Order Importer"
    _inherit = "sap.sale.purchase.importer.mixin"

    _products_dict = None

    @api.model
    def import_purchase_orders(self, cr):
        self._uppercase_all_cardcodes(cr)
        self._import_orders_and_rfqs(cr)

    @api.model
    def _uppercase_all_cardcodes(self, cr):
        cr.execute("UPDATE opqt SET cardcode = UPPER(cardcode)")
        cr.execute("UPDATE opor SET cardcode = UPPER(cardcode)")

    @api.model
    def _import_orders_and_rfqs(self, cr):
        order_pager = PagingIterator(
            cr,
            fetch_query="SELECT * from OPOR",
            count_query="SELECT count(*) from OPOR",
            limit=1000,
            orderby="docentry",
            logger=_logger,
        )
        where = """
        WHERE docentry not in (
        SELECT baseentry from por1
        WHERE basetype=20
        )
        """
        imported_docnums = tuple(self._get_imported_docnums())
        args = []
        if imported_docnums:
            where += " AND docnum not in %s"
            args = [imported_docnums]
        rfq_pager = PagingIterator(
            cr,
            fetch_query=f"SELECT * from OPQT {where}",
            fetch_args=args,
            count_query=f"SELECT count(*) from OPQT {where}",
            count_args=args,
            limit=1000,
            orderby="docentry",
            logger=_logger,
        )
        self._create_orders(
            cr, order_pager, "por1", "purchase.order", "purchase.order.line"
        )
        self._confirm_closed_orders(cr)
        self._confirm_open_orders(cr)
        self._create_orders(
            cr, rfq_pager, "pqt1", "purchase.order", "purchase.order.line"
        )
        self._cancel_canceled_orders_and_quotations(cr)

    @api.model
    def _get_imported_docnums(self):
        return self._get_imported_docnums_from_table("purchase_order")

    @api.model
    def _get_picking_policy(self, order):
        return "ship_partial" if order["partsupply"] == "Y" else "ship_complete"

    @api.model
    def _get_order_vals(self, sap_order_rows, sap_orders):
        order_rows_dict = {}
        for row in sap_order_rows:
            order_rows_dict.setdefault(row["docentry"], []).append(row)
        order_vals = []
        for order in sap_orders:
            partner = self._get_partner(order)
            terms = self._get_payment_terms(order["groupnum"])
            order_vals.append(
                {
                    "sap_docnum": order["docnum"],
                    "sap_docentry": order["docentry"],
                    "partner_id": partner.id,
                    # TODO: see if we need to do something with dest_address_id for drop ship
                    "payment_term_id": terms.id,
                    "date_order": order["docdate"].replace(tzinfo=None),
                    "date_planned": order["docduedate"].replace(tzinfo=None),
                    "notes": f"SAP Order {order['numatcard']}",
                    "shipping_policy_request": self._get_picking_policy(order),
                    "order_line": (
                        [
                            Command.create(self._get_row_vals(row))
                            for row in order_rows_dict[order["docentry"]]
                        ]
                        if order_rows_dict.get(order["docentry"])
                        else False
                    ),
                }
            )
        return order_vals

    @api.model
    def _confirm_closed_orders(self, cr):
        self._confirm_closed_orders_by_table("opor", "purchase.order")

    def _cancel_canceled_orders_and_quotations(self, cr):
        self._cancel_canceled_orders_and_quotations_by_table(
            cr, "ordr", "oqut", "purchase.order"
        )

    def _confirm_open_orders(self, cr):
        self._confirm_open_orders_by_table(cr, "opor", "purchase.order", "purchase")
