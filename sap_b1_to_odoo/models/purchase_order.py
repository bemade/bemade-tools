from odoo import models, fields, api, Command
from odoo.tools.sql import SQL
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator, fix_tz
import logging

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    sap_docentry = fields.Integer(
        index="btree", string="SAP Document Entry", copy=False
    )
    sap_docnum = fields.Integer(index="btree", string="SAP Document Number", copy=False)
    sap_atcentry = fields.Integer(index="btree", copy=False)

    _sql_constraints = [
        (
            "sap_docentry_unique",
            "EXCLUDE USING btree (sap_docentry WITH =) WHERE (sap_docentry != 0)",
            "SAP docentry must be unique when set!",
        )
    ]


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    sap_line_num = fields.Integer(
        index="btree",
        copy=False,
    )
    sap_aftlinenum = fields.Integer(
        index="btree",
        copy=False,
    )
    sap_lineseq = fields.Integer(
        index="btree",
        copy=False,
    )
    sap_docentry = fields.Integer(
        related="order_id.sap_docentry",
        store=True,
        index="btree",
        copy=False,
    )
    sap_table = fields.Char(
        index="btree",
        copy=False,
    )
    sap_qty_invoiced = fields.Float()

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

    @api.depends(
        "invoice_lines.move_id.state",
        "invoice_lines.quantity",
        "qty_received",
        "product_uom_qty",
        "order_id.state",
    )
    def _compute_qty_invoiced(self):
        super()._compute_qty_invoiced()
        # Pre-fetch quantities
        sap_lines = self.filtered("sap_qty_invoiced")
        # Prefetch the quantity instead of running one query per line later
        _ = sap_lines.invoice_lines.filtered("sap_docentry").mapped("quantity")
        for line in sap_lines:
            open_sap_qty = line.invoice_lines.filtered("sap_docentry").mapped(
                "quantity"
            )
            line.qty_invoiced += line.sap_qty_invoiced - sum(open_sap_qty)
            # Purchase order lines compute this second field right here
            line.qty_to_invoice -= line.sap_qty_invoiced


class SapPurchaseOrderImporter(models.AbstractModel):
    _name = "sap.purchase.order.importer"
    _description = "SAP Purchase Order Importer"
    _inherit = "sap.sale.purchase.importer.mixin"

    # Configuration
    _sap_header_table = "opor"
    _sap_lines_table = "por1"
    _sap_text_lines_table = "por10"
    _odoo_model = "purchase.order"
    _odoo_table = "purchase_order"
    _confirm_method = "button_confirm"
    _confirmed_state = "purchase"
    _date_field = "date_approve"
    _quantity_field = "qty_received"
    _quantity_method_field = "qty_received_method"
    _order_line_field = "purchase_line_id"

    @api.model
    def import_purchase_orders(self, cr):
        self._uppercase_all_cardcodes(cr)
        self._import_orders_and_rfqs(cr)

    @api.model
    def _uppercase_all_cardcodes(self, cr):
        cr.execute("UPDATE oprq SET cardcode = UPPER(cardcode)")
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
            fetch_query=f"SELECT * from {self._sap_header_table} {where}",
            fetch_args=args,
            count_query=f"SELECT count(*) from {self._sap_header_table} {where}",
            count_args=args,
            limit=500,
            orderby="docentry",
            logger=_logger,
        )
        _logger.info("Creating purchase orders.")
        self._create_orders(cr, order_pager)
        _logger.info("Confirming closed orders (no picking).")
        self._confirm_closed_orders(cr)
        _logger.info("Cancelling canceled orders.")
        self._cancel_canceled_orders(cr)
        _logger.info("Recomputing receipt status for all orders.")
        self._recompute_receipt_status()
        _logger.info("Setting delivered quantities for closed orders.")
        self._set_delivered_received_qty_for_closed_orders(cr)
        _logger.info("Confirming open orders.")
        self._confirm_open_orders(cr)
        _logger.info("Processing pickings that are partially shipped in SAP.")
        self._validate_pickings_with_sap_quantities(cr)
        _logger.info("Setting order dates.")
        self._set_order_dates(cr)
        self.env[self._odoo_model].flush_model()
        self.env.cr.commit()

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
            order_date = fix_tz(order["docdate"])
            vals = {
                "sap_docnum": order["docnum"],
                "sap_docentry": order["docentry"],
                "sap_atcentry": order["atcentry"],
                "partner_id": partner.id,
                "payment_term_id": terms and terms.id,
                "date_approve": order_date,
                "date_order": order_date,
                "date_planned": fix_tz(order["docduedate"]),
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
