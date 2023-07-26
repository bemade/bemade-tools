from odoo import models, fields, api, Command
from .tools import converter


class WorkOrder(models.Model):
    _inherit = 'durpro_fso.work_order'

    converted = fields.Many2one('project.task')
    active = fields.Boolean()

    def action_convert_to_fsm(self):
        tasks = self.copy_as_fsm()
        self._copy_actual_times_as_timesheets()
        self.active = False
        return {
            'view_mode': 'kanban',
            'res_model': 'project.project',
            'res_id': self.env.ref('industry_fsm.fsm_project').id,
            'type': 'ir.actions.act_window',
            'context': {'search_default_id': tasks.ids},
            'target': 'main',
        }

    @converter
    def copy_as_fsm(self):
        return self.env['project.task'].create([{
            'project_id': self.env.ref('industry_fsm.fsm_project').id,
            'sale_order_id': r.sale_id.id,
            'name': f'Work Order: {r.name}',
            'partner_id': r.customer_shipping_id.id,
            'stage_id': r._convert_stage().id,
            'work_order_contacts': [Command.set(r.send_work_order_to.ids)],
            'site_contacts': [Command.set(r.site_contact_ids.ids)],
            'user_ids': [Command.set(r._convert_assignees_to_users().ids)],
            'date_deadline': r.date_service,
            'planned_date_begin': r.time_start_planned,
            'planned_date_end': r.time_end_planned,
            'planned_hours': r.time_planned,
            # TODO: tools_needed are left out, see if they need to be transferred.
            'child_ids': [Command.set(r.intervention_ids.copy_as_fsm().ids)],
            'equipment_ids': [Command.set(r.equipment_ids.copy_as_fsm().ids)],
            'tag_ids': [Command.set(self.env.ref(
                'durpro_fso_to_bemade_fsm.tag_converted_from_fso').ids)]
            # TODO: Figure out if we can have some logic for tying back to labour lines
        } for r in self])

    def _convert_stage(self):
        draft = self.env.ref('durpro_fso.work_order_stage_draft')
        parts = self.env.ref('durpro_fso.work_order_stage_waiting_parts')
        schedule = self.env.ref('durpro_fso.work_order_stage_to_schedule')
        planned = self.env.ref('durpro_fso.work_order_stage_scheduled')
        ready = self.env.ref('durpro_fso.work_order_stage_ready')
        done = self.env.ref('durpro_fso.work_order_stage_done')
        exception = self.env.ref('durpro_fso.work_order_stage_exception')
        invoiced = self.env.ref('durpro_fso.work_order_stage_invoiced')

        fsm_new = self.env.ref('industry_fsm.planning_project_stage_0')
        fsm_parts = self.env.ref('bemade_fsm.planning_project_stage_waiting_parts')
        fsm_planned = self.env.ref('industry_fsm.planning_project_stage_1')
        fsm_in_prog = self.env.ref('industry_fsm.planning_project_stage_2')
        fsm_executed = self.env.ref('bemade_fsm.planning_project_stage_work_completed')
        fsm_exception = self.env.ref('bemade_fsm.planning_project_stage_exception')
        fsm_done = self.env.ref('industry_fsm.planning_project_stage_3')
        stage_map = {
            draft: fsm_new,
            parts: fsm_parts,
            schedule: fsm_new,
            planned: fsm_planned,
            ready: fsm_planned,
            done: fsm_executed,
            exception: fsm_exception,
            invoiced: fsm_done,
        }
        return stage_map[self.stage_id]

    def _convert_assignees_to_users(self):
        return self.env['res.users'].search([
            ('partner_id', 'in', (self.technician_id | self.assistant_ids).ids)
        ])

    def _copy_actual_times_as_timesheets(self):
        for rec in self:
            users = rec._convert_assignees_to_users()
            if users:
                return rec.env['account.analytic.line'].create([{
                    'date': rec.date_service,
                    'user_id': u.id,
                    'employee_id': u.employee_id.id,
                    'name': "Completed service",
                    'unit_amount': rec.time_actual,
                    'project_id': rec.env.ref('industry_fsm.fsm_project').id,
                    'task_id': rec.converted.id,
                } for u in users])
