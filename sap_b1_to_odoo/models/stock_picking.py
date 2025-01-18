from odoo import models, fields


class StockPicking(models.Model):
    _inherit = "stock.picking"

    sap_orders = fields.Text(index="trigram", compute="_compute_sap_orders")

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
