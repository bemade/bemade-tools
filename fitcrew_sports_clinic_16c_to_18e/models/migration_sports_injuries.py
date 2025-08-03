# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class MigrationSportsInjuries(models.Model):
    """Migration component for sports patient injuries from Odoo 16 to Odoo 18."""
    
    _name = 'migration.sports.injuries'
    _description = 'Sports Patient Injuries Migration'
    _inherits = {'odoo16.database.base': 'database_id'}
    
    database_id = fields.Many2one('odoo16.database.base', required=True, ondelete='cascade')
    
    # Delegation methods to access database connection
    def get_cursor(self):
        """Get database cursor through delegation to base class."""
        return self.database_id.get_cursor()
    
    def _update_migration_status(self, status, message):
        """Update migration status through delegation to base class."""
        return self.database_id._update_migration_status(status, message)
    
    def migrate_patient_injuries(self):
        """Migrate sports patient injuries from Odoo 16 to Odoo 18."""
        cursor = self.get_cursor()
        
        # Query sports_patient_injury records from source database
        cursor.execute("""
            SELECT 
                id, patient_id, diagnosis, injury_date, injury_date_na,
                predicted_resolution_date, resolution_date,
                internal_notes, external_notes, parental_consent,
                create_date, write_date, create_uid, write_uid
            FROM sports_patient_injury 
            ORDER BY id
        """)
        
        injury_records = cursor.fetchall()
        _logger.info(f"Found {len(injury_records)} patient injury records to migrate")
        
        migrated_count = 0
        
        for injury_data in injury_records:
            (
                source_id, patient_id, diagnosis, injury_date, injury_date_na,
                predicted_resolution_date, resolution_date,
                internal_notes, external_notes, parental_consent,
                create_date, write_date, create_uid, write_uid
            ) = injury_data
            
            try:
                # Find the corresponding migrated patient
                migrated_patient = self.env['sports.patient'].search([
                    ('source_id', '=', patient_id)
                ], limit=1)
                
                if not migrated_patient:
                    _logger.warning(f"Patient with source_id {patient_id} not found for injury {source_id}")
                    continue
                
                # Prepare injury data
                injury_vals = {
                    'patient_id': migrated_patient.id,
                    'diagnosis': diagnosis or '',
                    'injury_date': injury_date,
                    'injury_date_na': injury_date_na or False,
                    'predicted_resolution_date': predicted_resolution_date,
                    'resolution_date': resolution_date,
                    'internal_notes': internal_notes or '',
                    'external_notes': external_notes or '',
                    'parental_consent': parental_consent or 'not_required',
                    'source_id': source_id,  # Store original ID for reference
                }
                
                # Handle audit trail fields
                if create_uid:
                    # Try to find corresponding user, fallback to current user
                    migrated_user = self.env['res.users'].search([
                        ('source_id', '=', create_uid)
                    ], limit=1)
                    if migrated_user:
                        injury_vals['create_uid'] = migrated_user.id
                
                if write_uid:
                    migrated_user = self.env['res.users'].search([
                        ('source_id', '=', write_uid)
                    ], limit=1)
                    if migrated_user:
                        injury_vals['write_uid'] = migrated_user.id
                
                if create_date:
                    injury_vals['create_date'] = create_date
                if write_date:
                    injury_vals['write_date'] = write_date
                
                # Check if injury already exists
                existing_injury = self.env['sports.patient.injury'].search([
                    ('source_id', '=', source_id)
                ], limit=1)
                
                if existing_injury:
                    # Update existing injury
                    existing_injury.write(injury_vals)
                    _logger.debug(f"Updated existing injury {source_id}")
                else:
                    # Create new injury
                    new_injury = self.env['sports.patient.injury'].create(injury_vals)
                    _logger.debug(f"Created new injury {source_id} -> {new_injury.id}")
                
                migrated_count += 1
                
            except Exception as e:
                _logger.error(f"Error migrating injury {source_id}: {str(e)}")
                continue
        
        _logger.info(f"Successfully migrated {migrated_count} patient injuries")
        return migrated_count
    
    @api.model
    def action_migrate_sports_injuries(self):
        """Main action to migrate sports patient injuries."""
        try:
            self._update_migration_status('in_progress', 'Starting sports injuries migration...')
            
            # Migrate patient injuries
            injury_count = self.migrate_patient_injuries()
            
            # Update migration status
            message = f"Sports injuries migration completed: {injury_count} injuries migrated"
            self._update_migration_status('completed', message)
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Migration Completed'),
                    'message': message,
                    'type': 'success',
                    'sticky': False,
                }
            }
            
        except Exception as e:
            error_msg = f"Sports injuries migration failed: {str(e)}"
            _logger.error(error_msg)
            self._update_migration_status('failed', error_msg)
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Migration Failed'),
                    'message': error_msg,
                    'type': 'danger',
                    'sticky': True,
                }
            }
