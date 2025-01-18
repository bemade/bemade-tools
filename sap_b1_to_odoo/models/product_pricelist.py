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
    sap_listnum = fields.Integer(index="btree")  # ID in OPLN table
    _sql_constraints = [
        (
            "sap_abs_id_loginstanc_unique",
            "UNIQUE(sap_abs_id, sap_loginstanc)",
            "sap_abs_id and sap_loginstance must be unique together",
        )
    ]

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        _logger.info(f"Created {len(res)} pricelists.")
        return res


class ProductPricelistImporter(models.AbstractModel):
    _name = "sap.product.pricelist.importer"
    _description = "SAP Product Pricelist Importer"

    _products_dict = None
    _partners_dict = None

    @api.model
    def _get_products_dict(self, cr):
        product_dict = ProductPricelistImporter._products_dict
        if product_dict is None:
            sql = "SELECT distinct(itemcode) from OAT1"
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
            sql = "SELECT distinct (bpcode) from OOAT"
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
    def _get_sap_basic_pricelists(self, cr):
        sql = "SELECT * FROM opln"
        cr.execute(SQL(sql))
        return cr.dictfetchall()

    @api.model
    def import_all(self, cr):
        return self._import_all(cr)

    @api.model
    def _import_all(self, cr):
        _logger.info(f"Importing pricelists.")
        self._import_basic_pricelists(cr)
        sap_blanket_orders = self._get_all_sap_blanket_orders(cr)
        sap_blanket_lines_dict = self._get_sap_blanket_lines_dict(cr)
        products_dict = self._get_products_dict(cr)
        partners_dict = self._get_partners_dict(cr)
        pricelist_vals = self._get_pricelist_vals(
            sap_blanket_orders, sap_blanket_lines_dict, products_dict, partners_dict
        )
        pricelists = self.env["product.pricelist"].create(pricelist_vals)
        _logger.info(
            f"{len(pricelists)} Product pricelists imported "
            f"with {len(pricelists.mapped('item_ids'))} lines."
        )
        _logger.info("Setting default partner pricelists.")
        self._set_partner_default_pricelists(
            sap_blanket_orders, pricelists, partners_dict
        )
        self._set_usd_pricelist_partners(cr)
        _logger.info("Importing purchase blankets.")
        purchase_blanket_vals = self._get_purchase_blanket_vals(
            sap_blanket_orders, sap_blanket_lines_dict, products_dict, partners_dict
        )
        blankets = self.env["purchase.requisition"].create(purchase_blanket_vals)
        _logger.info(f"Imported {len(blankets)} purchase blankets.")

    @api.model
    def _set_usd_pricelist_partners(self, cr):
        cr.execute("SELECT cardcode FROM ocrd WHERE currency = 'USD'")
        cardcodes = [item[0] for item in cr.fetchall()]
        cad_pricelist = self.env["product.pricelist"].search(
            [("name", "=", "Default CAD Pricelist")]
        )
        usd_pricelist = self.env["product.pricelist"].search(
            [("name", "=", "Default USD Pricelist")]
        )
        odoo_partners = self.env["res.partner"].search(
            [
                ("sap_card_code", "in", cardcodes),
                ("active", "in", [True, False]),
            ]
        )
        odoo_partners = odoo_partners.filtered(
            lambda partner: partner.property_product_pricelist == cad_pricelist
        )
        odoo_partners.write({"property_product_pricelist": usd_pricelist.id})

    @api.model
    def _import_basic_pricelists(self, cr):
        """For simple pricelists in the OPLN table, we just import the name and the
        linenum so that we can later reference them. Each pricelist that isn't the base
        pricelist gets one line, simply using the main pricelist as its base. This is
        done so that we can associate clients to the pricelists and then apply the
        discount levels appropriately after import via manual config.

        The public pricelist gets renamed to the pricelist with ID 1 ("END USER") and
        gets its linenum set."""
        basic_lists = self._get_sap_basic_pricelists(cr)
        public_pricelist = self.env["product.pricelist"].search(
            [
                ("name", "ilike", "public"),
                ("currency_id", "=", self.env.ref("base.CAD").id),
            ]
        )
        for pricelist in basic_lists:
            if pricelist["listnum"] == 1:
                public_pricelist.name = pricelist["listname"]
                public_pricelist.sap_listnum = pricelist["listnum"]
            else:
                self.env["product.pricelist"].create(
                    {
                        "sap_listnum": pricelist["listnum"],
                        "name": pricelist["listname"],
                        "item_ids": [
                            Command.create(
                                {
                                    "applied_on": "3_global",
                                    "base": "pricelist",
                                    "base_pricelist_id": public_pricelist.id,
                                }
                            )
                        ],
                    }
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
                .filtered(
                    lambda line: (not line.date_start or line.date_start <= now)
                    and (not line.date_end or now <= line.date_end)
                )
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
        sql = "SELECT * from OOAT"
        cr.execute(SQL(sql))
        return cr.dictfetchall()

    @api.model
    def _get_sap_blanket_lines_dict(self, cr):
        sql = "SELECT * FROM OAT1"
        cr.execute(SQL(sql))
        lines = cr.dictfetchall()
        lines_dict = {}
        for line in lines:
            lines_dict.setdefault(line["agrno"], []).append(line)
        return lines_dict

    @api.model
    def _get_pricelist_vals(
        self, sap_blanket_orders, sap_blanket_lines_dict, products_dict, partners_dict
    ):
        vals = []
        for blanket in sap_blanket_orders:
            partner = partners_dict[blanket["bpcode"]]
            # Don't set these up for suppliers
            if partner.sap_partner_type == "S":
                continue
            start = blanket["startdate"]
            end = blanket["enddate"]
            start = start.astimezone(utc).replace(tzinfo=None)
            end = end.astimezone(utc).replace(tzinfo=None)
            active = datetime.now() <= end
            name = blanket["descript"] or partner.name
            currency_code = "USD" if blanket["bpcurr"] == "USD" else "CAD"
            currency = self.env["res.currency"].search([("name", "=", currency_code)])
            item_vals = self._extract_item_vals(
                sap_blanket_lines_dict[blanket["absid"]],
                products_dict,
                start,
                end,
            )
            # Add the final line to refer back to the base pricelist for all other products
            item_vals += [
                {
                    "applied_on": "3_global",
                    "base": "pricelist",
                    "base_pricelist_id": partner.property_product_pricelist.id,
                }
            ]
            vals.append(
                {
                    "sap_abs_id": blanket["absid"],
                    "name": partner.name + " - " + name,
                    "active": active,
                    "currency_id": currency.id,
                    "item_ids": [Command.create(val) for val in item_vals],
                    "company_id": self.env.company.id,
                }
            )
        return vals

    def _get_purchase_blanket_vals(
        self, sap_blanket_orders, sap_blanket_lines_dict, products_dict, partners_dict
    ):
        def _get_status(blanket):
            match blanket["status"]:
                case "A" | "X" | "P":
                    return "confirmed"
                case "B" | "D" | "F":
                    return "draft"
                case "T":
                    return "done"
                case "C":
                    return "cancel"

        vals = []
        for blanket in sap_blanket_orders:
            partner = partners_dict.get(blanket["bpcode"])
            # Only make these for suppliers
            if partner.sap_partner_type != "S":
                continue
            start = blanket["startdate"]
            end = blanket["enddate"]
            start = start.astimezone(utc).replace(tzinfo=None)
            end = end.astimezone(utc).replace(tzinfo=None)
            reference = blanket["descript"]
            status = _get_status(blanket)
            currency_code = "USD" if blanket["bpcurr"] == "USD" else "CAD"
            currency = self.env["res.currency"].search([("name", "=", currency_code)])
            item_vals = self._extract_blanket_item_vals(
                sap_blanket_lines_dict[blanket["absid"]],
                products_dict,
            )
            vals.append(
                {
                    "vendor_id": partner.id,
                    "reference": reference,
                    "date_start": start,
                    "date_end": end,
                    "state": status,
                    "currency_id": currency.id,
                    "line_ids": [Command.create(val) for val in item_vals],
                    "requisition_type": "blanket_order",
                }
            )
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

    @api.model
    def _extract_blanket_item_vals(self, sap_blanket_lines, products_dict):
        vals = []
        for line in sap_blanket_lines:
            product = products_dict[line["itemcode"]]
            quantity = line["planqty"]
            price = line["unitprice"]
            vals.append(
                {
                    "product_id": product.id,
                    "product_qty": quantity,
                    "price_unit": price,
                }
            )
        return vals
