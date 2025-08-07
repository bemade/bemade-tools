from odoo import models, fields


class MailMessage(models.Model):
    _inherit = 'mail.message'

    odoo16_message_id = fields.Integer(
        string='Odoo 16 Message ID',
        help='Original message ID from Odoo 16 database for migration tracking',
        index=True
    )
