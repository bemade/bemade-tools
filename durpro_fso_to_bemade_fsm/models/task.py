from odoo import models, fields, api, Command
from .tools import converter


class Task(models.Model):
    _inherit = 'durpro_fso.task'

    converted = fields.Many2one('project.task')

    @converter
    def copy_as_fsm(self):
        return self.env['project.task'].create([{
            'name': r.description or r.name,
            'description': r.comments,
            'planned_hours': r.time_estimate,
            'stage_id': r._convert_state().id,
            'sequence': r.sequence,
            'tag_ids': [Command.set(self.env.ref(
                'durpro_fso_to_bemade_fsm.tag_converted_from_fso').ids)],
            'project_id': self.env.ref('industry_fsm.fsm_project').id,
            'user_ids': [Command.set(self.intervention_id.work_order_id.
                                     _convert_assignees_to_users().ids)],
            # We don't copy the intervention_id as parent_id to avoid infinite loop
        } for r in self])


    def _convert_state(self):
        if self.state == 'done':
            return self.env.ref('industry_fsm.planning_project_stage_3')  # Done
        elif self.state == 'bo':
            return self.env.ref('industry_fsm.planning_project_stage_4')  # Cancelled
        else:
            return self.env.ref('industry_fsm.planning_project_stage_0')  # New
