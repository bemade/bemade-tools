# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.modules.module import get_module_path


class IrModule(models.Model):
    _inherit = "ir.module.module"

    def _check_path_exist(self):
        for module in self:
            print(get_module_path(module.name, display_warning=False))
            module.path_exist = get_module_path(module.name, display_warning=False) or False
            if get_module_path(module.name, display_warning=False) or False:
                module.unlink()

    path_exist = fields.Boolean('Path exist', compute='_check_path_exist')

# class ../dur_pro/dur-pro/bemade-tools/modules_cleaner(models.Model):
#     _name = '../dur_pro/dur-pro/bemade-tools/modules_cleaner.../dur_pro/dur-pro/bemade-tools/modules_cleaner'
#     _description = '../dur_pro/dur-pro/bemade-tools/modules_cleaner.../dur_pro/dur-pro/bemade-tools/modules_cleaner'

#     name = fields.Char()
#     value = fields.Integer()
#     value2 = fields.Float(compute="_value_pc", store=True)
#     description = fields.Text()
#
#     @api.depends('value')
#     def _value_pc(self):
#         for record in self:
#             record.value2 = float(record.value) / 100
