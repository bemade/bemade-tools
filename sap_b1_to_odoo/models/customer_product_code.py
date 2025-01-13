from odoo import fields, models, api, _
from odoo.tools.sql import SQL
import logging
import psycopg2

_logger = logging.getLogger(__name__)


class SapCustomerProductCodeImporter(models.AbstractModel):
    """Sap Customer Product Code Importer for importing from the OSCN table."""

    _name = "sap.customer.product.code.importer"
    _description = "Sap Customer Product Code Importer"

    def import_customer_product_codes(self, cr):
        cr.execute("SELECT * FROM OSCN")
        sap_codes = cr.dictfetchall()
        _logger.info(f"Importing {len(sap_codes)} customer product codes...")
        product_dict = self._get_products_dict(sap_codes)
        partners_dict = self._get_partners_dict(sap_codes)
        vals = self._get_values(partners_dict, product_dict, sap_codes)
        codes = self.env["product.customer.code"].create(vals)
        _logger.info(f"{len(codes)} customer product codes imported.")

    def _get_products_dict(self, sap_codes):
        item_codes = tuple(code["itemcode"] for code in sap_codes)
        sql = SQL(
            "SELECT id, sap_item_code FROM product_product WHERE sap_item_code in %s",
            item_codes,
        )
        self.env.cr.execute(sql)
        return {row["sap_item_code"]: row["id"] for row in self.env.cr.dictfetchall()}

    def _get_partners_dict(self, sap_codes):
        cardcodes = tuple(code["cardcode"] for code in sap_codes)
        sql = SQL(
            "SELECT id, sap_card_code FROM res_partner WHERE sap_card_code in %s",
            cardcodes,
        )
        self.env.cr.execute(sql)
        return {row["sap_card_code"]: row["id"] for row in self.env.cr.dictfetchall()}

    def _get_values(self, partners_dict, products_dict, sap_codes):
        vals = []
        for code in sap_codes:
            try:
                product_id = products_dict.get(code["itemcode"])
                partner_id = partners_dict.get(code["cardcode"])
                vals.append(
                    {
                        "product_id": product_id,
                        "partner_id": partner_id,
                        "company_id": self.env.company.id,
                        "product_code": code["substitute"],
                    }
                )
            except psycopg2.errors.UniqueViolation:
                _logger.warning(
                    f"Skipping duplicate customer product code: {code.substitute}"
                    f" for partner ID {code["cardcode"]}"
                )
                continue
        return vals
