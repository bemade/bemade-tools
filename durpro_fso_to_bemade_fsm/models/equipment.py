from odoo import api, fields, models, Command
from .tools import converter

class EquipmentTag(models.Model):
    _inherit = 'durpro_fso.equipment.tag'

    converted = fields.Many2one('bemade_fsm.equipment.tag')

    @converter
    def copy_as_fsm(self):
        return self.env['bemade_fsm.equipment.tag'].create([{
            'name': r.name,
            'color': r.color,
        } for r in self])


class Equipment(models.Model):
    _inherit = 'durpro_fso.equipment'

    converted = fields.Many2one('bemade_fsm.equipment')

    @converter
    def copy_as_fsm(self):
        res = self.env['bemade_fsm.equipment'].create([{
            'pid_tag': r.pid_tag,
            'name': r.name,
            'complete_name': r.complete_name,
            'tag_ids': [Command.set(r.tag_ids.copy_as_fsm().ids)],
            'partner_location_id': r.partner_location_id.id,
            'location_notes': r.location_notes,
            # task_ids left blank as set in the intervention
        } for r in self])
        # Locations with equipment should be of company type, convert them here
        self.mapped('converted').mapped('partner_location_id').\
            write({'company_type': 'company'})
        return res
