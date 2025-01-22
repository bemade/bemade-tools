from odoo import models, fields, api, Command
from odoo.tools.sql import SQL
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator
import logging

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    sap_docentry = fields.Integer(index="btree", string="SAP Document Entry")
    sap_docnum = fields.Integer(index="btree", string="SAP Document Number")
    sap_atcentry = fields.Integer(index="btree")

    _sql_constraints = [
        ("sap_docnum_unique", "UNIQUE(sap_docnum)", "Sap docnum must be unique!")
    ]


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    sap_line_num = fields.Integer(index="btree")
    sap_aftlinenum = fields.Integer(index="btree")
    sap_lineseq = fields.Integer(index="btree")
    sap_docentry = fields.Integer(
        related="order_id.sap_docentry",
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


class SapPurchaseOrderImporter(models.AbstractModel):
    _name = "sap.purchase.order.importer"
    _description = "SAP Purchase Order Importer"
    _inherit = "sap.sale.purchase.importer.mixin"
    _confirmed_state = "purchase"

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
        imported_docnums = tuple(self._get_imported_docnums())
        where = ""
        args = []
        if imported_docnums:
            where = "WHERE docnum not in %s"
            args = [imported_docnums]
        order_pager = PagingIterator(
            cr,
            fetch_query=f"SELECT * from OPOR {where}",
            fetch_args=args,
            count_query=f"SELECT count(*) from OPOR {where}",
            count_args=args,
            limit=500,
            orderby="docentry",
            logger=_logger,
        )
        where = """
        WHERE docentry not in (
        SELECT baseentry from por1
        WHERE basetype=20
        )
        """
        if imported_docnums:
            where += "AND docnum not in %s"
        rfq_pager = PagingIterator(
            cr,
            fetch_query=f"SELECT * from OPQT {where}",
            fetch_args=args,
            count_query=f"SELECT count(*) from OPQT {where}",
            count_args=args,
            limit=500,
            orderby="docentry",
            logger=_logger,
        )
        self._create_orders(cr, order_pager, "por1", "purchase.order")
        self._confirm_closed_orders(cr)
        self._confirm_open_orders(cr)
        self._create_orders(cr, rfq_pager, "pqt1", "purchase.order")
        self._set_order_dates(cr, "purchase_order", "opor")
        self._set_order_dates(cr, "purchase_order", "opqt")
        self._cancel_canceled_orders_and_quotations(cr)
        self._recompute_receipt_status()

    @api.model
    def _get_imported_docnums(self):
        return self._get_imported_docnums_from_table("purchase_order")

    @api.model
    def _get_picking_policy(self, order):
        return "ship_partial" if order["partsupply"] == "Y" else "ship_complete"

    @api.model
    def _get_order_vals(self, sap_order_rows, sap_orders, sap_table):
        def _get_carriers_dict():
            return {
                tpt.sap_trnspcode: tpt.delivery_carrier_id
                for tpt in self.env["sap.transporter"].search([])
            }

        order_rows_dict = {}
        for row in sap_order_rows:
            order_rows_dict.setdefault(row["docentry"], []).append(row)
        order_vals = []
        contacts_dict = self._get_contacts_dict()
        partners_dict = self._get_partners_dict()
        products_dict = self._get_products_dict()
        terms_dict = self._get_payment_terms_dict()
        carriers_dict = _get_carriers_dict()
        for order in sap_orders:
            partner = self._get_partner(
                order, contacts_dict, partners_dict
            ).commercial_partner_id
            terms = terms_dict.get(order["groupnum"], False)
            carrier = carriers_dict.get(order["trnspcode"])
            order_date = order["docdate"].replace(tzinfo=None)
            vals = {
                "sap_docnum": order["docnum"],
                "sap_docentry": order["docentry"],
                "sap_atcentry": order["atcentry"],
                "partner_id": partner.id,
                "payment_term_id": terms and terms.id,
                "date_approve": order_date,
                "date_order": order_date,
                "date_planned": order["docduedate"].replace(tzinfo=None),
                "notes": f"SAP Order {order['numatcard']}",
                # "shipping_policy_request": self._get_picking_policy(order),
                "carrier_id": carrier and carrier.id,
                "order_line": (
                    [
                        Command.create(
                            self._get_row_vals(row, products_dict, sap_table)
                        )
                        for row in order_rows_dict[order["docentry"]]
                    ]
                    if order_rows_dict.get(order["docentry"])
                    else False
                ),
            }
            order_vals.append(vals)
        return order_vals

    def _mark_closed_orders_invoiced(self, cr):
        cr.execute("SELECT docentry FROM opor WHERE docstatus='C'")
        closed_orders = [order[0] for order in cr.fetchall()]
        self.env.cr.execute(
            SQL(
                """
        WITH orders AS (SELECT id FROM purchase_order WHERE sap_docentry IN %s)
        UPDATE purchase_order_line SET qty_invoiced=qty_received
        WHERE purchase_order_line.id IN (SELECT id FROM orders)
        """,
                tuple(closed_orders),
            )
        )
        self.env.cr.execute(
            SQL(
                """
        UPDATE purchase_order SET invoice_status = 'invoiced'
        WHERE docentry IN %s
        """,
                tuple(closed_orders),
            )
        )

    @api.model
    def _confirm_closed_orders(self, cr):
        self._confirm_closed_orders_by_table(
            cr, "opor", "purchase_order", "purchase.order"
        )
        self._set_received_quantity_on_closed_orders(cr)

    @api.model
    def _set_received_quantity_on_closed_orders(self, cr):
        closed_orders = self._get_closed_orders_by_table(cr, "opor")
        sql = """
        WITH orders AS (SELECT id FROM purchase_order WHERE sap_docnum IN %s)
        UPDATE purchase_order_line SET qty_received=product_uom_qty
        WHERE purchase_order_line.order_id IN (SELECT id FROM orders)
        """
        self.env.cr.execute(SQL(sql, tuple(closed_orders)))

        sql = """
        UPDATE purchase_order SET receipt_status = 'full', invoice_status = 'invoiced'
        WHERE sap_docnum IN %s
        """
        self.env.cr.execute(SQL(sql, tuple(closed_orders)))

        # orders = self.env["purchase.order"].search(
        #     [
        #         ("sap_docnum", "in", closed_orders),
        #         ("order_line.qty_received_method", "!=", "manual"),
        #     ]
        # )
        # for line in orders.order_line:
        #     line.qty_received_method = "manual"
        #     line.qty_received = line.product_uom_qty

    @api.model
    def _cancel_canceled_orders_and_quotations(self, cr):
        self._cancel_canceled_orders_and_quotations_by_table(
            cr, "ordr", "oqut", "purchase_order"
        )

    @api.model
    def _confirm_open_orders(self, cr):
        self._confirm_open_orders_by_table(
            cr, "opor", "purchase.order", "button_confirm"
        )

    def _recompute_receipt_status(self):
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE purchase_order
            SET receipt_status = CASE 
                WHEN NOT EXISTS (
                    SELECT 1 from purchase_order_line
                    WHERE purchase_order_line.order_id = purchase_order.id
                      AND purchase_order_line.product_uom_qty != purchase_order_line.qty_received
                )
                THEN 'full'
                WHEN EXISTS (
                    SELECT 1 FROM purchase_order_line
                    WHERE purchase_order_line.order_id = purchase_order.id
                      AND purchase_order_line.qty_received > 0
                )
                THEN 'partial'
                ELSE 'pending'
            END
            WHERE sap_docentry IS NOT NULL
            """
        )
        self.env.cr.commit()
