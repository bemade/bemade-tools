# -*- coding: utf-8 -*-
"""
Sports Teams Migration Module for Odoo 16 to 18 Migration
Handles migration of sports.team records and related data.
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase
import logging

_logger = logging.getLogger(__name__)


class MigrationSportsTeams(models.Model):
    """Migration component for sports teams and related data."""
    _name = 'migration.sports.teams'
    _description = 'Sports Teams Migration'
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
    teams_migrated = fields.Integer(string='Teams Migrated', default=0, readonly=True)
    team_staff_migrated = fields.Integer(string='Team Staff Migrated', default=0, readonly=True)
    
    def action_migrate_sports_teams(self):
        """Migrate sports teams from Odoo 16 to Odoo 18."""
        try:
            self._update_migration_status('in_progress', 'Starting sports teams migration')
            
            # Use the inherited cursor method from base class
            with self.get_cursor() as cr:
                # Migrate teams first (foundational)
                teams_count = self._migrate_teams(cr)
                
                # Migrate team staff relationships with detailed logging
                _logger.info("Starting staff migration after successful teams migration")
                staff_count = self._migrate_team_staff(cr)
                _logger.info(f"Staff migration completed, migrated {staff_count} staff relationships")
                
                # Update statistics
                self.teams_migrated = teams_count
                self.team_staff_migrated = staff_count
            
            message = f"Sports teams migration completed: {teams_count} teams, {staff_count} staff relationships migrated"
            self._update_migration_status('completed', message)
            
            # Commit the transaction to ensure teams are persisted before dependent migrations
            self.env.cr.commit()
            _logger.info(f"✅ Sports teams migration transaction committed - {teams_count} teams and {staff_count} staff relationships persisted")
            
            return self._success_notification("Sports Teams Migration", message)
            
        except Exception as e:
            error_msg = f"Sports teams migration failed: {str(e)}"
            _logger.error(error_msg, exc_info=True)
            self._update_migration_status('failed', error_msg)
            raise UserError(_(error_msg))
    
    def _migrate_teams(self, cursor):
        """Migrate sports.team records."""
        _logger.info("Starting sports teams migration...")
        
        # Query source teams
        cursor.execute("""
            SELECT id, name, parent_id, create_date, create_uid, write_date, write_uid,
                   head_coach_id, head_therapist_id, website
            FROM sports_team 
            ORDER BY id
        """)
        source_teams = cursor.fetchall()
        
        if not source_teams:
            _logger.info("No sports teams found in source database")
            return 0
        
        teams_count = 0
        target_env = self.env
        
        for team_data in source_teams:
            team_id, name, parent_id, create_date, create_uid, write_date, write_uid, head_coach_id, head_therapist_id, website = team_data
            
            try:
                # Prepare team values
                team_vals = {
                    'name': name,
                    'parent_id': parent_id,  # Will be validated by Odoo
                }
                
                # Create team directly (no deduplication)
                new_team = target_env['sports.team'].create(team_vals)
                _logger.info(f"Created team '{name}' (ID: {new_team.id})")
                
                # Add chatter message
                if hasattr(new_team, 'message_post'):
                    new_team.message_post(
                        body="Team migrated from Odoo 16",
                        message_type='comment'
                    )
                
                teams_count += 1
                
            except Exception as e:
                _logger.error(f"Failed to migrate team '{name}': {str(e)}")
                continue
        
        _logger.info(f"Sports teams migration completed: {teams_count} teams processed")
        return teams_count
    
    def _migrate_team_staff(self, cursor):
        """Migrate team staff relationships from source database"""
        _logger.info("Starting team staff migration...")
        
        # Get target environment
        target_env = self.env
        
        # Query source database for team staff data
        _logger.info("Executing source database query for team staff...")
        cursor.execute("""
            SELECT id, team_id, partner_id, role, sequence, 
                   create_date, create_uid, write_date, write_uid
            FROM sports_team_staff
            ORDER BY id
        """)
        
        source_staff = cursor.fetchall()
        _logger.info(f"Found {len(source_staff)} staff relationships to migrate")
        
        migrated_count = 0
        
        for i, staff_data in enumerate(source_staff):
            staff_id, team_id, partner_id, role, sequence, create_date, create_uid, write_date, write_uid = staff_data
            _logger.info(f"Processing staff {i+1}/{len(source_staff)}: staff_id={staff_id}, team_id={team_id}, partner_id={partner_id}")
            
            try:
                with target_env.cr.savepoint():
                    _logger.debug(f"Created savepoint for staff {staff_id}")
                    
                    # Find the target team by name (since IDs may differ)
                    _logger.debug(f"Looking up team name for team_id {team_id}")
                    team_name = self._get_team_name_by_id(cursor, team_id)
                    if not team_name:
                        _logger.warning(f"Could not find team with ID {team_id} for staff {staff_id}")
                        continue
                    _logger.debug(f"Found team name: '{team_name}'")
                    
                    _logger.debug(f"Searching for target team '{team_name}'")
                    target_team = target_env['sports.team'].search([('name', '=', team_name)], limit=1)
                    if not target_team:
                        _logger.warning(f"Could not find target team '{team_name}' for staff {staff_id}")
                        continue
                    _logger.debug(f"Found target team: {target_team.id}")
                    
                    # Find the target partner using indexed lookup on original Odoo 16 partner ID
                    # Include inactive partners since team staff relationships should be preserved
                    _logger.debug(f"Looking up partner by odoo16_partner_id={partner_id}")
                    target_partner = target_env['res.partner'].with_context(active_test=False).search([('odoo16_partner_id', '=', partner_id)], limit=1)
                    if not target_partner:
                        # Fallback: Search for deduplicated partner using source partner data
                        _logger.warning(f"Partner with odoo16_partner_id={partner_id} not found for staff {staff_id}, searching for deduplicated partner...")
                        
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
                                target_partner = target_env['res.partner'].with_context(active_test=False).search([
                                    ('email', '=', source_email)
                                ], limit=1)
                                if target_partner:
                                    _logger.warning(f"✅ Found deduplicated partner {target_partner.id} for staff {staff_id} via email: {source_email}")
                            
                            # If not found by email, try by name + phone/mobile
                            if not target_partner and source_name:
                                search_domain = [('name', '=', source_name)]
                                if source_phone:
                                    search_domain.append(('phone', '=', source_phone))
                                elif source_mobile:
                                    search_domain.append(('mobile', '=', source_mobile))
                                
                                if len(search_domain) > 1:  # Only search if we have name + phone/mobile
                                    target_partner = target_env['res.partner'].with_context(active_test=False).search(search_domain, limit=1)
                                    if target_partner:
                                        _logger.warning(f"✅ Found deduplicated partner {target_partner.id} for staff {staff_id} via name+phone: {source_name}")
                        
                        if not target_partner:
                            _logger.warning(f"❌ Could not find deduplicated partner for staff {staff_id} (source partner_id: {partner_id}) - skipping")
                            continue
                    _logger.debug(f"Found target partner: {target_partner.id} - {target_partner.name}")
                    
                    # Create staff relationship
                    staff_vals = {
                        'team_id': target_team.id,
                        'partner_id': target_partner.id,  # Use target partner ID, not source partner ID
                        'role': role,
                        'sequence': sequence,
                    }
                    _logger.debug(f"Staff values: {staff_vals}")
                    
                    # Check if relationship already exists
                    _logger.debug(f"Checking for existing staff relationship")
                    existing_staff = target_env['sports.team.staff'].search([
                        ('team_id', '=', target_team.id),
                        ('partner_id', '=', target_partner.id)  # Use target partner ID, not source partner ID
                    ], limit=1)
                    
                    if existing_staff:
                        _logger.debug(f"Updating existing staff relationship {existing_staff.id}")
                        try:
                            existing_staff.write(staff_vals)
                            _logger.info(f"Updated existing staff relationship for team '{team_name}' and partner {target_partner.name}")
                        except Exception as write_error:
                            _logger.error(f"Failed to update staff relationship {existing_staff.id}: {write_error}")
                            raise  # Re-raise to trigger the outer exception handler
                    else:
                        _logger.debug(f"Creating new staff relationship")
                        try:
                            new_staff = target_env['sports.team.staff'].create(staff_vals)
                            _logger.info(f"Created staff relationship {new_staff.id} for team '{team_name}' and partner {target_partner.name}")
                        except Exception as create_error:
                            _logger.error(f"Failed to create staff relationship: {create_error}")
                            raise  # Re-raise to trigger the outer exception handler
                    
                    migrated_count += 1
                    _logger.debug(f"Successfully processed staff {staff_id}, total migrated: {migrated_count}")
                    
            except Exception as e:
                _logger.error(f"Error migrating staff {staff_id} (team_id: {team_id}, partner_id: {partner_id}): {e}")
                _logger.error(f"Exception type: {type(e).__name__}")
                _logger.error(f"Exception details: {str(e)}")
                continue
        
        _logger.info(f"Team staff migration completed. Migrated {migrated_count} staff relationships")
        return migrated_count
    
    def _get_team_name_by_id(self, cursor, team_id):
        """Helper method to get team name by ID from source database."""
        cursor.execute("SELECT name FROM sports_team WHERE id = %s", (team_id,))
        result = cursor.fetchone()
        return result[0] if result else None
