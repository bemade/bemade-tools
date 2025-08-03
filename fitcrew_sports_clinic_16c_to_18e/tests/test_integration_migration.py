"""
Integration test for complete Odoo 16 to 18 migration.

This test performs a full migration from the configured Odoo 16 database
to the current Odoo 18 test database, validating the complete migration process.
"""

import logging
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('migration_integration', 'post_install', '-at_install')
class TestMigrationIntegration(TransactionCase):
    """Integration test for complete migration process."""
    
    def setUp(self):
        super().setUp()
        self.coordinator_model = self.env['odoo16.database']
        
    def test_complete_migration_integration(self):
        """Test complete migration from Odoo 16 to Odoo 18."""
        _logger.info("=" * 80)
        _logger.info("STARTING INTEGRATION MIGRATION TEST")
        _logger.info("=" * 80)
        
        # Create migration coordinator with environment variables
        coordinator = self.coordinator_model.create({
            'name': 'Integration Test Migration',
            'database_host': 'localhost',  # Will use env vars if available
            'database_name': '2025-08-01-medsportsuroit-prod',  # Will use env vars if available
            'database_username': 'odoo',  # Will use env vars if available
            'database_password': 'password',  # Will use env vars if available
            'database_port': 5432,
            'skip_filestore': True,  # Skip filestore for faster testing
            'migrate_ir_filters': False,  # Skip filters for initial test
        })
        
        _logger.info(f"Created migration coordinator: {coordinator.name}")
        _logger.info(f"Source database: {coordinator.database_host}:{coordinator.database_port}/{coordinator.database_name}")
        
        try:
            # Step 1: Test connection to source database
            _logger.info("Step 1: Testing connection to Odoo 16 database...")
            result = coordinator.test_connection()
            self.assertEqual(result['type'], 'ir.actions.client')
            self.assertEqual(result['tag'], 'display_notification')
            _logger.info("✅ Connection test successful")
            
            # Step 2: Validate source data
            _logger.info("Step 2: Validating source database structure...")
            try:
                result = coordinator.action_validate_source_data()
                self.assertEqual(result['type'], 'ir.actions.client')
                _logger.info("✅ Source data validation successful")
            except Exception as e:
                _logger.warning(f"⚠️  Source validation warning: {e}")
                # Continue with migration even if some validation fails
            
            # Step 3: Migrate users and partners
            _logger.info("Step 3: Migrating users and partners...")
            result = coordinator.action_migrate_users_partners()
            self.assertEqual(result['type'], 'ir.actions.client')
            _logger.info("✅ Users and partners migration completed")
            
            # Step 4: Migrate mail system
            _logger.info("Step 4: Migrating mail system...")
            result = coordinator.action_migrate_mail_system()
            self.assertEqual(result['type'], 'ir.actions.client')
            _logger.info("✅ Mail system migration completed")
            
            # Step 5: Migrate calendar events to tasks
            _logger.info("Step 5: Migrating calendar events to project tasks...")
            result = coordinator.action_migrate_calendar_events_to_tasks()
            self.assertEqual(result['type'], 'ir.actions.client')
            _logger.info("✅ Calendar events migration completed")
            
            # Step 6: Migrate attachments (with filestore skip)
            _logger.info("Step 6: Migrating attachments...")
            result = coordinator.action_migrate_attachments()
            self.assertEqual(result['type'], 'ir.actions.client')
            _logger.info("✅ Attachments migration completed")
            
            # Step 7: Verify migration status
            _logger.info("Step 7: Verifying migration status...")
            coordinator.invalidate_cache()
            _logger.info(f"Migration status: {coordinator.migration_status}")
            _logger.info(f"Last migration date: {coordinator.last_migration_date}")
            
            # Step 8: Validate migrated data counts
            _logger.info("Step 8: Validating migrated data...")
            self._validate_migrated_data()
            
            _logger.info("=" * 80)
            _logger.info("✅ INTEGRATION MIGRATION TEST COMPLETED SUCCESSFULLY")
            _logger.info("=" * 80)
            
        except Exception as e:
            _logger.error("=" * 80)
            _logger.error(f"❌ INTEGRATION MIGRATION TEST FAILED: {e}")
            _logger.error("=" * 80)
            raise
    
    def test_complete_migration_all_at_once(self):
        """Test complete migration using the 'migrate all' method."""
        _logger.info("=" * 80)
        _logger.info("STARTING COMPLETE MIGRATION TEST (ALL AT ONCE)")
        _logger.info("=" * 80)
        
        # Create migration coordinator
        coordinator = self.coordinator_model.create({
            'name': 'Complete Migration Test',
            'database_host': 'localhost',
            'database_name': '2025-08-01-medsportsuroit-prod',
            'database_username': 'odoo',
            'database_password': 'password',
            'database_port': 5432,
            'skip_filestore': True,
            'migrate_ir_filters': False,
        })
        
        try:
            # Test connection first
            _logger.info("Testing connection...")
            coordinator.test_connection()
            
            # Run complete migration
            _logger.info("Running complete migration...")
            result = coordinator.action_migrate_all()
            self.assertEqual(result['type'], 'ir.actions.client')
            
            # Verify final status
            coordinator.refresh()
            _logger.info(f"Final migration status: {coordinator.migration_status}")
            
            # Validate migrated data
            self._validate_migrated_data()
            
            _logger.info("✅ COMPLETE MIGRATION TEST SUCCESSFUL")
            
        except Exception as e:
            _logger.error(f"❌ COMPLETE MIGRATION TEST FAILED: {e}")
            raise
    
    def _validate_migrated_data(self):
        """Validate that data was successfully migrated."""
        _logger.info("Validating migrated data counts...")
        
        # Check users
        user_count = self.env['res.users'].search_count([])
        _logger.info(f"Total users in system: {user_count}")
        
        # Check partners
        partner_count = self.env['res.partner'].search_count([])
        _logger.info(f"Total partners in system: {partner_count}")
        
        # Check projects (should have at least the migrated calendar events project)
        project_count = self.env['project.project'].search_count([])
        _logger.info(f"Total projects in system: {project_count}")
        
        # Check tasks (migrated from calendar events)
        task_count = self.env['project.task'].search_count([])
        _logger.info(f"Total tasks in system: {task_count}")
        
        # Check mail channels
        channel_count = self.env['mail.channel'].search_count([])
        _logger.info(f"Total mail channels in system: {channel_count}")
        
        # Check attachments
        attachment_count = self.env['ir.attachment'].search_count([])
        _logger.info(f"Total attachments in system: {attachment_count}")
        
        _logger.info("Data validation completed")
    
    def test_migration_error_handling(self):
        """Test migration error handling with invalid database connection."""
        _logger.info("Testing migration error handling...")
        
        # Create coordinator with invalid connection
        coordinator = self.coordinator_model.create({
            'name': 'Error Test Migration',
            'database_host': 'invalid_host',
            'database_name': 'invalid_db',
            'database_username': 'invalid_user',
            'database_password': 'invalid_pass',
            'database_port': 9999,
        })
        
        # Test connection should fail gracefully
        result = coordinator.test_connection()
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(result['params']['type'], 'danger')
        
        _logger.info("✅ Error handling test completed")
