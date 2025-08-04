# -*- coding: utf-8 -*-
"""
Activities Migration Module for Odoo 16 to 18 Migration
Handles migration of mail.activity records and related data.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase
import logging

_logger = logging.getLogger(__name__)


class MigrationActivities(models.Model):
    """Migration component for mail.activity records from Odoo 16 to Odoo 18."""
    
    _name = 'migration.activities'
    _description = 'Activities Migration'
    _inherits = {'odoo16.database.base': 'database_id'}
    
    database_id = fields.Many2one('odoo16.database.base', required=True, ondelete='cascade')
    
    # Delegation methods to access database connection
    def get_cursor(self):
        """Get database cursor through delegation to base class."""
        return self.database_id.get_cursor()
    
    def _update_migration_status(self, status, message):
        """Update migration status through delegation to base class."""
        return self.database_id._update_migration_status(status, message)
    
    def _success_notification(self, title, message):
        """Return success notification through delegation to base class."""
        return self.database_id._success_notification(title, message)
    
    def _error_notification(self, title, message):
        """Return error notification through delegation to base class."""
        return self.database_id._error_notification(title, message)
    
    # Migration statistics
    activities_migrated = fields.Integer(string='Activities Migrated', default=0, readonly=True)
    
    def action_migrate_activities(self):
        """Migrate mail.activity records from Odoo 16 to Odoo 18."""
        try:
            self._update_migration_status('in_progress', 'Starting activities migration')
            
            activities_count = 0
            
            with self.get_cursor() as cr:
                # Check if mail_activity table exists in source database
                cr.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'public' 
                        AND table_name = 'mail_activity'
                    )
                """)
                
                if not cr.fetchone()[0]:
                    _logger.info("No mail_activity table found in source database")
                    return self._success_notification(
                        "Activities Migration Completed",
                        "No activities found to migrate from source database"
                    )
                
                # Get available columns in mail_activity table
                cr.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = 'mail_activity' AND table_schema = 'public'
                """)
                available_columns = [row[0] for row in cr.fetchall()]
                _logger.info(f"Available columns in mail_activity: {available_columns}")
                
                # Define base columns that should exist
                base_columns = ['id', 'res_model', 'res_id', 'activity_type_id', 'summary', 'user_id']
                optional_columns = ['note', 'date_deadline', 'automated', 'create_date', 'create_uid', 'write_date', 'write_uid']
                
                # Build SELECT query with available columns
                select_columns = []
                for col in base_columns + optional_columns:
                    if col in available_columns:
                        select_columns.append(col)
                
                if not select_columns:
                    _logger.warning("No recognizable columns found in mail_activity table")
                    return self._success_notification(
                        "Activities Migration Completed",
                        "No compatible activity data found in source database"
                    )
                
                # Query activities from source database
                query = f"SELECT {', '.join(select_columns)} FROM mail_activity ORDER BY id"
                cr.execute(query)
                
                activities_data = cr.fetchall()
                _logger.info(f"Found {len(activities_data)} activities to migrate")
                
                if not activities_data:
                    return self._success_notification(
                        "Activities Migration Completed",
                        "No activities found in source database"
                    )
                
                activities_count = self._migrate_activities(activities_data, select_columns)
            
            # Update migration statistics
            self.write({'activities_migrated': activities_count})
            
            success_message = f"Activities migration completed: {activities_count} activities migrated"
            self._update_migration_status('completed', success_message)
            
            return self._success_notification("Activities Migration Successful", success_message)
            
        except Exception as e:
            error_message = f"Activities migration failed: {str(e)}"
            self._update_migration_status('failed', error_message)
            _logger.error(error_message, exc_info=True)
            raise UserError(_(error_message))
    
    def _migrate_activities(self, activities_data, column_names):
        """Migrate individual activity records."""
        migrated_count = 0
        skipped_count = 0
        
        for activity_data in activities_data:
            try:
                # Create dictionary mapping column names to values
                activity_dict = dict(zip(column_names, activity_data))
                source_id = activity_dict.get('id')
                
                # Validate required fields
                if not activity_dict.get('res_model') or not activity_dict.get('res_id'):
                    _logger.warning(f"Skipping activity {source_id}: missing res_model or res_id")
                    skipped_count += 1
                    continue
                
                # Check if the target model exists in current Odoo 18 environment
                res_model = activity_dict['res_model']
                if res_model not in self.env:
                    _logger.warning(f"Skipping activity {source_id}: model '{res_model}' not available in target environment")
                    skipped_count += 1
                    continue
                
                # Check if the target record exists
                try:
                    target_record = self.env[res_model].browse(activity_dict['res_id'])
                    if not target_record.exists():
                        _logger.warning(f"Skipping activity {source_id}: target record {res_model}({activity_dict['res_id']}) does not exist")
                        skipped_count += 1
                        continue
                except Exception as e:
                    _logger.warning(f"Skipping activity {source_id}: error accessing target record: {str(e)}")
                    skipped_count += 1
                    continue
                
                # Prepare activity values
                activity_vals = {
                    'res_model': res_model,
                    'res_id': activity_dict['res_id'],
                    'summary': activity_dict.get('summary') or 'Migrated Activity',
                }
                
                # Handle activity type - use default if source type doesn't exist
                activity_type_id = activity_dict.get('activity_type_id')
                if activity_type_id:
                    # Try to find matching activity type, fallback to default
                    activity_type = self.env['mail.activity.type'].search([
                        ('id', '=', activity_type_id)
                    ], limit=1)
                    if not activity_type:
                        # Use default activity type (usually "To Do")
                        activity_type = self.env['mail.activity.type'].search([], limit=1)
                    if activity_type:
                        activity_vals['activity_type_id'] = activity_type.id
                
                # Handle user assignment
                user_id = activity_dict.get('user_id')
                if user_id:
                    # Try to find migrated user
                    migrated_user = self.env['res.users'].search([
                        ('odoo16_user_id', '=', user_id)
                    ], limit=1)
                    if migrated_user:
                        activity_vals['user_id'] = migrated_user.id
                    else:
                        # Fallback to current user or admin
                        activity_vals['user_id'] = self.env.user.id
                else:
                    activity_vals['user_id'] = self.env.user.id
                
                # Add optional fields if available
                if activity_dict.get('note'):
                    activity_vals['note'] = activity_dict['note']
                
                if activity_dict.get('date_deadline'):
                    activity_vals['date_deadline'] = activity_dict['date_deadline']
                
                if activity_dict.get('automated') is not None:
                    activity_vals['automated'] = activity_dict['automated']
                
                # Handle audit trail fields
                if activity_dict.get('create_date'):
                    activity_vals['create_date'] = activity_dict['create_date']
                
                if activity_dict.get('write_date'):
                    activity_vals['write_date'] = activity_dict['write_date']
                
                # Handle create_uid - find migrated user
                create_uid = activity_dict.get('create_uid')
                if create_uid:
                    migrated_user = self.env['res.users'].search([
                        ('odoo16_user_id', '=', create_uid)
                    ], limit=1)
                    if migrated_user:
                        activity_vals['create_uid'] = migrated_user.id
                
                # Use merge functionality to create or update activity
                search_domain = [
                    ('res_model', '=', res_model),
                    ('res_id', '=', activity_dict['res_id']),
                    ('summary', '=', activity_vals['summary']),
                ]
                
                # Add user to search domain if available
                if 'user_id' in activity_vals:
                    search_domain.append(('user_id', '=', activity_vals['user_id']))
                
                record_identifier = f"mail.activity on {res_model}({activity_dict['res_id']}) - {activity_vals['summary']}"
                
                activity, action = self.database_id._create_or_update_record(
                    'mail.activity',
                    search_domain,
                    activity_vals,
                    record_identifier
                )
                
                if action in ['created', 'updated']:
                    migrated_count += 1
                    _logger.debug(f"Successfully {action} activity {source_id} -> {activity.id}")
                
            except Exception as e:
                _logger.error(f"Failed to migrate activity {source_id}: {str(e)}")
                skipped_count += 1
                continue
        
        if skipped_count > 0:
            _logger.info(f"Activities migration completed: {migrated_count} migrated, {skipped_count} skipped")
        else:
            _logger.info(f"Activities migration completed: {migrated_count} activities migrated")
        
        return migrated_count
