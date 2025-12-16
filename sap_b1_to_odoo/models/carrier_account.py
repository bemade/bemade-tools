from odoo import fields, models


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    sap_transporter_ids = fields.One2many(
        comodel_name="sap.transporter",
        inverse_name="delivery_carrier_id",
    )


class SapTransporter(models.Model):
    _name = "sap.transporter"
    _description = "SAP Transporter"

    sap_trnspcode = fields.Integer()
    delivery_carrier_id = fields.Many2one("delivery.carrier")
