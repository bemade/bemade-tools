from odoo import models, fields


class StockPicking(models.Model):
    _inherit = "stock.picking"

    sap_odln_docentry = fields.Integer(index="btree")
    sap_opdn_docentry = fields.Integer(index="btree")
    sap_docnum = fields.Integer(index="btree")
    sap_orders = fields.Text(index="trigram", compute="_compute_sap_orders")

    _sql_constraints = [
        (
            "sap_odln_docentry_unique",
            "UNIQUE(sap_odln_docentry)",
            "sap_odln_docentry must be unique",
        ),
        (
            "sap_opdn_docentry_unique",
            "UNIQUE(sap_opdn_docentry)",
            "sap_opdn_docentry must be unique",
        ),
        (
            "sap_docnum_odln_unique",
            "UNIQUE(sap_docnum, sap_odln_docentry)",
            "SAP docnum must be unique for each document type.",
        ),
        (
            "sap_docnum_opdn_unique",
            "UNIQUE(sap_docnum, sap_opdn_docentry)",
            "SAP docnum must be unique for each document type.",
        ),
    ]

    def _compute_sap_orders(self):
        for picking in self:
            orders = []
            sale = picking.sale_id and picking.sale_id.sap_docnum
            purchase = picking.purchase_id and picking.purchase_id.sap_docnum
            if sale:
                orders.append(sale.name)
            if purchase:
                orders.append(purchase.name)
            picking.sap_orders = ", ".join(orders) if orders else ""
