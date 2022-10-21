# -*- coding: utf-8 -*-

# from odoo import models, fields, api


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
