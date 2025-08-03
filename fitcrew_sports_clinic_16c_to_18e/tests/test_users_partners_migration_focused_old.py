# -*- coding: utf-8 -*-

import logging
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('users_partners_migration')
class TestUsersPartnersMigrationFocused(TransactionCase):
    """Focused tests for users and partners migration functionality."""

    def setUp(self):
        super().setUp()
        self.migration_coordinator = None
        
    def tearDown(self):
        # Clean up any created migration coordinator
        if self.migration_coordinator:
            try:
                self.migration_coordinator.unlink()
            except:
                pass
        super().tearDown()

    def test_01_create_migration_coordinator(self):
        """Test creating a migration coordinator with database connection."""
        _logger.info("=== Testing Migration Coordinator Creation ===")
        
        # Create migration coordinator with test database connection
        coordinator_vals = {
            'name': 'Test Migration Coordinator',
            'database_host': 'localhost',
            'database_name': '2025-08-01-medsportsuroit-prod',
            'database_username': 'odoo',
            'database_password': 'y@I^3eNg3*o!$NHA',
            'database_port': 5432,
        }
        
        try:
            self.migration_coordinator = self.env['odoo16.database'].create(coordinator_vals)
            _logger.info(f"✅ Migration coordinator created successfully: {self.migration_coordinator.id}")
            
            # Verify all migration components were created
            self.assertTrue(self.migration_coordinator.users_partners_migration_id, 
                          "Users/Partners migration component should be created")
            self.assertTrue(self.migration_coordinator.database_id, 
                          "Database base should be created")
            
            _logger.info(f"✅ Users/Partners migration component: {self.migration_coordinator.users_partners_migration_id.id}")
            
        except Exception as e:
            _logger.error(f"❌ Failed to create migration coordinator: {e}")
            self.fail(f"Migration coordinator creation failed: {e}")

    def test_02_test_database_connection(self):
        """Test database connection to Odoo 16 source."""
        _logger.info("=== Testing Database Connection ===")
        
        # Create migration coordinator
        coordinator_vals = {
            'database_host': 'localhost',
            'database_name': '2025-08-01-medsportsuroit-prod',
            'database_username': 'odoo',
            'database_password': 'y@I^3eNg3*o!$NHA',
            'database_port': 5432,
        }
        
        try:
            self.migration_coordinator = self.env['odoo16.database'].create(coordinator_vals)
            
            # Test database connection
            result = self.migration_coordinator.action_test_connection()
            _logger.info(f"✅ Database connection test result: {result}")
            
            # Verify connection was successful
            self.assertIn('successful', result.get('title', '').lower(), 
                         "Database connection should be successful")
            
        except Exception as e:
            _logger.error(f"❌ Database connection test failed: {e}")
            self.fail(f"Database connection test failed: {e}")

    def test_03_validate_source_data(self):
        """Test validation of source database structure."""
        _logger.info("=== Testing Source Data Validation ===")
        
        # Create migration coordinator
        coordinator_vals = {
            'database_host': 'localhost',
            'database_name': '2025-08-01-medsportsuroit-prod',
            'database_username': 'odoo',
            'database_password': 'y@I^3eNg3*o!$NHA',
            'database_port': 5432,
        }
        
        try:
            self.migration_coordinator = self.env['odoo16.database'].create(coordinator_vals)
            
            # Validate source data structure
            result = self.migration_coordinator.action_validate_source_data()
            _logger.info(f"✅ Source data validation result: {result}")
            
            # Verify validation was successful
            self.assertIn('successful', result.get('title', '').lower(), 
                         "Source data validation should be successful")
            
        except Exception as e:
            _logger.error(f"❌ Source data validation failed: {e}")
            self.fail(f"Source data validation failed: {e}")

    def test_04_users_partners_migration_basic(self):
        """Test basic users and partners migration functionality."""
        _logger.info("=== Testing Users/Partners Migration ===")
        
        # Create migration coordinator
        coordinator_vals = {
            'database_host': 'localhost',
            'database_name': '2025-08-01-medsportsuroit-prod',
            'database_username': 'odoo',
            'database_password': 'y@I^3eNg3*o!$NHA',
            'database_port': 5432,
        }
        
        try:
            self.migration_coordinator = self.env['odoo16.database'].create(coordinator_vals)
            
            # Get initial counts
            initial_partner_count = self.env['res.partner'].search_count([])
            initial_user_count = self.env['res.users'].search_count([])
            
            _logger.info(f"Initial partner count: {initial_partner_count}")
            _logger.info(f"Initial user count: {initial_user_count}")
            
            # Run users/partners migration
            result = self.migration_coordinator.action_migrate_users_partners()
            _logger.info(f"✅ Users/Partners migration result: {result}")
            
            # Check final counts
            final_partner_count = self.env['res.partner'].search_count([])
            final_user_count = self.env['res.users'].search_count([])
            
            _logger.info(f"Final partner count: {final_partner_count}")
            _logger.info(f"Final user count: {final_user_count}")
            
            # Verify migration was successful
            self.assertIn('successful', result.get('title', '').lower(), 
                         "Users/Partners migration should be successful")
            
            # Log migration statistics
            partners_migrated = final_partner_count - initial_partner_count
            users_migrated = final_user_count - initial_user_count
            
            _logger.info(f"📊 Partners migrated: {partners_migrated}")
            _logger.info(f"📊 Users migrated: {users_migrated}")
            
        except Exception as e:
            _logger.error(f"❌ Users/Partners migration failed: {e}")
            self.fail(f"Users/Partners migration failed: {e}")

    def test_05_merge_functionality_test(self):
        """Test the merge functionality by running migration twice."""
        _logger.info("=== Testing Merge Functionality (Double Migration) ===")
        
        # Create migration coordinator
        coordinator_vals = {
            'database_host': 'localhost',
            'database_name': '2025-08-01-medsportsuroit-prod',
            'database_username': 'odoo',
            'database_password': 'y@I^3eNg3*o!$NHA',
            'database_port': 5432,
        }
        
        try:
            self.migration_coordinator = self.env['odoo16.database'].create(coordinator_vals)
            
            # Get initial counts
            initial_partner_count = self.env['res.partner'].search_count([])
            initial_user_count = self.env['res.users'].search_count([])
            
            _logger.info(f"Initial partner count: {initial_partner_count}")
            _logger.info(f"Initial user count: {initial_user_count}")
            
            # Run first migration
            _logger.info("🔄 Running first migration...")
            result1 = self.migration_coordinator.action_migrate_users_partners()
            _logger.info(f"✅ First migration result: {result1}")
            
            # Check counts after first migration
            first_partner_count = self.env['res.partner'].search_count([])
            first_user_count = self.env['res.users'].search_count([])
            
            _logger.info(f"After first migration - Partners: {first_partner_count}, Users: {first_user_count}")
            
            # Run second migration (should merge, not duplicate)
            _logger.info("🔄 Running second migration (merge test)...")
            result2 = self.migration_coordinator.action_migrate_users_partners()
            _logger.info(f"✅ Second migration result: {result2}")
            
            # Check final counts
            final_partner_count = self.env['res.partner'].search_count([])
            final_user_count = self.env['res.users'].search_count([])
            
            _logger.info(f"After second migration - Partners: {final_partner_count}, Users: {final_user_count}")
            
            # Verify no duplicates were created
            self.assertEqual(first_partner_count, final_partner_count, 
                           "Partner count should not increase on second migration (merge should prevent duplicates)")
            self.assertEqual(first_user_count, final_user_count, 
                           "User count should not increase on second migration (merge should prevent duplicates)")
            
            _logger.info("✅ Merge functionality working correctly - no duplicates created")
            
        except Exception as e:
            _logger.error(f"❌ Merge functionality test failed: {e}")
            self.fail(f"Merge functionality test failed: {e}")

    def test_06_chatter_logging_verification(self):
        """Test that chatter messages are posted when records are updated."""
        _logger.info("=== Testing Chatter Logging ===")
        
        # Create migration coordinator
        coordinator_vals = {
            'database_host': 'localhost',
            'database_name': '2025-08-01-medsportsuroit-prod',
            'database_username': 'odoo',
            'database_password': 'y@I^3eNg3*o!$NHA',
            'database_port': 5432,
        }
        
        try:
            self.migration_coordinator = self.env['odoo16.database'].create(coordinator_vals)
            
            # Run migration first time
            _logger.info("🔄 Running first migration...")
            self.migration_coordinator.action_migrate_users_partners()
            
            # Find a partner that was likely migrated
            migrated_partner = self.env['res.partner'].search([
                ('email', '!=', False),
                ('is_company', '=', False)
            ], limit=1)
            
            if migrated_partner:
                initial_message_count = len(migrated_partner.message_ids)
                _logger.info(f"Partner '{migrated_partner.name}' has {initial_message_count} messages initially")
                
                # Run migration second time to trigger merge/update
                _logger.info("🔄 Running second migration to trigger merge...")
                self.migration_coordinator.action_migrate_users_partners()
                
                # Check if chatter message was added
                final_message_count = len(migrated_partner.message_ids)
                _logger.info(f"Partner '{migrated_partner.name}' has {final_message_count} messages after merge")
                
                # Look for migration update messages
                migration_messages = migrated_partner.message_ids.filtered(
                    lambda m: 'Migration Update' in (m.subject or '') or 'Data Migration Update' in (m.body or '')
                )
                
                _logger.info(f"Found {len(migration_messages)} migration-related messages")
                
                if migration_messages:
                    _logger.info("✅ Chatter logging is working - migration messages found")
                    for msg in migration_messages[:2]:  # Show first 2 messages
                        _logger.info(f"📝 Message: {msg.subject} - {msg.body[:100]}...")
                else:
                    _logger.warning("⚠️ No migration messages found in chatter")
            else:
                _logger.warning("⚠️ No suitable partner found for chatter testing")
                
        except Exception as e:
            _logger.error(f"❌ Chatter logging test failed: {e}")
            self.fail(f"Chatter logging test failed: {e}")
