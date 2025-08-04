# -*- coding: utf-8 -*-

from odoo import models, fields


class ResUsers(models.Model):
    """Extend res.users to add Odoo 16 migration tracking field."""
    
    _inherit = 'res.users'
    
    odoo16_user_id = fields.Integer(
        string='Odoo 16 User ID',
        help='Original user ID from Odoo 16 database for migration tracking',
        index=True
    )
