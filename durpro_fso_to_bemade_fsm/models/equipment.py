from odoo import api, fields, models, Command
from .tools import converter

class EquipmentTag(models.Model):
    _inherit = 'durpro_fso.equipment.tag'

    converted = fields.Many2one('bemade_fsm.equipment.tag')

    def copy_as_fsm(self):
        # Don't duplicate existing tags. Using the @converted annotation won't work here if tags have been manually
        # created with the same name, as old FSO ones, so we do this manually.
        new_tags = self.env['bemade_fsm.equipment.tag'].search([('name', 'in', self.mapped('name'))])
        new_tags |= self.env['bemade_fsm.equipment.tag'].create([{
            'name': r.name,
            'color': r.color,
        } for r in self.filtered(lambda tag: tag.name not in new_tags.mapped("name"))])
        return new_tags


class Equipment(models.Model):
    _inherit = 'durpro_fso.equipment'

    converted = fields.Many2one('bemade_fsm.equipment')

    @converter
    def copy_as_fsm(self):
        res = self.env['bemade_fsm.equipment']
        for r in self:
            rec = self.env['bemade_fsm.equipment'].create({
                'pid_tag': r.pid_tag,
                'name': r.name,
                'complete_name': r.complete_name,
                'tag_ids': [Command.set(r.tag_ids.copy_as_fsm().ids)],
                'partner_location_id': r.partner_location_id.id,
                'location_notes': r.location_notes,
                # task_ids left blank as set in the intervention
            })
            attachments = self.env['ir.attachment'].search([('res_model', '=', 'durpro_fso.equipment'),
                                                            ('res_id', '=', r.id)])
            attachments.sudo().write({'res_model': 'bemade_fsm.equipment', 'res_id': rec.id})
            r.message_change_thread(rec)
            res |= rec

        # Locations with equipment should be of company type, convert them here
        self.mapped('converted').mapped('partner_location_id').\
            write({'company_type': 'company'})
        return res
