# -*- coding: utf-8 -*-

from odoo import api, models


class IrModuleModule(models.Model):
    _inherit = 'ir.module.module'

    def button_immediate_upgrade(self):
        """Override to cleanup converted Studio views after module upgrade."""
        res = super().button_immediate_upgrade()
        self.env['ir.ui.view'].sudo().cleanup_converted_views()
        return res

    @api.model
    def update_list(self):
        """Override to cleanup converted Studio views after module list update."""
        res = super().update_list()
        self.env['ir.ui.view'].sudo().cleanup_converted_views()
        return res
