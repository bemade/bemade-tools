from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase, PAGE_SIZE
import logging

_logger = logging.getLogger(__name__)


class MigrationCalendarEvents(models.Model):
    """Migration methods for calendar events to project tasks transformation."""
    _name = 'migration.calendar.events'
    _description = 'Calendar Events to Tasks Migration'
    _inherits = {'odoo16.database.base': 'database_id'}
    
    database_id = fields.Many2one('odoo16.database.base', required=True, ondelete='cascade')
    
    def get_cursor(self):
        """Get database cursor - delegate to base class."""
        return self.database_id.get_cursor()
    
    def _update_migration_status(self, status, message):
        """Update migration status - delegate to base class."""
        return self.database_id._update_migration_status(status, message)
    
    def _success_notification(self, title, message):
        """Return success notification - delegate to base class."""
        return self.database_id._success_notification(title, message)
    
    def _error_notification(self, title, message):
        """Return error notification - delegate to base class."""
        return self.database_id._error_notification(title, message)
    
    def action_migrate_calendar_events_to_tasks(self):
        """Migrate calendar events from Odoo 16 and transform them into project tasks."""
        try:
            self._update_migration_status('in_progress', 'Starting calendar events to tasks migration')
            
            # Create or get the default project for migrated events
            project = self._get_or_create_migration_project()
            
            task_count = 0
            
            with self.get_cursor() as cr:
                # Get available columns in calendar_event table
                cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'calendar_event'")
                available_columns = [row[0] for row in cr.fetchall()]
                
                # Define base columns and optional columns
                base_columns = ['id', 'name', 'description', 'start', 'stop', 'allday']
                optional_columns = ['location', 'privacy', 'show_as', 'user_id', 'partner_ids', 'create_date', 'write_date', 'create_uid', 'write_uid']
                
                # Build select columns list based on what's available
                select_columns = base_columns + [col for col in optional_columns if col in available_columns]
                
                # Fetch calendar events with their details
                query = f"SELECT {', '.join(select_columns)} FROM calendar_event WHERE active = true ORDER BY id LIMIT %s"
                cr.execute(query, (PAGE_SIZE,))
                
                events = cr.fetchall()
                
                for event_data in events:
                    # Build event data dictionary dynamically based on available columns
                    event_dict = {}
                    for i, col_name in enumerate(select_columns):
                        event_dict[col_name] = event_data[i] if i < len(event_data) else None
                    
                    event_id = event_dict['id']
                    
                    # Get attendees for this event with dynamic column detection
                    cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'calendar_attendee'")
                    attendee_columns = [row[0] for row in cr.fetchall()]
                    
                    # Define base columns and optional columns for attendees
                    attendee_base_columns = ['partner_id', 'state']
                    attendee_optional_columns = ['email']
                    
                    # Build select columns list based on what's available
                    attendee_select_columns = [col for col in attendee_base_columns if col in attendee_columns] + \
                                            [col for col in attendee_optional_columns if col in attendee_columns]
                    
                    attendee_query = f"SELECT {', '.join(attendee_select_columns)} FROM calendar_attendee WHERE event_id = %s AND state IN ('accepted', 'tentative')"
                    cr.execute(attendee_query, (event_id,))
                    
                    attendees = cr.fetchall()
                    
                    # Build assignee IDs and attendee data with existence checks
                    assignee_ids = []
                    attendee_data_list = []
                    
                    for attendee in attendees:
                        # Build attendee data dictionary dynamically
                        attendee_dict = {}
                        for i, col_name in enumerate(attendee_select_columns):
                            attendee_dict[col_name] = attendee[i] if i < len(attendee) else None
                        
                        attendee_data_list.append(attendee_dict)
                        
                        partner_id = attendee_dict.get('partner_id')
                        if partner_id:
                            # Check if the partner has a corresponding user in the target database
                            user = self.env['res.users'].search([('partner_id', '=', partner_id)], limit=1)
                            if user:
                                assignee_ids.append(user.id)
                    
                    # Create task description with event details
                    description_parts = []
                    if event_dict.get('description'):
                        description_parts.append(f"Original Description: {event_dict['description']}")
                    if event_dict.get('location'):
                        description_parts.append(f"Location: {event_dict['location']}")
                    if event_dict.get('privacy'):
                        description_parts.append(f"Privacy: {event_dict['privacy']}")
                    if event_dict.get('show_as'):
                        description_parts.append(f"Show As: {event_dict['show_as']}")
                    
                    description_parts.append(f"Original Event Start: {event_dict['start']}")
                    description_parts.append(f"Original Event End: {event_dict['stop']}")
                    description_parts.append(f"All Day Event: {'Yes' if event_dict['allday'] else 'No'}")
                    
                    if attendee_data_list:
                        attendee_list = []
                        for attendee_dict in attendee_data_list:
                            partner_id = attendee_dict.get('partner_id')
                            state = attendee_dict.get('state', 'unknown')
                            email = attendee_dict.get('email')
                            
                            partner_name = self._get_partner_name(partner_id) if partner_id else email
                            attendee_list.append(f"- {partner_name} ({state})")
                        description_parts.append(f"Original Attendees:\n" + "\n".join(attendee_list))
                    
                    task_vals = {
                        'name': event_dict.get('name') or 'Migrated Calendar Event',
                        'description': '\n\n'.join(description_parts),
                        'project_id': project.id,
                        'user_ids': [(6, 0, assignee_ids)] if assignee_ids else False,
                        'date_deadline': event_dict.get('stop'),  # Use event end time as deadline
                        'tag_ids': [(6, 0, [self._get_or_create_migration_tag().id])],
                    }
                    
                    # Use merge functionality to create or update task
                    search_domain = [
                        ('name', '=', task_vals['name']),
                        ('project_id', '=', project.id)
                    ]
                    
                    record_identifier = f"project task '{task_vals['name']}' in project '{project.name}'"
                    task, action = self.database_id._create_or_update_record(
                        'project.task',
                        search_domain,
                        task_vals,
                        record_identifier
                    )
                    
                    if action in ['created', 'updated']:
                        task_count += 1
            
            self._update_migration_status('completed', 
                f'Calendar events to tasks migration completed: {task_count} tasks created')
            
            return self._success_notification(
                "Calendar Events Migration Successful",
                f"Successfully migrated {task_count} calendar events to project tasks. "
                f"Tasks created in project: {project.name}"
            )
            
        except Exception as e:
            self._update_migration_status('failed', f'Calendar events migration failed: {str(e)}')
            _logger.error(f"Calendar events migration failed: {str(e)}")
            raise UserError(_("Calendar events migration failed: %s") % str(e))
    
    def _get_or_create_migration_project(self):
        """Get or create the default project for migrated calendar events."""
        project = self.env['project.project'].search([
            ('name', '=', 'Migrated Calendar Events')
        ], limit=1)
        
        if not project:
            project = self.env['project.project'].create({
                'name': 'Migrated Calendar Events',
                'description': 'Project containing tasks migrated from Odoo 16 calendar events',
                'privacy_visibility': 'employees',
            })
        
        return project
    
    def _get_or_create_migration_tag(self):
        """Get or create a tag for migrated calendar events."""
        tag = self.env['project.tags'].search([
            ('name', '=', 'Migrated from Calendar')
        ], limit=1)
        
        if not tag:
            tag = self.env['project.tags'].create({
                'name': 'Migrated from Calendar',
                'color': 5,  # Blue color
            })
        
        return tag
    
    def _get_partner_name(self, partner_id):
        """Get partner name by ID."""
        partner = self.env['res.partner'].browse(partner_id)
        return partner.name if partner.exists() else f"Partner ID {partner_id}"
