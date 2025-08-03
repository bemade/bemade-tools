# -*- coding: utf-8 -*-
"""
Sports Patients Migration Module for Odoo 16 to 18 Migration
Handles migration of sports.patient records and related data.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase
import logging

_logger = logging.getLogger(__name__)


class MigrationSportsPatients(models.Model):
    """Migration component for sports patients and related data."""
    _name = 'migration.sports.patients'
    _description = 'Sports Patients Migration'
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
    
    # Migration statistics
    patients_migrated = fields.Integer(string='Patients Migrated', default=0, readonly=True)
    patient_contacts_migrated = fields.Integer(string='Patient Contacts Migrated', default=0, readonly=True)
    team_patient_relations_migrated = fields.Integer(string='Team-Patient Relations Migrated', default=0, readonly=True)

    def action_migrate_sports_patients(self):
        """Migrate sports patients from Odoo 16 to Odoo 18."""
        try:
            self._update_migration_status('in_progress', 'Starting sports patients migration')
            with self.get_cursor() as cr:
                patients_count = self._migrate_patients(cr)
                contacts_count = self._migrate_patient_contacts(cr)
                relations_count = self._migrate_team_patient_relations(cr)
            
            self.patients_migrated = patients_count
            self.patient_contacts_migrated = contacts_count
            self.team_patient_relations_migrated = relations_count
            
            message = f'Successfully migrated {patients_count} patients, {contacts_count} contacts, and {relations_count} team relations'
            self._update_migration_status('completed', message)
            return self._success_notification('Sports Patients Migration Complete', message)
            
        except Exception as e:
            error_msg = f'Sports patients migration failed: {str(e)}'
            _logger.error(error_msg, exc_info=True)
            self._update_migration_status('failed', error_msg)
            return self._error_notification('Sports Patients Migration Failed', error_msg)

    def _migrate_patients(self, cursor):
        """Migrate sports patients from source database."""
        _logger.info("Starting sports patients migration...")
        
        # Query source patients
        cursor.execute("""
            SELECT id, first_name, last_name, email, partner_id, date_of_birth, mobile,
                   match_status, practice_status, predicted_return_date, return_date, 
                   last_consultation_date, allergies, team_info_notes,
                   create_date, create_uid, write_date, write_uid
            FROM sports_patient 
            ORDER BY id
        """)
        source_patients = cursor.fetchall()
        
        if not source_patients:
            _logger.info("No patients found in source database")
            return 0
        
        _logger.info(f"Found {len(source_patients)} patients to migrate")
        migrated_count = 0
        
        for patient_data in source_patients:
            try:
                (patient_id, first_name, last_name, email, partner_id, date_of_birth, mobile,
                 match_status, practice_status, predicted_return_date, return_date,
                 last_consultation_date, allergies, team_info_notes,
                 create_date, create_uid, write_date, write_uid) = patient_data
                
                # Find corresponding partner in target database
                partner = None
                if partner_id:
                    partner = self.env['res.partner'].search([('id', '=', partner_id)], limit=1)
                    if not partner:
                        _logger.warning(f"Partner {partner_id} not found for patient {patient_id}")
                
                # Prepare patient values
                patient_vals = {
                    'first_name': first_name or '',
                    'last_name': last_name or '',
                    'email': email,
                    'mobile': mobile,
                    'date_of_birth': date_of_birth,
                    'match_status': match_status or 'available',
                    'practice_status': practice_status or 'available',
                    'predicted_return_date': predicted_return_date,
                    'return_date': return_date,
                    'last_consultation_date': last_consultation_date,
                    'allergies': allergies,
                    'team_info_notes': team_info_notes,
                }
                
                # Add partner relationship if found
                if partner:
                    patient_vals['partner_id'] = partner.id
                
                # Add audit trail fields
                if create_date:
                    patient_vals['create_date'] = create_date
                if write_date:
                    patient_vals['write_date'] = write_date
                
                # Create patient in target database
                new_patient = self.env['sports.patient'].create(patient_vals)
                migrated_count += 1
                
                _logger.info(f"Migrated patient: {first_name} {last_name} (ID: {patient_id} -> {new_patient.id})")
                
            except Exception as e:
                _logger.error(f"Failed to migrate patient {patient_id}: {str(e)}")
                continue
        
        _logger.info(f"Successfully migrated {migrated_count} patients")
        return migrated_count

    def _migrate_patient_contacts(self, cursor):
        """Migrate patient emergency contacts from source database."""
        _logger.info("Starting patient contacts migration...")
        
        # Query source patient contacts
        cursor.execute("""
            SELECT id, patient_id, name, contact_type, mobile, sequence,
                   create_date, create_uid, write_date, write_uid
            FROM sports_patient_contact 
            ORDER BY id
        """)
        source_contacts = cursor.fetchall()
        
        if not source_contacts:
            _logger.info("No patient contacts found in source database")
            return 0
        
        _logger.info(f"Found {len(source_contacts)} patient contacts to migrate")
        migrated_count = 0
        
        for contact_data in source_contacts:
            try:
                (contact_id, patient_id, name, contact_type, mobile, sequence,
                 create_date, create_uid, write_date, write_uid) = contact_data
                
                # Find corresponding patient in target database
                patient = self.env['sports.patient'].search([('id', '=', patient_id)], limit=1)
                if not patient:
                    _logger.warning(f"Patient {patient_id} not found for contact {contact_id}")
                    continue
                
                # Prepare contact values
                contact_vals = {
                    'patient_id': patient.id,
                    'name': name or '',
                    'contact_type': contact_type or 'other',
                    'mobile': mobile,
                    'sequence': sequence or 10,
                }
                
                # Add audit trail fields
                if create_date:
                    contact_vals['create_date'] = create_date
                if write_date:
                    contact_vals['write_date'] = write_date
                
                # Create contact in target database
                new_contact = self.env['sports.patient.contact'].create(contact_vals)
                migrated_count += 1
                
                _logger.info(f"Migrated contact: {name} for patient {patient.first_name} {patient.last_name}")
                
            except Exception as e:
                _logger.error(f"Failed to migrate contact {contact_id}: {str(e)}")
                continue
        
        _logger.info(f"Successfully migrated {migrated_count} patient contacts")
        return migrated_count

    def _migrate_team_patient_relations(self, cursor):
        """Migrate team-patient relationships from source database."""
        _logger.info("Starting team-patient relations migration...")
        
        # Query source team-patient relationships
        cursor.execute("""
            SELECT team_id, patient_id, create_date, create_uid, write_date, write_uid
            FROM sports_team_patient_rel 
            ORDER BY team_id, patient_id
        """)
        source_relations = cursor.fetchall()
        
        if not source_relations:
            _logger.info("No team-patient relations found in source database")
            return 0
        
        _logger.info(f"Found {len(source_relations)} team-patient relations to migrate")
        migrated_count = 0
        
        for relation_data in source_relations:
            try:
                (team_id, patient_id, create_date, create_uid, write_date, write_uid) = relation_data
                
                # Find corresponding team and patient in target database
                team = self.env['sports.team'].search([('id', '=', team_id)], limit=1)
                patient = self.env['sports.patient'].search([('id', '=', patient_id)], limit=1)
                
                if not team:
                    _logger.warning(f"Team {team_id} not found for relation")
                    continue
                    
                if not patient:
                    _logger.warning(f"Patient {patient_id} not found for relation")
                    continue
                
                # Add patient to team (Many2many relationship)
                team.write({'patient_ids': [(4, patient.id)]})
                migrated_count += 1
                
                _logger.info(f"Linked patient {patient.first_name} {patient.last_name} to team {team.name}")
                
            except Exception as e:
                _logger.error(f"Failed to migrate team-patient relation {team_id}-{patient_id}: {str(e)}")
                continue
        
        _logger.info(f"Successfully migrated {migrated_count} team-patient relations")
        return migrated_count

    def _get_patient_name_by_id(self, cursor, patient_id):
        """Helper method to get patient name by ID from source database."""
        cursor.execute("SELECT first_name, last_name FROM sports_patient WHERE id = %s", (patient_id,))
        result = cursor.fetchone()
        if result:
            first_name, last_name = result
            return f"{first_name} {last_name}"
        return f"Patient {patient_id}"
