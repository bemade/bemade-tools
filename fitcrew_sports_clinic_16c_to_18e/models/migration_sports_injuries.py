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
        try:
            self._update_migration_status('in_progress', 'Starting sports injuries migration...')
            
            with self.get_cursor() as cursor:
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
                        # Find the corresponding migrated patient using odoo16_patient_id
                        migrated_patient = self.env['sports.patient'].search([
                            ('odoo16_patient_id', '=', patient_id)
                        ], limit=1)
                        
                        if not migrated_patient:
                            # Debug: Check if patient exists in source database
                            with self.get_cursor() as debug_cursor:
                                debug_cursor.execute("SELECT id, first_name, last_name FROM sports_patient WHERE id = %s", (patient_id,))
                                source_patient = debug_cursor.fetchone()
                                if source_patient:
                                    _logger.error(f"Patient {patient_id} exists in source ({source_patient[1]} {source_patient[2]}) but not found in target for injury {source_id}")
                                else:
                                    _logger.error(f"Patient {patient_id} does not exist in source database for injury {source_id}")
                            
                            # Check how many patients we have migrated
                            total_migrated = self.env['sports.patient'].search_count([('odoo16_patient_id', '!=', False)])
                            _logger.error(f"Total migrated patients: {total_migrated}")
                            
                            # Check if there are any patients with this exact ID
                            all_with_id = self.env['sports.patient'].search([('odoo16_patient_id', '=', patient_id)])
                            _logger.error(f"Patients with odoo16_patient_id {patient_id}: {len(all_with_id)}")
                            
                            continue
                        
                        # Find the corresponding migrated user using odoo16_user_id
                        migrated_user = None
                        if create_uid:
                            migrated_user = self.env['res.users'].search([
                                ('odoo16_user_id', '=', create_uid)
                            ], limit=1)
                        
                        # Prepare injury values
                        injury_vals = {
                            'patient_id': migrated_patient.id,
                            'diagnosis': diagnosis or '',
                            'injury_date': injury_date,
                            'injury_date_na': injury_date_na or False,
                            'predicted_resolution_date': predicted_resolution_date,
                            'resolution_date': resolution_date,
                            'internal_notes': internal_notes,
                            'external_notes': external_notes,
                            'parental_consent': parental_consent or 'no',
                            'odoo16_injury_id': source_id,  # Store original Odoo 16 injury ID for tracking
                        }
                        
                        # Add audit trail fields
                        if create_date:
                            injury_vals['create_date'] = create_date
                        if migrated_user:
                            injury_vals['create_uid'] = migrated_user.id
                        if write_date:
                            injury_vals['write_date'] = write_date
                        
                        # Create injury directly (no deduplication)
                        new_injury = self.env['sports.patient.injury'].create(injury_vals)
                        _logger.debug(f"Created injury {source_id} -> {new_injury.id}")
                        
                        migrated_count += 1
                        
                    except Exception as e:
                        _logger.error(f"Error migrating injury {source_id}: {str(e)}")
                        continue
                
                _logger.info(f"Successfully migrated {migrated_count} patient injuries")
                return migrated_count
                
        except Exception as e:
            _logger.error(f"Sports injuries migration failed: {str(e)}")
            self._update_migration_status('failed', f'Sports injuries migration failed: {str(e)}')
            raise UserError(_("Sports injuries migration failed: %s") % str(e))
    
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
            self._update_migration_status('failed', error_msg)
            _logger.error(error_msg)
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
