from odoo import fields, models


class PurchaseRequisition(models.Model):
    _inherit = "purchase.requisition"

    sap_abs_id = fields.Integer(index="btree", copy=False)
    customer_ids = fields.Many2many(
        "res.partner",
        string="Customers",
        help="Customers associated with this blanket order",
    )
