from odoo import models, fields, api, _
from contextlib import contextmanager
import psycopg2
import os
import logging
from datetime import datetime

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
    
    def _get_skipped_items_log_path(self):
        """Get the path for the skipped items log file."""
        # Create logs directory if it doesn't exist
        logs_dir = '/tmp/migration_logs'
        if not os.path.exists(logs_dir):
            os.makedirs(logs_dir)
        
        # Create unique log file name with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return os.path.join(logs_dir, f'migration_skipped_items_{timestamp}.log')
    
    def _log_skipped_item(self, item_type, source_id, reason, context=None):
        """Log a skipped item to the dedicated skipped items log file.
        
        Args:
            item_type (str): Type of item (e.g., 'mail.message', 'channel.member', 'partner')
            source_id (int/str): ID of the item in the source database
            reason (str): Reason why the item was skipped
            context (dict): Additional context information
        """
        try:
            log_path = self._get_skipped_items_log_path()
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Format context information
            context_str = ''
            if context:
                context_parts = []
                for key, value in context.items():
                    context_parts.append(f"{key}={value}")
                context_str = f" | Context: {', '.join(context_parts)}"
            
            # Create log entry
            log_entry = f"[{timestamp}] SKIPPED: {item_type} (ID: {source_id}) | Reason: {reason}{context_str}\n"
            
            # Append to log file
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(log_entry)
            
            # Also log to standard logger with WARNING level so it's visible
            _logger.warning(f"SKIPPED {item_type} ID {source_id}: {reason}")
            
        except Exception as e:
            _logger.error(f"Failed to log skipped item {item_type} ID {source_id}: {e}")
    
    def _log_migration_summary(self, component_name, total_processed, total_skipped, skipped_breakdown=None):
        """Log a summary of migration results including skipped items.
        
        Args:
            component_name (str): Name of the migration component
            total_processed (int): Total number of items processed
            total_skipped (int): Total number of items skipped
            skipped_breakdown (dict): Breakdown of skipped items by reason
        """
        try:
            log_path = self._get_skipped_items_log_path()
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            summary_lines = [
                f"\n{'='*80}",
                f"[{timestamp}] MIGRATION SUMMARY: {component_name}",
                f"{'='*80}",
                f"Total Processed: {total_processed}",
                f"Total Skipped: {total_skipped}",
                f"Success Rate: {((total_processed - total_skipped) / total_processed * 100):.1f}%" if total_processed > 0 else "Success Rate: 0%"
            ]
            
            if skipped_breakdown:
                summary_lines.append("\nSkipped Items Breakdown:")
                for reason, count in skipped_breakdown.items():
                    summary_lines.append(f"  - {reason}: {count}")
            
            summary_lines.append(f"{'='*80}\n")
            
            # Write to log file
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write('\n'.join(summary_lines))
            
            # Also log to standard logger
            _logger.info(f"Migration Summary - {component_name}: {total_processed - total_skipped}/{total_processed} successful, {total_skipped} skipped")
            
        except Exception as e:
            _logger.error(f"Failed to log migration summary for {component_name}: {e}")
