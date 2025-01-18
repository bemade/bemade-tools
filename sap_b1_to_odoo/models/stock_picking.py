from odoo import models, fields


class StockPicking(models.Model):
    _inherit = "stock.picking"

    sap_orders = fields.Text(index="trigram", compute="_compute_sap_orders")

    def _compute_sap_orders(self):
        for picking in self:
            orders = []
            sale = str(picking.sale_id and picking.sale_id.sap_docnum)
            purchase = str(picking.purchase_id and picking.purchase_id.sap_docnum)
            if sale:
                orders.append(sale)
            if purchase:
                orders.append(purchase)
            picking.sap_orders = ", ".join(orders) if orders else ""
