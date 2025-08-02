from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase, PAGE_SIZE
import logging

_logger = logging.getLogger(__name__)


class MigrationIrFilters(models.Model):
    """Migration methods for ir.filter records with client validation."""
    _name = 'migration.ir.filters'
    _description = 'IR Filters Migration'
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
    
    # Configuration fields
    migrate_ir_filters = fields.Boolean(
        string='Migrate User Filters',
        default=False,
        help='Enable migration of user-created filters (ir.filter). Requires client validation.'
    )
    
    def action_migrate_ir_filters(self):
        """Migrate ir.filter records from Odoo 16."""
        if not self.migrate_ir_filters:
            return self._success_notification(
                "IR Filters Migration Skipped",
                "User filters migration is disabled. Enable 'Migrate User Filters' if needed."
            )
        
        try:
            self._update_migration_status('in_progress', 'Starting IR filters migration')
            
            filter_count = 0
            skipped_count = 0
            
            with self.get_cursor() as cr:
                cr.execute("""
                    SELECT id, name, model_id, user_id, domain, context, sort, is_default,
                           action_id, active, create_date, write_date, create_uid, write_uid
                    FROM ir_filters 
                    WHERE active = true 
                    ORDER BY id LIMIT %s
                """, (PAGE_SIZE,))
                
                filters = cr.fetchall()
                
                for filter_data in filters:
                    # Validate that the user exists in the target system
                    if filter_data[3]:  # user_id
                        user_exists = self.env['res.users'].search([('id', '=', filter_data[3])], limit=1)
                        if not user_exists:
                            _logger.warning(f"Skipping filter '{filter_data[1]}' - user {filter_data[3]} not found")
                            skipped_count += 1
                            continue
                    
                    # Validate that the model exists in the target system
                    if filter_data[2]:  # model_id
                        model_exists = self.env['ir.model'].search([('id', '=', filter_data[2])], limit=1)
                        if not model_exists:
                            _logger.warning(f"Skipping filter '{filter_data[1]}' - model {filter_data[2]} not found")
                            skipped_count += 1
                            continue
                    
                    filter_vals = {
                        'name': filter_data[1],
                        'model_id': filter_data[2],
                        'user_id': filter_data[3],
                        'domain': filter_data[4],
                        'context': filter_data[5],
                        'sort': filter_data[6],
                        'is_default': filter_data[7],
                        'action_id': filter_data[8],
                        'active': filter_data[9],
                    }
                    
                    # Check if filter already exists
                    existing_filter = self.env['ir.filters'].search([
                        ('name', '=', filter_data[1]),
                        ('model_id', '=', filter_data[2]),
                        ('user_id', '=', filter_data[3])
                    ], limit=1)
                    
                    if not existing_filter:
                        self.env['ir.filters'].create(filter_vals)
                        filter_count += 1
            
            status_message = f'IR filters migration completed: {filter_count} filters migrated'
            if skipped_count > 0:
                status_message += f', {skipped_count} filters skipped (missing dependencies)'
            
            self._update_migration_status('completed', status_message)
            
            success_message = f"Successfully migrated {filter_count} user filters from Odoo 16."
            if skipped_count > 0:
                success_message += f" {skipped_count} filters were skipped due to missing users or models."
            
            return self._success_notification("IR Filters Migration Successful", success_message)
            
        except Exception as e:
            self._update_migration_status('failed', f'IR filters migration failed: {str(e)}')
            _logger.error(f"IR filters migration failed: {str(e)}")
            raise UserError(_("IR filters migration failed: %s") % str(e))
