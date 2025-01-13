import psycopg2.errors

from odoo import models, fields, Command, api
from odoo.modules.registry import Registry
import logging
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator
from odoo.tools.sql import SQL
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
import os

_logger = logging.getLogger(__name__)


class SalesOrder(models.Model):
    _inherit = "sale.order"

    sap_docentry = fields.Integer(index="btree")
    sap_docnum = fields.Integer(index="btree")
    sap_atcentry = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "sap_docnum_unique",
            "UNIQUE (sap_docnum)",
            "Another sale order with this docnum already exists",
        )
    ]


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"
    sap_docentry = fields.Integer(
        index="btree",
        related="order_id.sap_docentry",
        store=True,
    )
    sap_linenum = fields.Integer(index="btree")
    sap_table = fields.Char(index="btree")

    _sql_constraints = [
        (
            "sap_linenum_docentry_table_unique",
            "UNIQUE (sap_linenum, sap_docentry, sap_table)",
            "Another sale order line with this linenum and docentry already exists for this SAP table.",
        )
    ]


class SapSaleOrderImporter(models.AbstractModel):
    _name = "sap.sale.order.importer"
    _description = "SAP Sales Order Importer"
    _inherit = ["sap.sale.purchase.importer.mixin"]

    _confirmed_state = "sale"

    @api.model
    def _get_sap_users_dict(self):
        return {
            user.sap_slpcode: user.id
            for user in self.env["res.users"].search(
                [
                    ("sap_slpcode", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
        }

    def import_sales_orders(self, cr):
        self._uppercase_all_cardcodes(cr)
        self._import_utm_sources(cr)
        self._import_orders_and_quotations(cr)

    @api.model
    def _import_utm_sources(self, cr):
        sql = (
            "SELECT DISTINCT u_fcsdk_source FROM ORDR "
            "WHERE u_fcsdk_source IS NOT null AND u_fcsdk_source <> ''"
        )
        cr.execute(SQL(sql))
        sources = cr.dictfetchall()
        sql = "SELECT DISTINCT name from utm_source"
        self.env.cr.execute(SQL(sql))
        existing_sources = set([source[0] for source in cr.fetchall()])
        vals_list = []
        for source in sources:
            if source["u_fcsdk_source"] not in existing_sources:
                vals_list.append(
                    {
                        "name": source["u_fcsdk_source"],
                    }
                )
        if vals_list:
            self.env["utm.source"].create(vals_list)
            self.env["utm.source"].flush_model()

    @api.model
    def _get_sources_dict(self):
        return {source.name: source.id for source in self.env["utm.source"].search([])}

    @staticmethod
    def _find_partner_by_type(order, partner, address_type):
        # Try first to find a partner matching the address.
        address = order["address2"] if address_type == "delivery" else order["address"]
        if not address:
            return partner
        potential_partners = (
            partner.commercial_partner_id | partner.commercial_partner_id.child_ids
        ).filtered(lambda prt: prt.street and prt.street in address)
        if len(potential_partners) == 1:
            return potential_partners
        elif len(potential_partners) > 1:
            shipping_addresses = potential_partners.filtered(
                lambda prt: prt.type == address_type
            )
            if shipping_addresses:
                return shipping_addresses[0]
            if partner in potential_partners:
                return partner
            if partner.commercial_partner_id in potential_partners:
                return partner.commercial_partner_id
        return partner

    @api.model
    def _uppercase_all_cardcodes(self, cr):
        """For some reason there is one record whose cardcode is lowercase but has an
        upper-case match in the ocrd table."""
        cr.execute("UPDATE ordr SET cardcode = UPPER(cardcode)")
        cr.execute("UPDATE oqut SET cardcode = UPPER(cardcode)")

    def _import_orders_and_quotations(self, cr):
        imported_docnums = tuple(self._get_imported_docnums())
        _logger.info(f"Found {len(imported_docnums)} imported sales orders.")
        args = []
        where = ""
        if imported_docnums:
            where += "WHERE docnum not in %s"
            args = [imported_docnums]
        order_pager = PagingIterator(
            cr,
            fetch_query=f"select * from ordr {where}",
            fetch_args=args,
            count_query=f"select count(*) from ordr {where}",
            count_args=args,
            limit=500,
            orderby="docentry",
            logger=_logger,
        )
        where = """
        where docentry not in (
        select baseentry from rdr1 where basetype = 23
        )
        """
        if imported_docnums:
            where += " and docnum not in %s"
            args = [imported_docnums]

        cr.execute("SELECT * FROM oqut ")
        quote_pager = PagingIterator(
            cr,
            fetch_query=f"select * from oqut {where}",
            fetch_args=args,
            count_query=f"select count(*) from oqut {where}",
            count_args=args,
            limit=500,
            orderby="docentry",
            logger=_logger,
        )
        _logger.info("Creating orders.")
        self._create_orders(cr, order_pager, "rdr1", "sale.order", "sale.order.line")
        _logger.info("Confirming closed orders.")
        self._confirm_closed_orders(cr)
        _logger.info("Confirming open orders.")
        self._confirm_open_orders(cr)
        _logger.info("Creating quotations.")
        self._create_orders(
            cr,
            quote_pager,
            "qut1",
            "sale.order",
            "sale.order.line",
            multiproc=False,
        )
        _logger.info("Canceling canceled orders and quotations.")
        self._cancel_canceled_orders_quotations(cr)

    @api.model
    def init_pricelists(self):
        cad_pricelist = self.env["product.pricelist"].search(
            [
                ("currency_id.name", "=", "CAD"),
                ("company_id", "=", self.env.company.id),
                ("name", "=", "Default CAD Pricelist"),
            ]
        )
        usd_pricelist = self.env["product.pricelist"].search(
            [
                ("currency_id.name", "=", "USD"),
                ("company_id", "=", self.env.company.id),
                ("name", "=", "Default USD Pricelist"),
            ]
        )
        if not cad_pricelist:
            cad_pricelist = self.env["product.pricelist"].create(
                {
                    "name": "Default CAD Pricelist",
                    "currency_id": self.env["res.currency"]
                    .search([("name", "=", "CAD")])
                    .id,
                }
            )
        if not usd_pricelist:
            usd_pricelist = self.env["product.pricelist"].create(
                {
                    "name": "Default USD Pricelist",
                    "currency_id": self.env["res.currency"]
                    .search([("name", "=", "USD")])
                    .id,
                }
            )

    @api.model
    def _get_order_vals(self, sap_order_rows, sap_orders, sap_table):

        def _get_pricelists_dict():
            cad_pricelist = self.env["product.pricelist"].search(
                [
                    ("currency_id.name", "=", "CAD"),
                    ("company_id", "=", self.env.company.id),
                    ("name", "=", "Default CAD Pricelist"),
                ]
            )
            usd_pricelist = self.env["product.pricelist"].search(
                [
                    ("currency_id.name", "=", "USD"),
                    ("company_id", "=", self.env.company.id),
                    ("name", "=", "Default USD Pricelist"),
                ]
            )
            pricelists_dict = {
                "CAD": cad_pricelist,
                "USD": usd_pricelist,
            }
            self.env["product.pricelist"].flush_model()
            self.env.cr.commit()
            return pricelists_dict

        def _get_pricelist(pricelists, doccur):
            if doccur == "USD":
                return pricelists["USD"]
            else:
                return pricelists["CAD"]

        pricelists = _get_pricelists_dict()
        partners_dict = self._get_partners_dict()
        contacts_dict = self._get_contacts_dict()
        sap_users_dict = self._get_sap_users_dict()
        sources_dict = self._get_sources_dict()

        order_rows_dict = {}
        for row in sap_order_rows:
            order_rows_dict.setdefault(row["docentry"], []).append(row)
        order_vals = []
        products_dict = self._get_products_dict()
        payment_terms_dict = self._get_payment_terms_dict()
        for order in sap_orders:
            # If there's a contact set, we use it instead of the company to be precise
            partner = self._get_partner(order, contacts_dict, partners_dict)
            if not partner:
                raise Exception(
                    f"Failed to find partner for order {order['docnum']}\n"
                    f"cntctcode: {order['cntctcode']}\n"
                    f"cardcode: {order['cardcode']}\n"
                )
            pricelist = _get_pricelist(pricelists, order["doccur"])
            partner_shipping_id = self._find_partner_by_type(
                order,
                partner,
                "delivery",
            )
            partner_invoice_id = self._find_partner_by_type(
                order,
                partner,
                "invoice",
            )
            terms = payment_terms_dict.get(order["groupnum"])
            user = sap_users_dict.get(order["slpcode"], False)
            source = sources_dict.get(order["u_fcsdk_source"], False)
            vals = {
                "sap_docnum": order["docnum"],
                "sap_docentry": order["docentry"],
                "sap_atcentry": order["atcentry"],
                "partner_id": partner.id,
                "pricelist_id": pricelist.id,
                "partner_invoice_id": partner_invoice_id.id,
                "partner_shipping_id": partner_shipping_id.id,
                "payment_term_id": terms.id,
                "date_order": order["docdate"].replace(tzinfo=None),
                "commitment_date": order["docduedate"].replace(tzinfo=None),
                "client_order_ref": order["numatcard"] or "N/A",
                "picking_policy": self._get_picking_policy(order),
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
                "source_id": source,
                "user_id": user,
            }
            if order["docstatus"] == "C":
                vals["invoice_status"] = "invoiced"
            order_vals.append(vals)
        return order_vals

    @api.model
    def _get_picking_policy(self, ordr):
        return "direct" if ordr["partsupply"] == "Y" else "direct"

    @api.model
    def _confirm_closed_orders(self, cr):
        self._confirm_closed_orders_by_table(cr, "ordr", "sale_order")
        self._set_delivered_qty_for_closed_orders(cr)

    @api.model
    def _set_delivered_qty_for_closed_orders(self, cr):
        closed_orders = self._get_closed_orders_by_table(cr, "ordr")
        orders = self.env["sale.order"].search(
            [
                ("sap_docnum", "in", closed_orders),
                ("order_line.qty_delivered_method", "!=", "manual"),
            ]
        )
        if not orders:
            return
        _logger.info(f"Setting delivered qty for {len(orders)} orders.")
        for order in orders:
            for line in order.order_line:
                line.qty_delivered_method = "manual"
                line.qty_delivered = line.product_uom_qty
        self.env.cr.commit()

    def _cancel_canceled_orders_quotations(self, cr):
        self._cancel_canceled_orders_and_quotations_by_table(
            cr, "ordr", "oqut", "sale_order"
        )

    def _confirm_open_orders(self, cr):
        self._confirm_open_orders_by_table(cr, "ordr", "sale.order", "action_confirm")

    def _get_imported_docnums(self):
        return self._get_imported_docnums_from_table("sale_order")

    def _add_procurement_groups_for_closed_orders(self, cr):
        _logger.info(f"Adding procurement groups for closed orders.")
        closed_orders = self._get_closed_orders_by_table(cr, "ordr")
        chunk_size = 500
        chunks = [
            closed_orders[i : i + chunk_size] for i in range(0, len(closed_orders), 100)
        ]

        start_method = multiprocessing.get_start_method()
        multiprocessing.set_start_method("fork", force=True)
        chunks_processed = 0
        total_chunks = len(chunks)
        try:
            with ProcessPoolExecutor(
                max_workers=multiprocessing.cpu_count() - 1
            ) as executor:
                futures = [
                    executor.submit(
                        self._subprocess_procurement_groups,
                        self.env.cr.dbname,
                        self.env.uid,
                        dict(self._context),
                        chunk,
                    )
                    for chunk in chunks
                ]
                for future in futures:
                    future.result()
                    chunks_processed += 1
                    _logger.info(
                        f"Processed {chunks_processed}/{total_chunks} chunks.\n"
                    )
            sql = SQL(
                """
            UPDATE sale_order
            SET procurement_group_id = matches.id
            FROM (
                SELECT sale_id, id
                FROM procurement_group
                WHERE sale_id is not null
                ) AS matches
            WHERE sale_order.id = matches.sale_id AND company_id = %s
            """,
                self.env.company.id,
            )
            self.env.cr.execute(sql)
            self.env.cr.commit()
            self.env.invalidate_all()
        except Exception as e:
            _logger.error("Subprocess failed: ", exc_info=True)
            raise e
        finally:
            multiprocessing.set_start_method(start_method, force=True)

    @staticmethod
    def _subprocess_procurement_groups(dbname, uid, context, sap_orders):
        try:
            with Registry(dbname).cursor() as cr:
                env = api.Environment(cr, uid, context)
                orders = env["sale.order"].search(
                    [
                        ("sap_docnum", "in", sap_orders),
                        ("procurement_group_id", "=", False),
                    ]
                )
                if not orders:
                    return
                procurement_vals = [
                    {
                        "name": order.name,
                        "move_type": order.picking_policy,
                        "sale_id": order.id,
                        "partner_id": order.partner_id.id,
                    }
                    for order in orders
                ]
                procs = env["procurement.group"].create(procurement_vals)
                _logger.info(f"Created { len(procs) } procurements.")
        except Exception as e:
            _logger.error(f"Subprocess {os.getpid()} failed: {e}.", exc_info=True)
            raise e
