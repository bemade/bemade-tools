from odoo import models, fields, api, Command
import logging

_logger = logging.getLogger(__name__)


class MrpBom(models.Model):
    _inherit = "mrp.bom"

    sap_code = fields.Char(index="trigram")


class SapBomImporter(models.AbstractModel):
    _name = "sap.bom.importer"
    _description = "SAP BOM Importer"

    @api.model
    def import_boms(self, cr):
        _logger.info(f"Importing BOMs...")
        return self._import_boms(cr)

    def _import_boms(self, cr):
        cr.execute("SELECT * from OITT")
        sap_boms = cr.dictfetchall()
        cr.execute("SELECT * from ITT1")
        sap_bom_lines = cr.dictfetchall()
        sql = """
        SELECT code from oitt
        UNION
        SELECT code from itt1 
        """
        cr.execute(sql)
        concerned_codes = [rec[0] for rec in cr.fetchall()]
        odoo_products = self.env["product.product"].search(
            [("sap_item_code", "in", concerned_codes), ("active", "in", [False, True])]
        )
        odoo_products = {product.sap_item_code: product for product in odoo_products}
        bom_vals = []
        _logger.info(f"Importing {len(sap_boms)} BOMs...")
        for bom in sap_boms:
            bom_vals.append(
                {
                    "product_tmpl_id": odoo_products[bom["code"]].product_tmpl_id.id,
                    "product_qty": bom["qauntity"],
                    "type": "phantom" if bom["treetype"] == "A" else "normal",
                    "sap_code": bom["code"],
                }
            )
        boms = self.env["mrp.bom"].create(bom_vals)
        boms_by_code = {bom.sap_code: bom for bom in boms}
        component_vals = []
        _logger.info(f"Importing {len(sap_bom_lines)} BOM lines...")
        for line in sap_bom_lines:
            component_vals.append(
                {
                    "product_id": odoo_products[bom["code"]].product_variant_id.id,
                    "product_qty": line["quantity"],
                    "sequence": line["childnum"],
                    "bom_id": boms_by_code[line["father"]].id,
                }
            )
        self.env["mrp.bom.line"].create(component_vals)
        return boms

    def delete_all(self):
        self.env["mrp.bom"].search(
            [("sap_code", "!=", False), ("active", "in", [False, True])]
        ).unlink()
