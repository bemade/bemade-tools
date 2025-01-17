from odoo import models, fields, api, Command
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

    sap_linenum = fields.Integer(index="btree")
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
            "sap_docentry_linenum_table_unique",
            "UNIQUE(sap_docentry, sap_linenum, sap_table)",
            "Sap docentry and linenum must be unique per sap table!",
        )
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
        args = []
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
        self._create_orders(
            cr, order_pager, "por1", "purchase.order", "purchase.order.line"
        )
        self._confirm_closed_orders(cr)
        self._confirm_open_orders(cr)
        self._create_orders(
            cr, rfq_pager, "pqt1", "purchase.order", "purchase.order.line"
        )
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
        company_partner = self.env.company.partner_id
        for order in sap_orders:
            partner = self._get_partner(order, contacts_dict, partners_dict)
            terms = terms_dict.get(order["groupnum"], False)
            carrier = carriers_dict.get(order["trnspcode"])
            company_has_account = (
                carrier
                and carrier
                in company_partner.carrier_account_ids.mapped("delivery_carrier_id")
            )
            billing_mode = "collect" if company_has_account else "ppc"

            vals = {
                "sap_docnum": order["docnum"],
                "sap_docentry": order["docentry"],
                "sap_atcentry": order["atcentry"],
                "partner_id": partner.id,
                "payment_term_id": terms and terms.id,
                "date_order": order["docdate"].replace(tzinfo=None),
                "date_planned": order["docduedate"].replace(tzinfo=None),
                "notes": f"SAP Order {order['numatcard']}",
                # "shipping_policy_request": self._get_picking_policy(order),
                "carrier_id": carrier and carrier.id,
                "delivery_billing_mode": billing_mode,
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
            if order["docstatus"] == "C":
                vals["invoice_status"] = "invoiced"
            order_vals.append(vals)
        return order_vals

    @api.model
    def _confirm_closed_orders(self, cr):
        self._confirm_closed_orders_by_table(cr, "opor", "purchase_order")
        self._set_received_quantity_on_closed_orders(cr)

    @api.model
    def _set_received_quantity_on_closed_orders(self, cr):
        closed_orders = self._get_closed_orders_by_table(cr, "opor")
        orders = self.env["purchase.order"].search(
            [
                ("sap_docnum", "in", closed_orders),
                ("order_line.qty_received_method", "!=", "manual"),
            ]
        )
        for line in orders.order_line:
            line.qty_received_method = "manual"
            line.qty_received = line.product_uom_qty

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

    @api.model
    def _get_row_vals(self, row, products_dict, sap_table):
        vals = super()._get_row_vals(row, products_dict, sap_table)
        vals.update(
            product_qty=vals["product_uom_qty"],
        )
        return vals

    def _recompute_receipt_status(self):
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
