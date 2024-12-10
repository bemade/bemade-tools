from odoo import models, fields, api, Command
from odoo.tools.sql import SQL
import logging
from datetime import datetime, timezone

utc = timezone.utc

_logger = logging.getLogger(__name__)


class ProductPricelist(models.Model):
    _inherit = "product.pricelist"

    sap_abs_id = fields.Integer(
        index="btree",
    )
    sap_loginstanc = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "sap_abs_id_loginstanc_unique",
            "UNIQUE(sap_abs_id, sap_loginstanc)",
            "sap_abs_id and sap_loginstance must be unique together",
        )
    ]


class ProductPricelistImporter(models.AbstractModel):
    _name = "sap.product.pricelist.importer"
    _description = "SAP Product Pricelist Importer"

    _products_dict = None
    _partners_dict = None

    @api.model
    def _get_products_dict(self, cr):
        product_dict = ProductPricelistImporter._products_dict
        if product_dict is None:
            sql = "SELECT distinct(itemcode) from AOA1"
            cr.execute(SQL(sql))
            itemcodes = [item[0] for item in cr.fetchall()]
            products = self.env["product.product"].search(
                [
                    ("sap_item_code", "in", itemcodes),
                    ("active", "in", [False, True]),
                ]
            )
            ProductPricelistImporter._products_dict = product_dict = {
                product.sap_item_code: product for product in products
            }
        return product_dict

    @api.model
    def _get_partners_dict(self, cr):
        partners_dict = ProductPricelistImporter._partners_dict
        if partners_dict is None:
            sql = "SELECT distinct (bpcode) from AOAT"
            cr.execute(SQL(sql))
            cardcodes = [item[0] for item in cr.fetchall()]
            partners = self.env["res.partner"].search(
                [
                    ("sap_card_code", "in", cardcodes),
                    ("active", "in", [True, False]),
                ]
            )
            ProductPricelistImporter._partners_dict = partners_dict = {
                partner.sap_card_code: partner for partner in partners
            }
        return partners_dict

    @api.model
    def import_all(self, cr):
        return self._import_all(cr)

    @api.model
    def _import_all(self, cr):
        _logger.info(f"Importing pricelists.")
        sap_blanket_orders = self._get_all_sap_blanket_orders(cr)
        sap_blanket_lines_dict = self._get_sap_blanket_lines_dict(cr)
        _logger.info(
            f"{len(sap_blanket_orders)} pricelists found. Loading products and partners."
        )
        products_dict = self._get_products_dict(cr)
        partners_dict = self._get_partners_dict(cr)
        _logger.info(f"Generating pricelist values...")
        pricelist_vals = self._get_pricelist_vals(
            sap_blanket_orders, sap_blanket_lines_dict, products_dict, partners_dict
        )
        _logger.info(f"Creating pricelists.")
        pricelists = self.env["product.pricelist"].create(pricelist_vals)
        _logger.info(
            f"{len(pricelists)} Product pricelists imported "
            f"with {len(pricelists.mapped('item_ids'))} lines."
        )
        self._set_partner_default_pricelists(
            sap_blanket_orders, pricelists, partners_dict
        )

    @api.model
    def _set_partner_default_pricelists(
        self,
        sap_blanket_orders,
        pricelists,
        partners_dict,
    ):
        now = datetime.now()
        pricelists_dict = {
            pricelist.sap_abs_id: pricelist
            for pricelist in (
                pricelists.mapped("item_ids")
                .filtered(lambda line: line.date_start <= now <= line.date_end)
                .mapped("pricelist_id")
            )
        }
        for blanket in sap_blanket_orders:
            partner = partners_dict[blanket["bpcode"]]
            pricelist = pricelists_dict.get(blanket["absid"])
            if pricelist and partner:
                partner.property_product_pricelist = pricelist

    @api.model
    def _get_all_sap_blanket_orders(self, cr):
        sql = "SELECT * from AOAT"
        cr.execute(SQL(sql))
        return cr.dictfetchall()

    @api.model
    def _get_sap_blanket_lines_dict(self, cr):
        sql = "SELECT * FROM AOA1"
        cr.execute(SQL(sql))
        lines = cr.dictfetchall()
        lines_dict = {}
        for line in lines:
            lines_dict.setdefault((line["agrno"], line["loginstanc"]), []).append(line)
        return lines_dict

    @api.model
    def _get_pricelist_vals(
        self, sap_blanket_orders, sap_blanket_lines_dict, products_dict, partners_dict
    ):
        vals = []
        for blanket in sap_blanket_orders:
            partner = partners_dict[blanket["bpcode"]]
            start = blanket["startdate"]
            end = blanket["enddate"]
            start_end = "-".join(
                [
                    datetime.strftime(start, "%Y-%m-%d"),
                    datetime.strftime(end, "%Y-%m-%d"),
                ]
            )
            start = start.astimezone(utc).replace(tzinfo=None)
            end = end.astimezone(utc).replace(tzinfo=None)
            active = datetime.now() <= end
            name = (
                (blanket["descript"] or partner.name)
                + " "
                + start_end
                + f" rev. {blanket['loginstanc']}"
            )
            currency_code = "USD" if blanket["bpcurr"] == "USD" else "CAD"
            currency = self.env["res.currency"].search([("name", "=", currency_code)])
            item_vals = self._extract_item_vals(
                sap_blanket_lines_dict[(blanket["absid"], blanket["loginstanc"])],
                products_dict,
                start,
                end,
            )
            vals.append(
                {
                    "sap_abs_id": blanket["absid"],
                    "sap_loginstanc": blanket["loginstanc"],
                    "name": name,
                    "active": active,
                    "currency_id": currency.id,
                    "item_ids": [Command.create(val) for val in item_vals],
                    "company_id": self.env.company.id,
                }
            )
        # Deactivate superceded lists. We do this after processing all the values since
        # we need to be able to check for matching abs_id but higher loginstanc
        vals_dict = {}
        for val in vals:
            vals_dict.setdefault(val["sap_abs_id"], []).append(val)
        for abs_id, dict_vals in vals_dict.items():
            dict_vals.sort(key=lambda val: val["sap_loginstanc"], reverse=True)
            for val in dict_vals[1:]:
                val.update({"active": False})
        return vals

    @api.model
    def _extract_item_vals(self, sap_blanket_lines, products_dict, start, end):
        vals = []
        for line in sap_blanket_lines:
            product = products_dict[line["itemcode"]]
            vals.append(
                {
                    "applied_on": "1_product",
                    "product_tmpl_id": product.product_tmpl_id.id,
                    "compute_price": "fixed",
                    "fixed_price": line["unitprice"],
                    "company_id": self.env.company.id,
                    "date_start": start,
                    "date_end": end,
                }
            )
        return vals
