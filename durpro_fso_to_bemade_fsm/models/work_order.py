from odoo import models, fields, Command
from .tools import converter


class WorkOrder(models.Model):
    _inherit = 'durpro_fso.work_order'

    converted = fields.Many2one('project.task')
    visit = fields.Many2one('bemade_fsm.visit')
    active = fields.Boolean(default=True)

    def action_convert_to_fsm(self):
        tasks = self.copy_as_fsm()
        self._copy_actual_times_as_timesheets()
        self.active = False
        return tasks.ids

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
            'equipment_ids': [Command.set(r.equipment_ids.copy_as_fsm().ids)],
            'tag_ids': [Command.set(self.env.ref(
                'durpro_fso_to_bemade_fsm.tag_converted_from_fso').ids), ],
            'visit_id': r._convert_to_visit().id,
            'child_ids': [Command.set(r.intervention_ids.copy_as_fsm().ids)],
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

    def _convert_to_visit(self):
        if self.visit:
            return self.visit
        if not self.sale_id.visit_ids:
            visit = self._add_first_visit_to_so()
        else:
            visit = self._add_subsequent_visit_to_so()
        self.visit = visit
        return visit

    def _add_first_visit_to_so(self):
        so = self.sale_id
        visit = self.env['bemade_fsm.visit'].create({
            'sale_order_id': so.id,
            'label': self.name,
            'approx_date': self.date_service,
        })
        visit.so_section_id.sequence = 1
        for sol in so.order_line.filtered(lambda l: l != visit.so_section_id):
            sol.sequence += 1
        return visit

    def _add_subsequent_visit_to_so(self):
        visit = self.env['bemade_fsm.visit'].create({
            'sale_order_id': self.sale_id.id,
            'label': self.name,
            'approx_date': self.date_service,
        })
        visit.so_section_id.sequence = max(
            self.sale_id.order_line.mapped('sequence')) + 1
        return visit
