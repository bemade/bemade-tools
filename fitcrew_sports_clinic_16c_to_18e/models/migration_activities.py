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
    activities_skipped = fields.Integer(string='Activities Skipped', default=0, readonly=True)
    model_mappings_found = fields.Integer(string='Model Mappings Found', default=0, readonly=True)
    
    def _build_model_mapping(self):
        """Build mapping table between source and target database model IDs."""
        # Use a simple approach - rebuild mapping each time for now
        # In a production environment, you might want to cache this in a file or database
        _logger.info("Building model ID mapping between source and target databases")
        model_mapping = {}
        
        try:
            with self.get_cursor() as cr:
                # Get all models from source database (Odoo 16)
                cr.execute("""
                    SELECT id, model FROM ir_model 
                    WHERE model IS NOT NULL AND model != ''
                    ORDER BY model
                """)
                source_models = cr.fetchall()
                
            # Get all models from target database (Odoo 18)
            target_models = self.env['ir.model'].search_read(
                [('model', '!=', False)], 
                ['id', 'model']
            )
            
            # Create mapping dictionaries for efficient lookup
            source_model_dict = {model_name: model_id for model_id, model_name in source_models}
            target_model_dict = {model['model']: model['id'] for model in target_models}
            
            # Build the mapping
            mappings_found = 0
            for model_name in source_model_dict:
                source_id = source_model_dict[model_name]
                if model_name in target_model_dict:
                    target_id = target_model_dict[model_name]
                    model_mapping[source_id] = {
                        'model_name': model_name,
                        'source_id': source_id,
                        'target_id': target_id
                    }
                    mappings_found += 1
                else:
                    _logger.warning(f"Model '{model_name}' (ID: {source_id}) exists in source but not in target database")
            
            # Update statistics
            self.write({'model_mappings_found': mappings_found})
            
            _logger.info(f"Model mapping completed: {mappings_found} models mapped out of {len(source_models)} source models")
            
            return model_mapping
            
        except Exception as e:
            _logger.error(f"Failed to build model mapping: {str(e)}")
            raise UserError(_(f"Failed to build model mapping: {str(e)}"))
    
    def _get_target_model_info(self, source_res_model, source_res_id, model_mapping):
        """Get target model information for a given source res_model and res_id.
        
        Args:
            source_res_model: Model name from source database (e.g., 'sports.patient')
            source_res_id: Record ID from source database
            model_mapping: Dictionary mapping source model IDs to target model info
            
        Returns:
            tuple: (target_model_name, target_res_id, target_model_id) or (None, None, None) if not found
        """
        # For most cases, model names should be the same between Odoo 16 and 18
        # But we should verify the model exists in the target environment
        if source_res_model not in self.env:
            _logger.warning(f"Model '{source_res_model}' not available in target environment")
            return None, None, None
            
        # Get the target model ID from our mapping
        target_model_id = None
        for mapping_info in model_mapping.values():
            if mapping_info['model_name'] == source_res_model:
                target_model_id = mapping_info['target_id']
                break
                
        if not target_model_id:
            _logger.warning(f"No model ID mapping found for '{source_res_model}'")
            return None, None, None
            
        # Find the target record using the appropriate odoo16_*_id field
        target_record = None
        try:
            if source_res_model == 'sports.patient':
                target_record = self.env['sports.patient'].with_context(active_test=False).search([
                    ('odoo16_patient_id', '=', source_res_id)
                ], limit=1)
            elif source_res_model == 'sports.patient.injury':
                target_record = self.env['sports.patient.injury'].with_context(active_test=False).search([
                    ('odoo16_injury_id', '=', source_res_id)
                ], limit=1)
            elif source_res_model == 'sports.team':
                # Sports teams don't have odoo16_team_id, they use direct ID mapping
                # Check if this is a migrated team by looking for it directly
                target_record = self.env['sports.team'].browse(source_res_id)
                if not target_record.exists():
                    target_record = None
            elif source_res_model == 'res.partner':
                target_record = self.env['res.partner'].with_context(active_test=False).search([
                    ('odoo16_partner_id', '=', source_res_id)
                ], limit=1)
            elif source_res_model == 'res.users':
                target_record = self.env['res.users'].with_context(active_test=False).search([
                    ('odoo16_user_id', '=', source_res_id)
                ], limit=1)
            else:
                # For other models, try direct ID lookup as fallback
                _logger.warning(f"No specific odoo16_*_id mapping defined for model '{source_res_model}', trying direct ID lookup")
                target_record = self.env[source_res_model].browse(source_res_id)
                if not target_record.exists():
                    target_record = None
                    
            if not target_record:
                _logger.warning(f"Target record {source_res_model}({source_res_id}) not found using odoo16_*_id mapping")
                return None, None, None
                
            return source_res_model, target_record.id, target_model_id
            
        except Exception as e:
            _logger.warning(f"Error looking up target record {source_res_model}({source_res_id}): {str(e)}")
            return None, None, None
    
    def action_migrate_activities(self):
        """Migrate mail.activity records from Odoo 16 to Odoo 18."""
        try:
            self._update_migration_status('in_progress', 'Starting activities migration')
            
            # Build model mapping first
            model_mapping = self._build_model_mapping()
            
            activities_count = 0
            skipped_count = 0
            
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
                
                activities_count, skipped_count = self._migrate_activities(activities_data, select_columns, model_mapping)
            
            # Update migration statistics
            self.write({
                'activities_migrated': activities_count,
                'activities_skipped': skipped_count
            })
            
            success_message = f"Activities migration completed: {activities_count} activities migrated, {skipped_count} skipped"
            self._update_migration_status('completed', success_message)
            
            return self._success_notification("Activities Migration Successful", success_message)
            
        except Exception as e:
            error_message = f"Activities migration failed: {str(e)}"
            self._update_migration_status('failed', error_message)
            _logger.error(error_message, exc_info=True)
            raise UserError(_(error_message))
    
    def _migrate_activities(self, activities_data, column_names, model_mapping):
        """Migrate individual activity records with model mapping.
        
        Args:
            activities_data: List of activity records from source database
            column_names: List of column names corresponding to activity_data
            model_mapping: Dictionary mapping source model IDs to target model info
            
        Returns:
            tuple: (migrated_count, skipped_count)
        """
        migrated_count = 0
        skipped_count = 0
        skipped_reasons = {}
        
        for activity_data in activities_data:
            try:
                # Create dictionary mapping column names to values
                activity_dict = dict(zip(column_names, activity_data))
                source_id = activity_dict.get('id')
                
                # Validate required fields
                if not activity_dict.get('res_model') or not activity_dict.get('res_id'):
                    reason = "Missing res_model or res_id"
                    self.database_id._log_skipped_item(
                        'mail.activity', 
                        source_id, 
                        reason,
                        {
                            'res_model': activity_dict.get('res_model'),
                            'res_id': activity_dict.get('res_id'),
                            'summary': activity_dict.get('summary')
                        }
                    )
                    skipped_count += 1
                    skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                    continue
                
                # Use model mapping to get target model information
                source_res_model = activity_dict['res_model']
                source_res_id = activity_dict['res_id']
                
                target_model, target_res_id, target_model_id = self._get_target_model_info(source_res_model, source_res_id, model_mapping)
                if not target_model or not target_res_id or not target_model_id:
                    reason = f"Cannot map model/record {source_res_model}({source_res_id})"
                    self.database_id._log_skipped_item(
                        'mail.activity', 
                        source_id, 
                        reason,
                        {
                            'res_model': source_res_model,
                            'res_id': source_res_id,
                            'summary': activity_dict.get('summary')
                        }
                    )
                    skipped_count += 1
                    skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                    continue
                
                # Prepare activity values with mapped model information
                activity_vals = {
                    'res_model': target_model,
                    'res_model_id': target_model_id,  # This is required in Odoo 18
                    'res_id': target_res_id,
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
                    migrated_user = self.env['res.users'].with_context(active_test=False).search([
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
                    migrated_user = self.env['res.users'].with_context(active_test=False).search([
                        ('odoo16_user_id', '=', create_uid)
                    ], limit=1)
                    if migrated_user:
                        activity_vals['create_uid'] = migrated_user.id
                
                # Use merge functionality to create or update activity
                search_domain = [
                    ('res_model', '=', target_model),
                    ('res_id', '=', target_res_id),
                    ('summary', '=', activity_vals['summary']),
                ]
                
                # Add user to search domain if available
                if 'user_id' in activity_vals:
                    search_domain.append(('user_id', '=', activity_vals['user_id']))
                
                record_identifier = f"mail.activity on {target_model}({target_res_id}) - {activity_vals['summary']}"
                
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
                reason = f"Processing error: {str(e)}"
                self.database_id._log_skipped_item(
                    'mail.activity', 
                    source_id, 
                    reason,
                    {
                        'res_model': activity_dict.get('res_model'),
                        'res_id': activity_dict.get('res_id'),
                        'summary': activity_dict.get('summary'),
                        'error': str(e)
                    }
                )
                skipped_count += 1
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                continue
        
        # Log comprehensive activities migration summary
        total_processed = migrated_count + skipped_count
        self.database_id._log_migration_summary(
            'Mail Activities', 
            total_processed, 
            skipped_count, 
            skipped_reasons
        )
        
        _logger.info(f"Activities migration completed: {migrated_count} activities migrated, {skipped_count} skipped")
        return migrated_count, skipped_count
