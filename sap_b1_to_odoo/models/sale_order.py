from odoo import models, fields, Command, api
from odoo.modules.registry import Registry
from odoo.tools.sql import SQL
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator, fix_tz
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import logging
import os
from fuzzywuzzy import process, fuzz

workers = os.cpu_count() - 1

_logger = logging.getLogger(__name__)


class SalesOrder(models.Model):
    _inherit = "sale.order"

    sap_docentry = fields.Integer(index="btree", copy=False)
    sap_docnum = fields.Integer(index="btree", copy=False)
    sap_atcentry = fields.Integer(index="btree", copy=False)

    _sql_constraints = [
        (
            "sap_docentry_unique",
            "EXCLUDE USING btree (sap_docentry WITH =) WHERE (sap_docentry != 0)",
            "Another sale order with this docentry already exists when set",
        )
    ]


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"
    sap_line_num = fields.Integer(index="btree", copy=False)
    sap_aftlinenum = fields.Integer(index="btree", copy=False)
    sap_lineseq = fields.Integer(index="btree", copy=False)
    sap_docentry = fields.Integer(
        index="btree", related="order_id.sap_docentry", store=True, copy=False
    )
    sap_table = fields.Char(index="btree", copy=False)
    sap_qty_invoiced = fields.Float(copy=False)

    _sql_constraints = [
        (
            "sap_line_type_check",
            """CHECK(
                (sap_line_num != 0 AND sap_lineseq = 0 AND sap_aftlinenum = 0) OR  -- Product lines have line_num
                (sap_line_num = 0 AND sap_aftlinenum != 0 AND sap_lineseq != 0)     -- Text lines have aftlinenum and lineseq
            )""",
            "A line must have either a line_num (for product lines) or an aftlinenum (for text lines), but not both.",
        ),
        (
            "sap_line_docentry_table_unique",
            "UNIQUE(sap_line_num, sap_aftlinenum, sap_lineseq, sap_docentry, sap_table)",
            "Another line with this line number and docentry already exists for this SAP table.",
        ),
    ]

    @api.depends("invoice_lines.move_id.state", "invoice_lines.quantity")
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


class SapSaleOrderImporter(models.AbstractModel):
    _name = "sap.sale.order.importer"
    _description = "SAP Sales Order Importer"
    _inherit = "sap.sale.purchase.importer.mixin"

    # Configuration
    _sap_header_table = "ordr"
    _sap_lines_table = "rdr1"
    _sap_text_lines_table = "rdr10"
    _odoo_model = "sale.order"
    _odoo_table = "sale_order"
    _confirm_method = "action_confirm"
    _confirmed_state = "sale"
    _date_field = "date_order"
    _quantity_field = "qty_delivered"
    _quantity_method_field = "qty_delivered_method"
    _order_line_field = "sale_line_id"

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
        # self._import_utm_sources(cr)
        self._import_all(cr)

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
        def extract_address(partner):
            """Function to process a partner to get its address"""
            parts = ["street", "street2", "city", "state", "zip", "country"]
            address = " ".join(
                [
                    getattr(partner, part).strip()
                    for part in parts
                    if getattr(partner, part, False)
                ]
            )
            return address

        address = order["address2"] if address_type == "delivery" else order["address"]
        if address:
            address = address.replace("\r\n", " ")
        potential_partners = (
            partner.commercial_partner_id | partner.commercial_partner_id.child_ids
        ).filtered(lambda prt: prt.type == address_type)

        if len(potential_partners) > 1 and address:
            partner_addresses = {
                extract_address(prt): prt for prt in potential_partners
            }
            fuzzy_match = process.extractOne(address, partner_addresses.keys())[0]
            return partner_addresses[fuzzy_match]
        elif len(potential_partners) >= 1:
            return potential_partners[0]
        else:
            return partner.commercial_partner_id

    @api.model
    def _uppercase_all_cardcodes(self, cr):
        """For some reason there is one record whose cardcode is lowercase but has an
        upper-case match in the ocrd table."""
        cr.execute("UPDATE ordr SET cardcode = UPPER(cardcode)")
        cr.execute("UPDATE oqut SET cardcode = UPPER(cardcode)")

    def _import_all(self, cr):
        # First import orders
        imported_docnums = tuple(self._get_imported_docnums())
        _logger.info(f"Found {len(imported_docnums)} imported sales orders.")
        args = []
        where = ""
        if imported_docnums:
            where += "WHERE docnum not in %s"
            args = [imported_docnums]
        order_pager = PagingIterator(
            cr,
            fetch_query=f"select * from {self._sap_header_table} {where}",
            fetch_args=args,
            count_query=f"select count(*) from {self._sap_header_table} {where}",
            count_args=args,
            limit=500,
            orderby="docentry",
            logger=_logger,
        )
        _logger.info("Creating orders.")
        self._create_orders(cr, order_pager)
        _logger.info("Confirming closed orders (no picking).")
        self._confirm_closed_orders(cr)
        self._set_delivered_received_qty_for_closed_orders(cr)
        _logger.info("Confirming open orders.")
        self._confirm_open_orders(cr)
        _logger.info("Canceling canceled orders.")
        self._cancel_canceled_orders(cr)
        _logger.info("Recomputing delivery status for all orders.")
        self._recompute_delivery_status()
        _logger.info("Processing pickings that are partially shipped in SAP.")
        self._validate_pickings_with_sap_quantities(cr)
        _logger.info("Setting order dates.")
        self._set_order_dates(cr)
        self.env[self._odoo_model].flush_model()
        self.env.cr.commit()

    @api.model
    def init_pricelists(self):
        currencies = self.env["res.currency"].search([])
        for currency in currencies:
            curr_name = currency.name
            pricelist_name = f"Default {curr_name} Pricelist"
            if not self.env["product.pricelist"].search(
                [("name", "=", pricelist_name)]
            ):
                self.env["product.pricelist"].create(
                    {
                        "name": pricelist_name,
                        "currency_id": currency.id,
                        "company_id": self.env.company.id,
                    }
                )

    @api.model
    def _get_order_vals(self, sap_order_rows, sap_orders, sap_table):
        def _get_pricelists_dict():
            pricelists_dict = {}
            pricelists = self.env["product.pricelist"].search(
                [("name", "ilike", "Default%Pricelist")]
            )
            for pricelist in pricelists:
                pricelists_dict[pricelist.currency_id.name] = pricelist
            self.env["product.pricelist"].flush_model()
            self.env.cr.commit()
            return pricelists_dict

        def _get_pricelist(pricelists, doccur):
            # if doccur == "USD":
            #     return pricelists["USD"]
            # else:
            #     return pricelists["CAD"]
            if doccur in pricelists:
                return pricelists[doccur]
            return pricelists["USD"]

        def _get_carriers_dict():
            return {
                tpt.sap_trnspcode: tpt.delivery_carrier_id
                for tpt in self.env["sap.transporter"].search([])
            }

        pricelists = _get_pricelists_dict()
        partners_dict = self._get_partners_dict()
        contacts_dict = self._get_contacts_dict()
        sap_users_dict = self._get_sap_users_dict()
        sources_dict = self._get_sources_dict()
        carriers_dict = _get_carriers_dict()

        order_rows_dict = {}
        for row in sap_order_rows:
            order_rows_dict.setdefault(row["docentry"], []).append(row)

        order_vals = []
        products_dict = self._get_products_dict()
        terms_dict = self._get_payment_terms_dict()

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
            terms = terms_dict.get(order["groupnum"])
            user = sap_users_dict.get(order["slpcode"], False)
            # source = sources_dict.get(order["u_fcsdk_source"], False)
            carrier = carriers_dict.get(order["trnspcode"])
            rows = order_rows_dict.get(order["docentry"])
            row_vals = [
                Command.create(self._get_row_vals(row, products_dict, sap_table))
                for row in (rows or [])
                if row.get("itemcode") or row.get("linetext")
            ]
            vals = {
                "sap_docnum": order["docnum"],
                "sap_docentry": order["docentry"],
                "sap_atcentry": order["atcentry"],
                "partner_id": partner.id,
                "pricelist_id": pricelist.id,
                "partner_invoice_id": partner_invoice_id.id,
                "partner_shipping_id": partner_shipping_id.id,
                "payment_term_id": terms.id,
                "date_order": fix_tz(order["docdate"]),
                "commitment_date": fix_tz(order["docduedate"]),
                "client_order_ref": order["numatcard"] or "N/A",
                "picking_policy": self._get_picking_policy(order),
                "carrier_id": carrier and carrier.id,
                "order_line": row_vals,
                # "source_id": source,
                "user_id": user,
            }
            if order["docstatus"] == "C":
                vals["invoice_status"] = "invoiced"
            order_vals.append(vals)
        return order_vals

    @api.model
    def _get_picking_policy(self, ordr):
        return "direct" if ordr["partsupply"] == "Y" else "direct"

    def _add_procurement_groups_for_closed_orders(self, cr):
        _logger.info(f"Adding procurement groups for closed orders.")
        closed_orders = self._get_closed_orders(cr)
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

    @api.model
    def _recompute_delivery_status(self):
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE sale_order
            SET delivery_status = CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM sale_order_line
                    WHERE sale_order_line.order_id = sale_order.id
                      AND sale_order_line.product_uom_qty != sale_order_line.qty_delivered
                )
                THEN 'full'
                WHEN EXISTS (
                    SELECT 1 FROM sale_order_line
                    WHERE sale_order_line.order_id = sale_order.id
                      AND sale_order_line.qty_delivered > 0
                )
                THEN 'partial'
                ELSE 'pending'
            END
            WHERE sap_docentry IS NOT NULL
        """
        )
        self.env.cr.commit()
