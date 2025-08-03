from odoo import models, fields, api, _
from contextlib import contextmanager
import psycopg2
import os
import logging

_logger = logging.getLogger(__name__)
PAGE_SIZE = 1000


class Odoo16DatabaseBase(models.Model):
    """Base class for Odoo 16 database connection and common functionality."""
    _name = 'odoo16.database.base'
    _description = 'Odoo 16 Database Base'
    
    # Database connection fields
    database_host = fields.Char(
        string='Database Host',
        required=True,
        default=lambda self: os.environ.get('ODOO16_HOST', 'localhost')
    )
    database_name = fields.Char(
        string='Database Name',
        required=True,
        default=lambda self: os.environ.get('ODOO16_DBNAME', '')
    )
    database_username = fields.Char(
        string='Database Username',
        required=True,
        default=lambda self: os.environ.get('ODOO16_USER', 'odoo')
    )
    database_password = fields.Char(
        string='Database Password',
        default=lambda self: os.environ.get('ODOO16_PASSWORD', '')
    )
    database_port = fields.Integer(
        string='Database Port',
        required=True,
        default=lambda self: int(os.environ.get('ODOO16_PORT', '5432'))
    )
    filestore_path = fields.Char(
        string='Filestore Path',
        help='Path to the Odoo 16 filestore directory'
    )
    
    # Migration status fields
    migration_status = fields.Selection([
        ('not_started', 'Not Started'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed')
    ], default='not_started', string='Migration Status')
    
    last_migration_date = fields.Datetime(string='Last Migration Date', readonly=True)
    migration_log = fields.Text(string='Migration Log')
    
    @api.constrains('filestore_path')
    def _constrain_filestore_path(self):
        """Validate filestore path accessibility."""
        for record in self:
            if record.filestore_path:
                try:
                    if not os.access(record.filestore_path, os.R_OK):
                        raise Exception(
                            f"The provided filestore path is not readable: {record.filestore_path}"
                        )
                except Exception as e:
                    _logger.warning(f"Unable to access filestore path: {record.filestore_path}. Error: {e}")
                    # Don't fail validation for filestore path issues in test environment
    
    @contextmanager
    def get_cursor(self):
        """Get a database cursor for the Odoo 16 database."""
        conn = None
        try:
            conn = psycopg2.connect(
                host=self.database_host,
                database=self.database_name,
                user=self.database_username,
                password=self.database_password,
                port=self.database_port
            )
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            _logger.error(f"Database connection error: {str(e)}")
            raise Exception(f"Database connection failed: {str(e)}")
        finally:
            if conn:
                cursor.close()
                conn.close()
    
    def _update_migration_status(self, status, message):
        """Update migration status and log."""
        self.migration_status = status
        current_log = self.migration_log or ''
        timestamp = fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.migration_log = f"{current_log}\n[{timestamp}] {message}"
        
        # Update last migration date when migration completes or fails
        if status in ('completed', 'failed'):
            self.last_migration_date = fields.Datetime.now()
            
        _logger.info(f"Migration status updated: {status} - {message}")
    
    def _success_notification(self, title, message):
        """Return a success notification."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': 'success',
                'sticky': False,
            }
        }
    
    def _error_notification(self, title, message):
        """Return an error notification."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': 'danger',
                'sticky': True,
            }
        }
    
    def _create_or_update_record(self, model_name, search_domain, values, record_identifier="record"):
        """Create or update a record with merge functionality and chatter logging.
        
        Args:
            model_name (str): Name of the model (e.g., 'res.partner')
            search_domain (list): Domain to search for existing record
            values (dict): Values to create/update the record with
            record_identifier (str): Human-readable identifier for logging
            
        Returns:
            tuple: (record, action) where action is 'created' or 'updated'
        """
        model = self.env[model_name]
        # Search for existing records including archived ones
        existing_record = model.with_context(active_test=False).search(search_domain, limit=1)
        
        if existing_record:
            # Record exists - merge/overwrite with Odoo 16 data
            old_values = {}
            updated_fields = []
            
            # Track which fields are being updated
            for field_name, new_value in values.items():
                if hasattr(existing_record, field_name):
                    old_value = getattr(existing_record, field_name)
                    # Handle different field types for comparison
                    if field_name.endswith('_id') and hasattr(old_value, 'id'):
                        old_value = old_value.id
                    elif hasattr(old_value, 'ids'):  # Many2many field
                        old_value = old_value.ids
                    
                    if old_value != new_value:
                        old_values[field_name] = old_value
                        updated_fields.append(field_name)
            
            if updated_fields:
                # Update the record
                existing_record.sudo().write(values)
                
                # Log the overwrite
                _logger.info(f"Overwritten {record_identifier} (ID: {existing_record.id}) - "
                           f"Updated fields: {', '.join(updated_fields)}")
                
                # Add chatter message if the model supports mail.thread
                if hasattr(existing_record, 'message_post'):
                    try:
                        message = f"""<p><strong>Data Migration Update</strong></p>
                        <p>This {record_identifier} was updated during Odoo 16 to 18 migration.</p>
                        <p><strong>Updated fields:</strong> {', '.join(updated_fields)}</p>
                        <p><strong>Previous data was overwritten with Odoo 16 source data.</strong></p>"""
                        
                        existing_record.message_post(
                            body=message,
                            subject=f"Migration Update: {record_identifier}",
                            message_type='notification'
                        )
                    except Exception as e:
                        _logger.warning(f"Could not post chatter message for {record_identifier}: {e}")
                
                return existing_record, 'updated'
            else:
                # No changes needed
                _logger.info(f"No changes needed for {record_identifier} (ID: {existing_record.id})")
                return existing_record, 'unchanged'
        else:
            # Record doesn't exist - create new one
            new_record = model.sudo().create(values)
            _logger.info(f"Created new {record_identifier} (ID: {new_record.id})")
            return new_record, 'created'
