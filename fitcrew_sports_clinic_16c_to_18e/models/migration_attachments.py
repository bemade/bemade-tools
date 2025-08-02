from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase, PAGE_SIZE
import logging

_logger = logging.getLogger(__name__)


class MigrationAttachments(models.Model):
    """Migration methods for ir.attachment records with filestore skip option."""
    _name = 'migration.attachments'
    _description = 'Attachments Migration'
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
    skip_filestore = fields.Boolean(
        string='Skip Filestore Import',
        default=True,
        help='If enabled, attachment records will be created but file content will be skipped (nullified references)'
    )
    
    def action_migrate_attachments(self):
        """Migrate ir.attachment records from Odoo 16."""
        try:
            self._update_migration_status('in_progress', 'Starting attachments migration')
            
            attachment_count = 0
            
            with self.get_cursor() as cr:
                # Get available columns in ir_attachment table
                cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ir_attachment'")
                available_columns = [row[0] for row in cr.fetchall()]
                
                # Define base columns and optional columns
                base_columns = ['id', 'name', 'description', 'res_model', 'res_id', 'res_field']
                optional_columns = ['datas_fname', 'company_id', 'type', 'url', 'public', 'access_token', 
                                  'datas', 'store_fname', 'file_size', 'checksum', 'mimetype', 
                                  'index_content', 'active', 'create_date', 'write_date', 
                                  'create_uid', 'write_uid']
                
                # Build select columns list based on what's available
                select_columns = base_columns + [col for col in optional_columns if col in available_columns]
                
                # Build WHERE clause conditionally based on available columns
                where_clause = ""
                if 'active' in available_columns:
                    where_clause = "WHERE active = true"
                
                query = f"SELECT {', '.join(select_columns)} FROM ir_attachment {where_clause} ORDER BY id LIMIT %s"
                cr.execute(query, (PAGE_SIZE,))
                
                attachments = cr.fetchall()
                
                for attachment_data in attachments:
                    # Build attachment data dictionary dynamically based on available columns
                    attachment_dict = {}
                    for i, col_name in enumerate(select_columns):
                        attachment_dict[col_name] = attachment_data[i] if i < len(attachment_data) else None
                    
                    # Build attachment values using dynamic data with proper type handling
                    attachment_vals = {
                        'name': attachment_dict.get('name'),
                        'description': attachment_dict.get('description'),
                        'res_model': attachment_dict.get('res_model'),
                        'res_field': attachment_dict.get('res_field'),
                    }
                    
                    # Handle res_id with proper integer conversion
                    res_id = attachment_dict.get('res_id')
                    if res_id is not None:
                        try:
                            attachment_vals['res_id'] = int(res_id) if res_id else False
                        except (ValueError, TypeError):
                            # Skip attachments with invalid res_id values
                            continue
                    
                    # Add optional fields if they exist in the source data
                    optional_field_mapping = {
                        'datas_fname': 'datas_fname',
                        'company_id': 'company_id',
                        'type': 'type',
                        'url': 'url',
                        'public': 'public',
                        'access_token': 'access_token',
                        'datas': 'datas',
                        'store_fname': 'store_fname',
                        'file_size': 'file_size',
                        'checksum': 'checksum',
                        'mimetype': 'mimetype',
                        'index_content': 'index_content',
                        'active': 'active',
                    }
                    
                    for source_field, target_field in optional_field_mapping.items():
                        if source_field in attachment_dict:
                            value = attachment_dict[source_field]
                            
                            # Handle integer fields with proper type conversion
                            if target_field in ['company_id', 'file_size'] and value is not None:
                                try:
                                    attachment_vals[target_field] = int(value) if value else False
                                except (ValueError, TypeError):
                                    # Skip invalid integer values
                                    continue
                            else:
                                attachment_vals[target_field] = value
                    
                    # Handle filestore skip logic
                    if self.skip_filestore:
                        attachment_vals.update({
                            'datas': False,
                            'store_fname': False,
                            'checksum': False,
                            'description': (attachment_vals['description'] or '') + 
                                         '\n\n[NOTE: Original file content not migrated - filestore import was skipped]'
                        })
                    
                    # Check if attachment already exists
                    existing_attachment = self.env['ir.attachment'].search([
                        ('name', '=', attachment_dict.get('name')),
                        ('res_model', '=', attachment_dict.get('res_model')),
                        ('res_id', '=', attachment_dict.get('res_id'))
                    ], limit=1)
                    
                    if not existing_attachment:
                        self.env['ir.attachment'].create(attachment_vals)
                        attachment_count += 1
            
            status_message = f'Attachments migration completed: {attachment_count} attachments migrated'
            if self.skip_filestore:
                status_message += ' (file content skipped)'
            
            self._update_migration_status('completed', status_message)
            
            success_message = f"Successfully migrated {attachment_count} attachments from Odoo 16."
            if self.skip_filestore:
                success_message += " File content was skipped as configured."
            
            return self._success_notification("Attachments Migration Successful", success_message)
            
        except Exception as e:
            self._update_migration_status('failed', f'Attachments migration failed: {str(e)}')
            _logger.error(f"Attachments migration failed: {str(e)}")
            raise UserError(_("Attachments migration failed: %s") % str(e))
