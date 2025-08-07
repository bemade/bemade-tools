from odoo import models, fields


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    odoo16_channel_id = fields.Integer(
        string='Odoo 16 Channel ID',
        help='Original channel ID from Odoo 16 database for migration tracking',
        index=True
    )
