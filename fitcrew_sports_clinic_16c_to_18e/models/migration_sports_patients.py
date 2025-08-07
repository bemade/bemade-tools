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
            
            # Step 1: Migrate patients first and commit
            with self.get_cursor() as cr:
                patients_count = self._migrate_patients(cr)
            
            self.patients_migrated = patients_count
            self.env.cr.commit()
            _logger.info(f"✅ Patients migration committed - {patients_count} patients persisted")
            
            # Step 2: Migrate patient contacts (depends on patients) and commit
            with self.get_cursor() as cr:
                contacts_count = self._migrate_patient_contacts(cr)
            
            self.patient_contacts_migrated = contacts_count
            self.env.cr.commit()
            _logger.info(f"✅ Patient contacts migration committed - {contacts_count} contacts persisted")
            
            # Step 3: Migrate team-patient relations (depends on patients and teams) and commit
            with self.get_cursor() as cr:
                relations_count = self._migrate_team_patient_relations(cr)
            
            self.team_patient_relations_migrated = relations_count
            self.env.cr.commit()
            _logger.info(f"✅ Team-patient relations migration committed - {relations_count} relations persisted")
            
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
        
        # Ensure we have a proper environment reference
        target_env = self.env
        
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
        failed_count = 0
        failed_patient_ids = []
        skipped_count = 0
        skipped_reasons = {}
        
        for patient_data in source_patients:
            try:
                (patient_id, first_name, last_name, email, partner_id, date_of_birth, mobile,
                 match_status, practice_status, predicted_return_date, return_date,
                 last_consultation_date, allergies, team_info_notes,
                 create_date, create_uid, write_date, write_uid) = patient_data
                
                # Find or create corresponding partner (MANDATORY for sports.patient)
                partner = None
                if partner_id:
                    # First try direct lookup by odoo16_partner_id
                    partner = target_env['res.partner'].with_context(active_test=False).search([('odoo16_partner_id', '=', partner_id)], limit=1)
                    if partner:
                        _logger.debug(f"Found existing partner {partner.id} for patient {patient_id} via odoo16_partner_id")
                    else:
                        # Fallback: Search for deduplicated partner using source partner data
                        _logger.warning(f"Partner with odoo16_partner_id {partner_id} not found for patient {patient_id}, searching for deduplicated partner...")
                        
                        # Query source database to get original partner details
                        cursor.execute("""
                            SELECT name, email, phone, mobile 
                            FROM res_partner 
                            WHERE id = %s
                        """, (partner_id,))
                        source_partner = cursor.fetchone()
                        
                        if source_partner:
                            source_name, source_email, source_phone, source_mobile = source_partner
                            
                            # Try to find partner by email (most reliable identifier)
                            if source_email:
                                partner = target_env['res.partner'].with_context(active_test=False).search([
                                    ('email', '=', source_email)
                                ], limit=1)
                                if partner:
                                    _logger.warning(f"✅ Found deduplicated partner {partner.id} for patient {patient_id} via email: {source_email}")
                            
                            # If not found by email, try by name + phone/mobile
                            if not partner and source_name:
                                search_domain = [('name', '=', source_name)]
                                if source_phone:
                                    search_domain.append(('phone', '=', source_phone))
                                elif source_mobile:
                                    search_domain.append(('mobile', '=', source_mobile))
                                
                                if len(search_domain) > 1:  # Only search if we have name + phone/mobile
                                    partner = target_env['res.partner'].with_context(active_test=False).search(search_domain, limit=1)
                                    if partner:
                                        _logger.warning(f"✅ Found deduplicated partner {partner.id} for patient {patient_id} via name+phone: {source_name}")
                        
                        if not partner:
                            _logger.warning(f"❌ Could not find deduplicated partner for patient {patient_id} (source partner_id: {partner_id}), will create new partner")
                
                # Create partner if none exists (partner_id is MANDATORY for sports.patient)
                if not partner:
                    partner_name = f"{first_name or ''} {last_name or ''}".strip() or f"Patient {patient_id}"
                    partner = target_env['res.partner'].create({
                        'name': partner_name,
                        'email': email,
                        'mobile': mobile,
                        'is_company': False,
                        'odoo16_partner_id': partner_id or (100000 + patient_id)  # Use offset if no partner_id
                    })
                    _logger.info(f"Created basic partner {partner.id} for patient {patient_id}")
                
                # Prepare patient values with MANDATORY partner_id
                patient_vals = {
                    'first_name': first_name or '',
                    'last_name': last_name or '',
                    'partner_id': partner.id,  # MANDATORY!
                    'date_of_birth': date_of_birth,
                    'match_status': match_status or 'available',
                    'practice_status': practice_status or 'available',
                    'predicted_return_date': predicted_return_date,
                    'return_date': return_date,
                    'last_consultation_date': last_consultation_date,
                    'allergies': allergies,
                    'team_info_notes': team_info_notes,
                    'odoo16_patient_id': patient_id,  # Store original Odoo 16 patient ID for efficient lookups
                }
                
                # Add audit trail fields
                if create_date:
                    patient_vals['create_date'] = create_date
                if write_date:
                    patient_vals['write_date'] = write_date
                
                # Use merge functionality to handle duplicate odoo16_patient_id gracefully
                search_domain = [('odoo16_patient_id', '=', patient_id)]
                record_identifier = f"patient {patient_id} ({first_name} {last_name})"
                
                new_patient, action = self.database_id._create_or_update_record(
                    'sports.patient',
                    search_domain,
                    patient_vals,
                    record_identifier
                )
                
                if action in ['created', 'updated']:
                    migrated_count += 1
                    _logger.info(f"{'Created' if action == 'created' else 'Updated'} patient: {first_name} {last_name} (ID: {patient_id} -> {new_patient.id})")
                else:
                    _logger.warning(f"No action taken for patient {patient_id} ({first_name} {last_name}) - action: {action}")
                
            except Exception as e:
                reason = f"Processing error: {str(e)}"
                self.database_id._log_skipped_item(
                    'sports.patient', 
                    patient_id, 
                    reason,
                    {
                        'first_name': first_name,
                        'last_name': last_name,
                        'email': email,
                        'error': str(e)
                    }
                )
                skipped_count += 1
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                failed_count += 1
                failed_patient_ids.append(patient_id)
                continue
        
        _logger.info(f"Successfully migrated {migrated_count} patients")
        if failed_count > 0:
            _logger.error(f"Failed to migrate {failed_count} patients. Failed patient IDs: {failed_patient_ids[:10]}{'...' if len(failed_patient_ids) > 10 else ''}")
            _logger.error(f"This explains why {failed_count} patient injuries will fail to find their associated patients")
            
        # Log comprehensive patients migration summary
        total_processed = migrated_count + skipped_count
        self.database_id._log_migration_summary(
            'Sports Patients', 
            total_processed, 
            skipped_count, 
            skipped_reasons
        )
        
        # Verify migration by checking total count in target
        total_in_target = self.env['sports.patient'].search_count([('odoo16_patient_id', '!=', False)])
        _logger.info(f"📊 PATIENT MIGRATION SUMMARY: Source: {len(source_patients)}, Migrated: {migrated_count}, Skipped: {skipped_count}, Target total: {total_in_target}")
        return migrated_count

    def _migrate_patient_contacts(self, cursor):
        """Migrate patient emergency contacts from source database."""
        _logger.info("Starting patient contacts migration...")
        
        # Query source patient contacts (exclude contacts with null patient_id)
        cursor.execute("""
            SELECT id, patient_id, name, contact_type, mobile, sequence,
                   create_date, create_uid, write_date, write_uid
            FROM sports_patient_contact 
            WHERE patient_id IS NOT NULL
            ORDER BY id
        """)
        source_contacts = cursor.fetchall()
        
        if not source_contacts:
            _logger.info("No patient contacts found in source database")
            return 0
        
        _logger.info(f"Found {len(source_contacts)} patient contacts to migrate")
        migrated_count = 0
        skipped_count = 0
        skipped_reasons = {}
        
        for contact_data in source_contacts:
            try:
                (contact_id, patient_id, name, contact_type, mobile, sequence,
                 create_date, create_uid, write_date, write_uid) = contact_data
                
                # Find corresponding patient in target database using original Odoo 16 patient ID
                patient = self.env['sports.patient'].with_context(active_test=False).search([('odoo16_patient_id', '=', patient_id)], limit=1)
                if not patient:
                    reason = f"Patient with odoo16_patient_id {patient_id} not found"
                    _logger.warning(f"{reason} for contact {contact_id} (contact name: {name})")
                    skipped_count += 1
                    skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                    # Check if any patient exists with this ID to debug the issue
                    all_patients_with_id = self.env['sports.patient'].with_context(active_test=False).search([('odoo16_patient_id', '=', patient_id)])
                    if all_patients_with_id:
                        _logger.error(f"Found {len(all_patients_with_id)} patients with odoo16_patient_id {patient_id} - this should not happen!")
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
                reason = f"Contact creation failed: {type(e).__name__}: {str(e)}"
                self.database_id._log_skipped_item(
                    'sports.patient.contact', 
                    contact_id, 
                    reason,
                    {
                        'contact_name': name,
                        'patient_id': patient_id,
                        'contact_type': contact_type,
                        'error': str(e)
                    }
                )
                skipped_count += 1
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                continue
        
        _logger.info(f"Successfully migrated {migrated_count} patient contacts")
        
        # Log comprehensive patient contacts migration summary
        total_processed = migrated_count + skipped_count
        self.database_id._log_migration_summary(
            'Patient Contacts', 
            total_processed, 
            skipped_count, 
            skipped_reasons
        )
        
        _logger.info(f"Patient contacts migration completed: {migrated_count} contacts migrated, {skipped_count} skipped")
            
        return migrated_count

    def _migrate_team_patient_relations(self, cursor):
        """Migrate team-patient relationships from source database."""
        _logger.info("Starting team-patient relations migration...")
        
        # Query source team-patient relationships with dynamic column detection
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'sports_team_patient_rel' AND table_schema = 'public'
        """)
        available_columns = [row[0] for row in cursor.fetchall()]
        
        # Build query based on available columns
        base_columns = ['team_id', 'patient_id']
        optional_columns = ['create_date', 'create_uid', 'write_date', 'write_uid']
        query_columns = base_columns + [col for col in optional_columns if col in available_columns]
        
        cursor.execute(f"""
            SELECT {', '.join(query_columns)}
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
                # Build relation dictionary dynamically based on available columns
                relation_dict = {}
                for i, col_name in enumerate(query_columns):
                    relation_dict[col_name] = relation_data[i] if i < len(relation_data) else None
                
                team_id = relation_dict['team_id']
                patient_id = relation_dict['patient_id']
                
                # Find corresponding team and patient in target database using original Odoo 16 IDs
                # Teams are looked up by name since they don't have an odoo16_team_id field
                team_name = self._get_team_name_by_id(cursor, team_id)
                team = self.env['sports.team'].search([('name', '=', team_name)], limit=1) if team_name else None
                patient = self.env['sports.patient'].with_context(active_test=False).search([('odoo16_patient_id', '=', patient_id)], limit=1)
                
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
        return None

    def _get_team_name_by_id(self, cursor, team_id):
        """Helper method to get team name by ID from source database."""
        cursor.execute("SELECT name FROM sports_team WHERE id = %s", (team_id,))
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
