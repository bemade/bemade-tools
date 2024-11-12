from odoo import models, fields, api, Command
from odoo.sql_db import SQL
import logging

_logger = logging.getLogger(__name__)


class SapSalePurchaseImporterMixin(models.AbstractModel):
    _name = "sap.sale.purchase.importer.mixin"

    _products_dict = None
    _contacts_dict = None
    _partners_dict = None
    _payment_terms_dict = None

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
    def _get_row_vals(self, row):
        product = self._get_product(row["itemcode"])
        # TODO: confirm tax_ids come in properly with fiscal positions
        # tax_ids = self._get_tax(row["vatprcnt"])
        return {
            "product_id": product.id,
            "product_uom_qty": row["quantity"],
            "price_unit": row["price"],
            "discount": row["discprcnt"],  # Likely problematic
            # "tax_ids": None,
        }

    @api.model
    def _get_product(self, itemcode):
        products_dict = SapSalePurchaseImporterMixin._products_dict
        if products_dict is None:
            products = self.env["product.product"].search(
                [("sap_item_code", "!=", False), ("active", "in", [True, False])]
            )
            SapSalePurchaseImporterMixin._products_dict = products_dict = {
                product.sap_item_code: product for product in products
            }
        return products_dict.get(itemcode)

    def _get_partner(self, sap_order):
        if sap_order["cntctcode"]:
            contacts_dict = self._get_contacts_dict()
            cntctcode = sap_order["cntctcode"]
            return contacts_dict.get(cntctcode)
        else:
            partners_dict = self._get_partners_dict()
            cardcode = sap_order["cardcode"]
            return (
                partners_dict.get(cardcode)
                or partners_dict.get(cardcode.upper())
                or partners_dict.get(cardcode.lower())
            )

    @api.model
    def _get_partners_dict(self):
        partners_dict = SapSalePurchaseImporterMixin._partners_dict
        if not partners_dict:
            partners = self.env["res.partner"].search(
                [
                    "|",
                    ("sap_card_code", "!=", False),
                    ("sap_cntct_code", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
            SapSalePurchaseImporterMixin._partners_dict = partners_dict = {
                partner.sap_card_code: partner for partner in partners
            }
        return partners_dict

    @api.model
    def _get_contacts_dict(self):
        contacts_dict = SapSalePurchaseImporterMixin._contacts_dict
        if not contacts_dict:
            contacts = self.env["res.partner"].search(
                [
                    ("sap_cntct_code", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
            SapSalePurchaseImporterMixin._contacts_dict = contacts_dict = {
                contact.sap_cntct_code: contact for contact in contacts
            }
        return contacts_dict

    @api.model
    def _get_payment_terms(self, sap_groupnum):
        terms_dict = SapSalePurchaseImporterMixin._payment_terms_dict
        if not terms_dict:
            terms = self.env["account.payment.term"].search(
                [("sap_groupnum", "!=", False)]
            )
            SapSalePurchaseImporterMixin._payment_terms_dict = terms_dict = {
                term.sap_groupnum: term for term in terms
            }
        return terms_dict[sap_groupnum]

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
        for sap_orders in pager:
            docentries = [order["docentry"] for order in sap_orders]
            query = SQL(
                "SELECT * FROM %s WHERE docentry in %s",
                SQL.identifier(lines_table),
                tuple(docentries),
            )
            cr.execute(query)
            sap_order_rows = cr.dictfetchall()
            _logger.info(
                f"Importing {len(sap_orders)} orders with "
                f"{len(sap_order_rows)} rows from {lines_table}."
            )
            _logger.info("Getting order vals...")
            order_vals = self._get_order_vals(sap_order_rows, sap_orders)
            _logger.info("Creating objects...")
            self.env[header_model].create(order_vals)
            _logger.info("Flushing to the database...")
            self.env[header_model].flush_model()
            self.env[lines_model].flush_model()

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
        if confirmed_orders:
            _logger.info(
                f"Marking {len(confirmed_orders)} orders as confirmed and closed "
                f"(no delivery order)."
            )
            sql = """
                    UPDATE %s set state='sale' WHERE sap_docnum in %s
                    """
            self.env.cr.execute(
                SQL(sql, SQL.identifier(odoo_table), tuple(confirmed_orders))
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
            SQL(sql), SQL.identifier(sap_order_table), SQL.identifier(sap_quote_table)
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
    def _confirm_open_orders_by_table(self, cr, sap_table, odoo_model, confirmed_state):
        """Mark confirmed orders that are open and confirmed in SAP. This is done
        separately due to the long runtime of confirming orders through the ORM."""
        sql = """
        SELECT docnum FROM %s
        WHERE canceled='N' and confirmed='Y' and invntsttus='O'
        """
        cr.execute(SQL(sql), SQL.identifier(sap_table))
        open_orders = [order[0] for order in cr.fetchall()]
        if open_orders:
            _logger.info(f"Confirming {len(open_orders)} open orders ...")
            self.env[odoo_model].search(
                [("sap_docnum", "in", open_orders)]
            ).state = confirmed_state


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
