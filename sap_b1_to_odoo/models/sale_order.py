from odoo import models, fields, Command, api
import logging
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator
from odoo.tools.sql import SQL

_logger = logging.getLogger(__name__)


class SalesOrder(models.Model):
    _inherit = "sale.order"

    sap_docentry = fields.Integer(index="btree")
    sap_docnum = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "sap_docnum_unique",
            "UNIQUE (sap_docnum)",
            "Another sale order with this docnum already exists",
        )
    ]


class SapSaleOrderImporter(models.AbstractModel):
    _name = "sap.sale.order.importer"
    _description = "SAP Sales Order Importer"
    _inherit = ["sap.sale.purchase.importer.mixin"]

    _sources_dict = None
    _confirmed_state = "sale"
    _sap_users_dict = None

    @classmethod
    def _get_user(cls, sap_slpcode):
        if cls._sap_users_dict is None:
            cls._sap_users_dict = {
                user.sap_slpcode: user.id
                for user in cls.env["res.users"].search(
                    [
                        ("sap_slpcode", "!=", False),
                        ("active", "in", [False, True]),
                    ]
                )
            }
        return cls._sap_users_dict.get(sap_slpcode, False)

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
    def _get_pricelist(self, sap_doccur):
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
        if sap_doccur == "USD":
            return usd_pricelist
        else:
            return cad_pricelist

    @api.model
    def _get_source(self, order):
        sources_dict = self.__class__._sources_dict
        if sources_dict is None:
            sources_dict = self.__class__._sources_dict = {
                source.name: source.id for source in self.env["utm.source"].search([])
            }
        return sources_dict.get(order["u_fcsdk_source"], False)

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

    # @api.model
    # def _get_payment_terms(self, sap_groupnum):
    #     terms = self.env["account.payment.term"].search([("sap_groupnum", "!=", False)])
    #     terms_dict = {term.sap_groupnum: term for term in terms}
    #     return terms_dict[sap_groupnum]

    @api.model
    def _uppercase_all_cardcodes(self, cr):
        """For some reason there is one record whose cardcode is lowercase but has an
        upper-case match in the ocrd table."""
        cr.execute("UPDATE ordr SET cardcode = UPPER(cardcode)")
        cr.execute("UPDATE oqut SET cardcode = UPPER(cardcode)")

    def _import_orders_and_quotations(self, cr):
        order_pager = PagingIterator(
            cr,
            fetch_query="select * from ordr",
            count_query="select count(*) " "from ordr",
            limit=1000,
            orderby="docentry",
            logger=_logger,
        )
        where = """
        where docentry not in (
        select baseentry from rdr1 where basetype = 23
        )
        """
        imported_docnums = tuple(self._get_imported_docnums())
        args = []
        if imported_docnums:
            where += " and docnum not in %s"
            args = [imported_docnums]

        quote_pager = PagingIterator(
            cr,
            fetch_query=f"select * from oqut {where}",
            fetch_args=args,
            count_query=f"select count(*) from oqut {where}",
            count_args=args,
            limit=1000,
            orderby="docentry",
            logger=_logger,
        )
        self._create_orders(cr, order_pager, "rdr1", "sale.order", "sale.order.line")
        self._confirm_closed_orders(cr)
        self._confirm_open_orders(cr)
        self._create_orders(cr, quote_pager, "qut1", "sale.order", "sale.order.line")
        self._cancel_canceled_orders_quotations(cr)

    def _get_order_vals(self, sap_order_rows, sap_orders):
        order_rows_dict = {}
        for row in sap_order_rows:
            order_rows_dict.setdefault(row["docentry"], []).append(row)
        order_vals = []
        for order in sap_orders:
            # If there's a contact set, we use it instead of the company to be precise

            partner = self._get_partner(order)
            if not partner:
                raise Exception(
                    f"Failed to find partner for order {order['docnum']}\n"
                    f"cntctcode: {order['cntctcode']}\n"
                    f"cardcode: {order['cardcode']}\n"
                )
            pricelist = self._get_pricelist(order["doccur"])
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
            terms = self._get_payment_terms(order["groupnum"])
            user = self._get_user(order["slpcode"])
            order_vals.append(
                {
                    "sap_docnum": order["docnum"],
                    "sap_docentry": order["docentry"],
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
                            Command.create(self._get_row_vals(row))
                            for row in order_rows_dict[order["docentry"]]
                        ]
                        if order_rows_dict.get(order["docentry"])
                        else False
                    ),
                    "source_id": self._get_source(order),
                    "user_id": user and user.id,
                }
            )
        return order_vals

    @api.model
    def _get_picking_policy(self, ordr):
        return "direct" if ordr["partsupply"] == "Y" else "direct"

    def _confirm_closed_orders(self, cr):
        self._confirm_closed_orders_by_table(cr, "ordr", "sale_order")

    def _cancel_canceled_orders_quotations(self, cr):
        self._cancel_canceled_orders_and_quotations_by_table(
            cr, "ordr", "oqut", "sale_order"
        )

    def _confirm_open_orders(self, cr):
        self._confirm_open_orders_by_table(cr, "ordr", "sale.order", "action_confirm")

    def _get_imported_docnums(self):
        return self._get_imported_docnums_from_table("sale_order")
