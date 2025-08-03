from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase
import logging

_logger = logging.getLogger(__name__)


class Odoo16Database(models.Model):
    """Main coordinator class for Odoo 16 to 18 migration."""
    _name = 'odoo16.database'
    _description = 'Odoo 16 Database Migration Coordinator'
    _inherits = {'odoo16.database.base': 'database_id'}
    
    database_id = fields.Many2one('odoo16.database.base', required=True, ondelete='cascade')
    name = fields.Char(string='Connection Name', required=True)
    
    # Migration component references
    users_partners_migration_id = fields.Many2one('migration.users.partners', ondelete='cascade')
    mail_system_migration_id = fields.Many2one('migration.mail.system', ondelete='cascade')
    calendar_events_migration_id = fields.Many2one('migration.calendar.events', ondelete='cascade')
    attachments_migration_id = fields.Many2one('migration.attachments', ondelete='cascade')
    ir_filters_migration_id = fields.Many2one('migration.ir.filters', ondelete='cascade')
    sports_teams_migration_id = fields.Many2one('migration.sports.teams', ondelete='cascade')
    sports_patients_migration_id = fields.Many2one('migration.sports.patients', ondelete='cascade')
    sports_injuries_migration_id = fields.Many2one('migration.sports.injuries', ondelete='cascade')
    
    # Configuration fields (delegated from components)
    skip_filestore = fields.Boolean(
        related='attachments_migration_id.skip_filestore',
        readonly=False,
        string='Skip Filestore Import'
    )
    migrate_ir_filters = fields.Boolean(
        related='ir_filters_migration_id.migrate_ir_filters',
        readonly=False,
        string='Migrate User Filters'
    )
    

    
    @api.model_create_multi
    def create(self, vals_list):
        """Create migration coordinators with all component migrations (batch support)."""
        coordinators = self.browse()
        for vals in vals_list:
            coordinator = self._create_single_coordinator(vals)
            coordinators += coordinator
        return coordinators
    
    def _create_single_coordinator(self, vals):
        """Create a single migration coordinator with all components."""
        # Create base database record
        base_vals = {
            'database_host': vals.get('database_host'),
            'database_name': vals.get('database_name'),
            'database_username': vals.get('database_username'),
            'database_password': vals.get('database_password'),
            'database_port': vals.get('database_port'),
        }
        database_base = self.env['odoo16.database.base'].create(base_vals)
        vals['database_id'] = database_base.id
        
        # Create the coordinator record
        coordinator = super().create([vals])[0]  # Use batch create for consistency
        
        # Create all migration component records
        coordinator._create_migration_components()
        
        return coordinator
    
    def _create_migration_components(self):
        """Create all migration component records."""
        # Create users & partners migration
        users_partners = self.env['migration.users.partners'].create({
            'database_id': self.database_id.id
        })
        self.users_partners_migration_id = users_partners.id
        
        # Create mail system migration
        mail_system = self.env['migration.mail.system'].create({
            'database_id': self.database_id.id
        })
        self.mail_system_migration_id = mail_system.id
        
        # Create calendar events migration
        calendar_events = self.env['migration.calendar.events'].create({
            'database_id': self.database_id.id
        })
        self.calendar_events_migration_id = calendar_events.id
        
        # Create attachments migration
        attachments = self.env['migration.attachments'].create({
            'database_id': self.database_id.id
        })
        self.attachments_migration_id = attachments.id
        
        # Create IR filters migration
        ir_filters = self.env['migration.ir.filters'].create({
            'database_id': self.database_id.id
        })
        self.ir_filters_migration_id = ir_filters.id
        
        # Create sports teams migration
        sports_teams = self.env['migration.sports.teams'].create({
            'database_id': self.database_id.id
        })
        self.sports_teams_migration_id = sports_teams.id
        
        # Create sports patients migration
        sports_patients = self.env['migration.sports.patients'].create({
            'database_id': self.database_id.id
        })
        self.sports_patients_migration_id = sports_patients.id
        
        # Create sports injuries migration
        sports_injuries = self.env['migration.sports.injuries'].create({
            'database_id': self.database_id.id
        })
        self.sports_injuries_migration_id = sports_injuries.id
    
    # Delegation methods for migration actions
    def action_migrate_users_partners(self):
        """Delegate to users & partners migration."""
        return self.users_partners_migration_id.action_migrate_users_partners()
    
    def action_migrate_mail_system(self):
        """Delegate to mail system migration."""
        return self.mail_system_migration_id.action_migrate_mail_system()
    
    def action_migrate_calendar_events_to_tasks(self):
        """Delegate to calendar events migration."""
        return self.calendar_events_migration_id.action_migrate_calendar_events_to_tasks()
    
    def action_migrate_attachments(self):
        """Delegate to attachments migration."""
        return self.attachments_migration_id.action_migrate_attachments()
    
    def action_migrate_ir_filters(self):
        """Delegate to IR filters migration."""
        return self.ir_filters_migration_id.action_migrate_ir_filters()
    
    def action_migrate_sports_teams(self):
        """Delegate to sports teams migration."""
        return self.sports_teams_migration_id.action_migrate_sports_teams()
    
    def action_migrate_sports_patients(self):
        """Delegate to sports patients migration."""
        return self.sports_patients_migration_id.action_migrate_sports_patients()
    
    def action_migrate_sports_injuries(self):
        """Delegate to sports injuries migration."""
        return self.sports_injuries_migration_id.action_migrate_sports_injuries()
    
    # Updated sports clinic migration methods
    def action_migrate_teams(self):
        """Migrate sports teams - delegates to sports teams migration component."""
        return self.action_migrate_sports_teams()
    
    def action_migrate_patients(self):
        """Migrate patients - delegates to sports patients migration component."""
        return self.action_migrate_sports_patients()
    
    def action_migrate_injuries(self):
        """Migrate injuries - delegates to sports injuries migration component."""
        return self.action_migrate_sports_injuries()
    
    def action_migrate_activities(self):
        """Migrate activities (placeholder - to be implemented)."""
        return self._success_notification("Activities Migration", "Activities migration not yet implemented")
    
    def action_migrate_all(self):
        """Perform complete migration from Odoo 16 to Odoo 18."""
        try:
            self._update_migration_status('in_progress', 'Starting complete migration')
            
            # Migration sequence
            self.action_migrate_users_partners()  # All users and partners first
            self.action_migrate_teams()
            self.action_migrate_patients()
            self.action_migrate_injuries()
            self.action_migrate_activities()
            self.action_migrate_calendar_events_to_tasks()
            self.action_migrate_mail_system()  # Mail channels, notifications, etc.
            self.action_migrate_attachments()
            # IR filters migration is optional and controlled by configuration
            
            self._update_migration_status('completed', 'Complete migration finished successfully')
            return self._success_notification(
                "Complete Migration Successful",
                "All data has been successfully migrated from Odoo 16 to Odoo 18."
            )
            
        except Exception as e:
            self._update_migration_status('failed', f'Migration failed: {str(e)}')
            _logger.error(f"Migration failed: {str(e)}")
            raise UserError("Migration failed: %s" % str(e))
    
    def action_validate_source_data(self):
        """Validate the source Odoo 16 database structure and data."""
        try:
            with self.get_cursor() as cr:
                # Check if bemade_sports_clinic module exists
                cr.execute("""
                    SELECT name, state FROM ir_module_module 
                    WHERE name = 'bemade_sports_clinic'
                """)
                
                module_info = cr.fetchone()
                if not module_info:
                    raise Exception("The bemade_sports_clinic module was not found in the source database.")
                
                if module_info[1] != 'installed':
                    raise Exception("The bemade_sports_clinic module is not installed in the source database.")
                
                # Check for core required tables (essential for migration)
                core_required_tables = ['res_users', 'res_partner']
                optional_tables = ['sports_team', 'sports_patient', 'sports_patient_injury', 'mail_activity', 'calendar_event']
                
                missing_core_tables = []
                missing_optional_tables = []
                
                # Check core required tables
                for table in core_required_tables:
                    cr.execute("""
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'public' AND table_name = %s
                    """, (table,))
                    
                    if not cr.fetchone():
                        missing_core_tables.append(table)
                
                # Check optional tables
                for table in optional_tables:
                    cr.execute("""
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'public' AND table_name = %s
                    """, (table,))
                    
                    if not cr.fetchone():
                        missing_optional_tables.append(table)
                
                # Fail only if core tables are missing
                if missing_core_tables:
                    raise Exception(f"Core required tables missing: {missing_core_tables}")
                
                # Log missing optional tables as warnings
                if missing_optional_tables:
                    _logger.warning(f"Optional tables missing (migration will skip these): {missing_optional_tables}")
                
                return self._success_notification(
                    "Validation Successful",
                    "Source database structure validated successfully. Ready for migration."
                )
                
        except Exception as e:
            _logger.error(f"Validation failed: {str(e)}")
            raise Exception(f"Source database validation failed: {str(e)}")
    
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
    
    def test_connection(self):
        """Test connection to the Odoo 16 database (XML view method)."""
        return self.action_test_connection()
    
    def action_test_connection(self):
        """Test connection to the Odoo 16 database."""
        try:
            with self.get_cursor() as cr:
                cr.execute("SELECT version()")
                version = cr.fetchone()[0]
                
                # Count calendar events as a test
                cr.execute("SELECT COUNT(*) FROM calendar_event")
                event_count = cr.fetchone()[0]
                
                return self._success_notification(
                    "Connection Successful",
                    f"Connected to database successfully.\nPostgreSQL Version: {version}\nCalendar Events Found: {event_count}"
                )
                
        except Exception as e:
            _logger.error(f"Connection test failed: {str(e)}")
            return self._error_notification(
                "Connection Failed",
                f"Failed to connect to the Odoo 16 database: {str(e)}"
            )
