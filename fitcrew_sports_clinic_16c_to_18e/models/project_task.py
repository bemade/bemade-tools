from odoo import models, fields


class ProjectTask(models.Model):
    _inherit = 'project.task'
    
    # Migration tracking field for calendar events converted to tasks
    odoo16_event_id = fields.Integer(
        string='Odoo 16 Event ID',
        help='Original calendar.event ID from Odoo 16 for migration tracking',
        index=True
    )
