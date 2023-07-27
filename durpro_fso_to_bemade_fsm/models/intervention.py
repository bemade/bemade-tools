from odoo import models, fields, api, Command
from .tools import converter


class Intervention(models.Model):
    _inherit = 'durpro_fso.intervention'

    converted = fields.Many2one('project.task')

    @converter
    def copy_as_fsm(self):
        return self.env['project.task'].create([{
            'name': r.name,
            'description': r.description,
            'planned_date_begin': r.date_planned,
            # Leave out parent_id since it's set by the work order side
            'equipment_ids': [Command.link(r.equipment_id.copy_as_fsm().id)],
            'partner_id': r.customer_id.id,
            'sequence': r.sequence,
            'child_ids': [Command.set(r.task_ids.copy_as_fsm().ids)],
            'stage_id': r._convert_state().id,
            'project_id': self.env.ref('industry_fsm.fsm_project').id,
            'tag_ids': [Command.set(self.env.ref(
                'durpro_fso_to_bemade_fsm.tag_converted_from_fso').ids)],
            'user_ids': [Command.set(r.work_order_id._convert_assignees_to_users().ids)],
        } for r in self])

    def _convert_state(self):
        if self.state == 'done':
            return self.env.ref('industry_fsm.planning_project_stage_3')  # Done
        elif self.state == 'bo':
            return self.env.ref('industry_fsm.planning_project_stage_4')  # Cancelled
        else:
            return self.env.ref('industry_fsm.planning_project_stage_0')  # New
