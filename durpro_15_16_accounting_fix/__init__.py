from . import models
from odoo import api, SUPERUSER_ID


def post_init(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['account.move.line'].fix()
    env['ir.module.module'].search([('name', '=', 'durpro_15_16_accounting_fix')]).write({'state': 'to remove'})
