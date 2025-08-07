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
            
            # Dictionary to store projects by client (user_id or create_uid)
            client_projects = {}
            
            task_count = 0
            skipped_count = 0
            skipped_reasons = {}
            
            with self.get_cursor() as cr:
                # Get available columns in calendar_event table
                cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'calendar_event'")
                available_columns = [row[0] for row in cr.fetchall()]
                
                # Define base columns and optional columns
                base_columns = ['id', 'name', 'description', 'start', 'stop', 'allday']
                optional_columns = ['location', 'privacy', 'show_as', 'user_id', 'partner_ids', 'create_date', 'write_date', 'create_uid', 'write_uid']
                
                # Build select columns list based on what's available
                select_columns = base_columns + [col for col in optional_columns if col in available_columns]
                
                # Get total count for progress tracking (include both active and inactive events)
                cr.execute("SELECT COUNT(*) FROM calendar_event")
                total_count = cr.fetchone()[0]
                _logger.info(f"Found {total_count} calendar events to migrate (including inactive)")
                
                # Debug: Check if we have any events at all
                if total_count == 0:
                    _logger.warning("No calendar events found in source database")
                    self._update_migration_status('completed', 'Calendar events migration completed: 0 events found in source database')
                    return self._success_notification(
                        "Calendar Events Migration Completed",
                        "No calendar events found in source database to migrate."
                    )
                
                # Migrate all calendar events using pagination
                offset = 0
                batch_size = PAGE_SIZE
                
                while True:
                    # Fetch calendar events with their details (include both active and inactive)
                    query = f"SELECT {', '.join(select_columns)} FROM calendar_event ORDER BY id LIMIT %s OFFSET %s"
                    cr.execute(query, (batch_size, offset))
                    
                    events = cr.fetchall()
                    if not events:
                        break
                        
                    _logger.info(f"Processing calendar events batch {offset}-{offset + len(events)} of {total_count}")
                    
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
                                # First find the partner by odoo16_partner_id, then check for user
                                partner = self.env['res.partner'].with_context(active_test=False).search([('odoo16_partner_id', '=', partner_id)], limit=1)
                                if partner:
                                    user = self.env['res.users'].with_context(active_test=False).search([('partner_id', '=', partner.id)], limit=1)
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
                        
                        # Add calendar event timing information
                        if event_dict.get('start'):
                            description_parts.append(f"Original Event Start: {event_dict['start']}")
                        if event_dict.get('stop'):
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
                        
                        # Determine client for this event (use user_id, create_uid, or default)
                        client_id = event_dict.get('user_id') or event_dict.get('create_uid') or 0
                        
                        # Get or create project for this client
                        if client_id not in client_projects:
                            client_projects[client_id] = self._get_or_create_client_project(client_id)
                        
                        project = client_projects[client_id]
                        
                        # Create unique task name to avoid any conflicts
                        original_name = event_dict.get('name') or 'Migrated Calendar Event'
                        unique_name = f"{original_name} (Event ID: {event_id})"
                        
                        # Prepare task values with proper date mapping
                        task_vals = {
                            'name': unique_name,
                            'description': '\n\n'.join(description_parts),
                            'project_id': project.id,
                            'user_ids': [(6, 0, assignee_ids)] if assignee_ids else False,
                            'tag_ids': [(6, 0, [self._get_or_create_migration_tag().id])],
                            'odoo16_event_id': event_id,  # Track original calendar event ID
                        }
                        
                        # Map calendar event dates to task fields
                        if event_dict.get('start'):  # Calendar event start date
                            task_vals['planned_date_start'] = event_dict['start']  # Task planned start
                            task_vals['planned_date_begin'] = event_dict['start']  # Task planned begin
                        
                        if event_dict.get('stop'):  # Calendar event end date
                            task_vals['date_deadline'] = event_dict['stop']  # Task deadline
                        
                        try:
                            # Debug: Log task creation attempt
                            _logger.info(f"Creating task for calendar event {event_id}: {task_vals.get('name')} in project {project.name}")
                            
                            # Create task directly without deduplication
                            task = self.env['project.task'].create(task_vals)
                            action = 'created'
                            
                            _logger.info(f"Successfully created task {task.id} for calendar event {event_id}")
                            
                            if action in ['created', 'updated']:
                                task_count += 1
                        except Exception as e:
                            reason = f"Task creation failed: {str(e)}"
                            self.database_id._log_skipped_item(
                                'calendar.event', 
                                event_id, 
                                reason,
                                {
                                    'event_name': original_name,
                                    'start': str(event_dict.get('start')) if event_dict.get('start') else None,
                                    'stop': str(event_dict.get('stop')) if event_dict.get('stop') else None,
                                    'error': str(e)
                                }
                            )
                            skipped_count += 1
                            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                            continue
                    
                    # Move to next batch
                    offset += len(events)
            
            # Commit all ORM changes to ensure tasks are persisted
            self.env.cr.commit()
            _logger.info(f"Committed {task_count} task creations to database")
            
            # Log comprehensive calendar events migration summary
            total_processed = task_count + skipped_count
            self.database_id._log_migration_summary(
                'Calendar Events to Tasks', 
                total_processed, 
                skipped_count, 
                skipped_reasons
            )
            
            self._update_migration_status('completed', 
                f'Calendar events to tasks migration completed: {task_count} tasks created, {skipped_count} skipped')
            
            project_summary = ", ".join([f"{p.name} ({len(self.env['project.task'].search([('project_id', '=', p.id), ('tag_ids', 'in', self._get_or_create_migration_tag().id)]))} tasks)" for p in client_projects.values()])
            
            return self._success_notification(
                "Calendar Events Migration Successful",
                f"Successfully migrated {task_count} calendar events to project tasks. "
                f"Projects created: {project_summary}"
            )
            
        except Exception as e:
            self._update_migration_status('failed', f'Calendar events migration failed: {str(e)}')
            _logger.error(f"Calendar events migration failed: {str(e)}")
            raise UserError(_("Calendar events migration failed: %s") % str(e))
    
    def _get_or_create_client_project(self, client_id):
        """Get or create a project for a specific client's migrated calendar events."""
        # Determine project name based on client
        if client_id and client_id != 0:
            # Try to find the user/partner name for the client
            client_name = self._get_client_name(client_id)
            project_name = f"Migrated Calendar Events - {client_name}"
        else:
            project_name = "Migrated Calendar Events - Unknown Client"
            
        project = self.env['project.project'].search([
            ('name', '=', project_name)
        ], limit=1)
        
        if not project:
            project = self.env['project.project'].create({
                'name': project_name,
                'description': f'Project containing calendar events migrated from Odoo 16 for client ID {client_id}',
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
    
    def _get_client_name(self, client_id):
        """Get client name from user ID in source database."""
        try:
            with self.get_cursor() as cr:
                # Get user name via partner relationship (res_users.partner_id -> res_partner.name)
                cr.execute("""
                    SELECT p.name 
                    FROM res_users u 
                    JOIN res_partner p ON u.partner_id = p.id 
                    WHERE u.id = %s
                """, (client_id,))
                result = cr.fetchone()
                if result:
                    return result[0]
                    
                # If no user found, try to get partner name directly
                cr.execute("SELECT name FROM res_partner WHERE id = %s", (client_id,))
                result = cr.fetchone()
                if result:
                    return result[0]
                    
        except Exception as e:
            _logger.warning(f"Could not get client name for ID {client_id}: {e}")
            
        return f"Client {client_id}"
    
    def _get_partner_name(self, partner_id):
        """Get partner name by odoo16_partner_id."""
        partner = self.env['res.partner'].with_context(active_test=False).search([('odoo16_partner_id', '=', partner_id)], limit=1)
        return partner.name if partner else f"Partner ID {partner_id}"
