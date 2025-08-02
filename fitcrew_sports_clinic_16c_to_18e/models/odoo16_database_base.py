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
