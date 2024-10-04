from odoo import models, fields, Command, api
import logging

_logger = logging.getLogger(__name__)


class SalesOrder(models.Model):
    _inherit = "sale.order"

    sap_docentry = fields.Integer(index="btree")
    sap_docnum = fields.Integer(index="btree")


class SapSaleOrderImporter(models.AbstractModel):
    _name = "sap.sale.order.importer"
    _description = "SAP Sales Order Importer"

    def _get_pricelist(self, sap_order):
        if not self.cad_pricelist:
            self.cad_pricelist = self.env["product.pricelist"].search(
                [("currency_id.name", "=", "CAD")]
            )
        if not self.usd_pricelist:
            self.usd_pricelist = self.env["product.pricelist"].search(
                [("currency_id.name", "=", "USD")]
            )
        if sap_order["doccur"] == "USD":
            return self.usd_pricelist
        else:
            return self.cad_pricelist

    def _get_partner(self, sap_order):
        if not self.partners_dict:
            partners = self.env["res.partner"].search(
                [
                    ("sap_card_code", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
            self.partners_dict = {
                partner.sap_card_code: partner for partner in partners
            }
        if not self.contacts_dict:
            contacts = self.env["res.partner"].search(
                [
                    ("sap_cntct_code", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
            self.contacts_dict = {
                contact.sap_cntct_code: contact for contact in contacts
            }
        if sap_order["cntctcode"]:
            return self.contacts_dict[sap_order["cntctcode"]]
        else:
            return self.partners_dict[sap_order["cardcode"]]

    @staticmethod
    def _find_partner_by_type(order, partner, address_type):
        # Try first to find a partner matching the address.
        address = order["address2"] if address_type == "delivery" else order["address"]
        potential_partners = (
            partner.commercial_partner_id | partner.commercial_partner_id.child_ids
        ).filtered(lambda partner: partner.street in address)
        if len(potential_partners) == 1:
            return potential_partners
        elif len(potential_partners) > 1:
            shipping_addresses = potential_partners.filtered(
                lambda partner: partner.type == address_type
            )
            if shipping_addresses:
                return shipping_addresses[0]
            if partner in potential_partners:
                return partner
            if partner.commercial_partner_id in potential_partners:
                return partner.commercial_partner_id
        else:
            return partner

    def _get_payment_terms(self, order):
        if not self.terms_dict:
            self.terms = self.env["account.payment.term"].search(
                [("sap_groupnum", "!=", False)]
            )
            self.terms_dict = {term.sap_groupnum: term for term in self.terms}
        return self.terms_dict[order["groupnum"]]

    def import_sales_orders(self, cr):
        terms = self._import_octg(cr)
        orders = self._import_ordr(cr)
        quotations = self._import_oqut(cr)

    def _import_ordr(self, cr):
        cr.execute("SELECT * FROM ordr")
        sap_orders = cr.dictfetchall()
        cr.execute("SELECT * FROM RDR1")
        sap_order_rows = cr.dictfetchall()
        _logger.info(
            f"Importing {len(sap_orders)} sales orders with "
            f"{len(sap_order_rows)} rows."
        )
        order_rows_dict = {}
        for row in sap_order_rows:
            order_rows_dict.setdefault(row["docentry"], []).append(row)

        order_vals = []
        for order in sap_orders:
            # If there's a contact set, we use it instead of the company to be precise
            partner = self._get_partner(order)
            pricelist = self._get_pricelist(order)
            partner_shipping_id = self._find_partner_by_type(order, partner, "delivery")
            partner_invoice_id = self._find_partner_by_type(order, partner, "invoice")
            terms = self._get_payment_terms(order)
            order_vals.append(
                {
                    "sap_docentry": order["docentry"],
                    "partner_id": partner.id,
                    "pricelist_id": pricelist.id,
                    "partner_invoice_id": partner_invoice_id.id,
                    "partner_shipping_id": partner_shipping_id.id,
                    "payment_term_id": terms.id,
                    "date_order": order["docdate"],
                    "commitment_date": order["docduedate"],
                    "client_order_ref": order["numatcard"],
                    "order_line": [
                        Command.create(
                            self._get_row_vals(row)
                            for row in order_rows_dict[order["docentry"]]
                        )
                    ],
                }
            )
        self.env["sale.order"].create(order_vals)

    def _get_row_vals(self, row):
        product = self._get_product(row["itemcode"])
        tax_ids = self._get_tax(row["vatprcnt"])
        vals = {
            "product_id": product.id,
            "product_uom_qty": row["quantity"],
            "price_unit": row["price"],
            "discount": row["discprscnt"],  # Likely problematic
            "tax_ids": None,
        }

    def _get_product(self, itemcode):
        if not self.products_dict:
            products = self.env["product.product"].search(
                [("sap_item_code", "!=", False), ("active", "in", [True, False])]
            )
            self.products_dict = {
                product.sap_item_code: product for product in products
            }
            return self.products_dict.get(itemcode)

    def _import_oqut(self, cr):
        pass

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
