from odoo import models, fields, api, Command
from odoo.sql_db import SQL
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from odoo import registry

_logger = logging.getLogger(__name__)
workers = 8


class SapSalePurchaseImporterMixin(models.AbstractModel):
    _name = "sap.sale.purchase.importer.mixin"
    _description = "SAP Sale and Purchase Order Importer Mixin"

    def import_payment_terms(self, cr):
        self._import_octg(cr)

    def _import_octg(self, cr):
        """Import payment terms."""
        cr.execute("SELECT * from octg")
        sap_terms = cr.dictfetchall()
        vals = []
        for term in sap_terms:
            vals.append(
                {
                    "name": term["pymntgroup"],
                    "sap_groupnum": term["groupnum"],
                    "line_ids": [
                        Command.create(
                            {
                                "value_amount": 100.0,
                                "value": "percent",
                                "nb_days": term["extradays"],
                                "delay_type": "days_after",
                            }
                        )
                    ],
                }
            )
        return self.env["account.payment.term"].create(vals)

    @api.model
    def _get_row_vals(self, row, products_dict):
        product = products_dict.get(row["itemcode"])
        # TODO: confirm tax_ids come in properly with fiscal positions
        # tax_ids = self._get_tax(row["vatprcnt"])
        if product:
            return {
                "product_id": product.id,
                "product_uom_qty": row["quantity"] if row["quantity"] else 0.0,
                "price_unit": row["price"],
                "discount": row["discprcnt"],  # Likely problematic
                # "tax_ids": None,
            }
        else:
            # Some PO lines in SAP have no product linked, so we make a note in Odoo
            price = row["price"]
            discount = row["discprcnt"]
            quantity = row["quantity"] if row["quantity"] else 0.0
            price = f" {price} $" if price else ""
            discount = f" {discount}%" if discount else ""
            quantity = f" {quantity} x"
            return {
                "product_id": False,
                "product_uom_qty": 0.0,
                "display_type": "line_note",
                "name": f"{row['dscription']}{quantity}{price}{discount}",
            }

    @api.model
    def _get_products_dict(self):
        products = self.env["product.product"].search(
            [("sap_item_code", "!=", False), ("active", "in", [True, False])]
        )
        return {product.sap_item_code: product for product in products}

    @api.model
    def _get_partner(self, sap_order, contacts_dict, partners_dict):
        if sap_order["cntctcode"]:
            cntctcode = sap_order["cntctcode"]
            return contacts_dict.get(cntctcode)
        else:
            cardcode = sap_order["cardcode"]
            return (
                partners_dict.get(cardcode)
                or partners_dict.get(cardcode.upper())
                or partners_dict.get(cardcode.lower())
            )

    @api.model
    def _get_partners_dict(self):
        partners = self.env["res.partner"].search(
            [
                "|",
                ("sap_card_code", "!=", False),
                ("sap_cntct_code", "!=", False),
                ("active", "in", [False, True]),
            ]
        )
        return {partner.sap_card_code: partner for partner in partners}

    @api.model
    def _get_contacts_dict(self):
        contacts = self.env["res.partner"].search(
            [
                ("sap_cntct_code", "!=", False),
                ("active", "in", [False, True]),
            ]
        )
        return {contact.sap_cntct_code: contact for contact in contacts}

    @api.model
    def _get_payment_terms_dict(self):
        return {
            term.sap_groupnum: term
            for term in self.env["account.payment.term"].search(
                [("sap_groupnum", "!=", False)]
            )
        }

    def _get_imported_docnums_from_table(self, table):
        sql = SQL(
            """
        SELECT distinct(sap_docnum) from %s WHERE sap_docnum is not null
        """,
            SQL.identifier(table),
        )
        cr = self.env.cr
        cr.execute(sql)
        docnums = [order[0] for order in cr.fetchall()]
        return docnums

    def _create_orders(self, cr, pager, lines_table, header_model, lines_model):
        start_method = multiprocessing.get_start_method()
        multiprocessing.set_start_method("fork", force=True)
        chunks = [chunk for chunk in pager]
        try:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        self._sub_create_orders,
                        self._name,
                        self.env.cr.dbname,
                        self.env.uid,
                        dict(self.env.context),
                        header_model,
                        lines_model,
                        chunk,
                        self._get_lines(cr, lines_table, chunk),
                    )
                    for chunk in chunks
                ]
                for future in futures:
                    future.result()
        except Exception as e:
            _logger.error("An exception occurred in a subprocess.", exc_info=True)
            raise
        finally:
            multiprocessing.set_start_method(start_method, force=True)

    @staticmethod
    def _get_lines(cr, lines_table, sap_orders):
        docentries = [order["docentry"] for order in sap_orders]
        query = SQL(
            "SELECT * FROM %s WHERE docentry in %s",
            SQL.identifier(lines_table),
            tuple(docentries),
        )
        cr.execute(query)
        return cr.dictfetchall()

    @staticmethod
    def _sub_create_orders(
        importer_model,
        dbname,
        uid,
        context,
        header_model,
        lines_model,
        sap_orders,
        sap_order_rows,
    ):
        with registry(dbname).cursor() as cr:
            env = api.Environment(cr, uid, context)
            self = env[importer_model]
            _logger.info(
                f"Importing {len(sap_orders)} orders with "
                f"{len(sap_order_rows)} rows."
            )
            _logger.info("Getting order vals...")
            order_vals = self._get_order_vals(sap_order_rows, sap_orders)
            _logger.info("Creating objects...")
            env[header_model].create(order_vals)
            _logger.info("Flushing to the database...")
            env[header_model].flush_model()
            env[lines_model].flush_model()

    @api.model
    def _get_order_vals(self, sap_order_rows, sap_orders):
        raise NotImplementedError

    @api.model
    def _confirm_closed_orders_by_table(self, cr, sap_table, odoo_table):
        """Mark confirmed orders that are confirmed and closed in SAP. This does NOT
        create delivery orders as the confirmation is just flagged directly in the DB.
        """
        sql = """
        SELECT docnum from %s
        WHERE confirmed = 'Y' and invntsttus = 'C' and canceled = 'N'
        """
        cr.execute(SQL(sql, SQL.identifier(sap_table)))
        confirmed_orders = [order[0] for order in cr.fetchall()]
        state = getattr(self, "_confirmed_state")
        if confirmed_orders:
            _logger.info(
                f"Marking {len(confirmed_orders)} orders as confirmed and closed "
                f"(no delivery order)."
            )
            sql = """
                    UPDATE %s set state=%s WHERE sap_docnum in %s
                    """
            self.env.flush_all()
            self.env.cr.commit()
            self.env.cr.execute(
                SQL(
                    sql,
                    SQL.identifier(odoo_table),
                    state,
                    tuple(
                        confirmed_orders,
                    ),
                )
            )

    @api.model
    def _cancel_canceled_orders_and_quotations_by_table(
        self, cr, sap_order_table, sap_quote_table, odoo_table
    ):
        """Mark canceled orders as cancelled directly in the DB."""
        sql = """
        SELECT docnum FROM %s
        WHERE canceled = 'Y'
        UNION
        SELECT docnum FROM %s
        WHERE canceled = 'Y'
        """
        cr.execute(
            SQL(sql, SQL.identifier(sap_order_table), SQL.identifier(sap_quote_table))
        )
        canceled_orders = [order[0] for order in cr.fetchall()]
        if canceled_orders:
            _logger.info(f"Cancelling {len(canceled_orders)} cancelled orders ...")
            sql = """
                UPDATE %s set state='cancel' WHERE sap_docnum in %s
                """
            self.env.cr.execute(
                SQL(sql, SQL.identifier(odoo_table), tuple(canceled_orders))
            )

    @api.model
    def _confirm_open_orders_by_table(self, cr, sap_table, odoo_model, confirm_method):
        """Mark confirmed orders that are open and confirmed in SAP. This is done
        separately due to the long runtime of confirming orders through the ORM."""
        sql = """
        SELECT docnum FROM %s
        WHERE canceled='N' and confirmed='Y' and invntsttus='O'
        """
        cr.execute(SQL(sql, SQL.identifier(sap_table)))
        open_orders = [order[0] for order in cr.fetchall()]
        active_automations = self.env["base.automation"].search([("active", "=", True)])
        active_automations.active = False
        self.env["base.automation"].flush_model()
        if open_orders:
            self._sub_confirm_open_orders_by_table(
                self.env.cr.dbname,
                self.env.uid,
                dict(self.env.context),
                odoo_model,
                confirm_method,
                open_orders,
            )
        active_automations.active = True

    # @api.model
    # def _confirm_open_orders_by_table(self, cr, sap_table, odoo_model, confirm_method):
    #     """Mark confirmed orders that are open and confirmed in SAP. This is done
    #     separately due to the long runtime of confirming orders through the ORM."""
    #     sql = """
    #     SELECT docnum FROM %s
    #     WHERE canceled='N' and confirmed='Y' and invntsttus='O'
    #     """
    #     cr.execute(SQL(sql, SQL.identifier(sap_table)))
    #     open_orders = [order[0] for order in cr.fetchall()]
    #     if open_orders:
    #         _logger.info(f"Confirming {len(open_orders)} open orders ...")
    #         chunk_size = 50
    #         chunks = [
    #             open_orders[i : i + chunk_size]
    #             for i in range(0, len(open_orders), chunk_size)
    #         ]
    #         start_method = multiprocessing.get_start_method()
    #         multiprocessing.set_start_method("fork", force=True)
    #         max_workers = min(workers, len(chunks)) or 1
    #         try:
    #             with ProcessPoolExecutor(max_workers=max_workers) as executor:
    #                 futures = [
    #                     executor.submit(
    #                         self._sub_confirm_open_orders_by_table,
    #                         self.env.cr.dbname,
    #                         self.env.uid,
    #                         dict(self.env.context),
    #                         odoo_model,
    #                         confirm_method,
    #                         chunk,
    #                     )
    #                     for chunk in chunks
    #                 ]
    #                 for future in futures:
    #                     future.result()
    #         except Exception as e:
    #             _logger.error("An exception occurred in a subprocess.", exc_info=True)
    #             raise e
    #         finally:
    #             multiprocessing.set_start_method(start_method, force=True)
    #
    @staticmethod
    def _sub_confirm_open_orders_by_table(
        dbname, uid, context, odoo_model, confirm_method, sap_orders
    ):
        # try:
        with registry(dbname).cursor() as cr:
            env = api.Environment(cr, uid, context)
            self = env[odoo_model].search([("sap_docnum", "in", sap_orders)])
            recs = self.env[odoo_model].search(
                [
                    ("sap_docnum", "in", sap_orders),
                    ("state", "in", ["draft", "sent"]),
                ],
            )
            _logger.info(f"Confirming {len(recs)} open orders ...")
            for rec in recs:
                method = getattr(rec, confirm_method)
                method()

    # except Exception as e:
    #     _logger.error(
    #         "An exception occurred in _sub_confirm_open_orders_by_table.",
    #         exc_info=True,
    #     )
    #     raise e


class PaymentTerms(models.Model):
    _inherit = "account.payment.term"

    sap_groupnum = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "unique_sap_groupnum",
            "UNIQUE(sap_groupnum)",
            "A payment term with this SAP ID already exists.",
        )
    ]
