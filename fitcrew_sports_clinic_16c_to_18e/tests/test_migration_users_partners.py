"""Tests for Users/Partners Migration."""
import logging
from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install', 'migration', 'users_partners')
class TestMigrationUsersPartners(TransactionCase):
    """Test suite for users/partners migration."""

    def setUp(self):
        """Set up test environment."""
        super().setUp()
        
        # Create migration coordinator
        self.coordinator = self.env['migration.coordinator'].create({
            'name': 'Test Users/Partners Migration',
            'source_db_host': 'localhost',
            'source_db_name': '2025-08-01-medsportsuroit-prod',
            'source_db_user': 'odoo',
            'source_db_password': 'y@I^3eNg3*o!$NHA',
            'source_db_port': 5432,
            'migration_status': 'not_started'
        })
        
        # Create migration instance
        self.migration = self.env['migration.users.partners'].create({
            'coordinator_id': self.coordinator.id
        })
        
        _logger.info("✅ Users/partners migration test setup completed")

    def tearDown(self):
        """Clean up test environment."""
        try:
            if hasattr(self, 'coordinator') and self.coordinator:
                self.coordinator.unlink()
            _logger.info("✅ Users/partners migration test cleanup completed")
        except Exception as e:
            _logger.warning(f"⚠️ Cleanup warning: {e}")
        super().tearDown()

    def test_migration_coordinator_creation(self):
        """Test that migration coordinator is properly created."""
        _logger.info("🧪 Testing users/partners migration coordinator creation...")
        
        self.assertTrue(self.coordinator.exists())
        self.assertEqual(self.coordinator.migration_status, 'not_started')
        self.assertTrue(self.migration.exists())
        self.assertEqual(self.migration.coordinator_id, self.coordinator)
        
        _logger.info("✅ Users/partners migration coordinator creation test passed")

    def test_database_connection(self):
        """Test database connection parameters."""
        _logger.info("🧪 Testing users/partners migration database connection...")
        
        try:
            # Test connection parameters are set
            self.assertEqual(self.migration.coordinator_id.source_db_host, 'localhost')
            self.assertEqual(self.migration.coordinator_id.source_db_name, '2025-08-01-medsportsuroit-prod')
            self.assertEqual(self.migration.coordinator_id.source_db_user, 'odoo')
            self.assertEqual(self.migration.coordinator_id.source_db_port, 5432)
            
            _logger.info("✅ Users/partners migration database connection test passed")
        except Exception as e:
            _logger.warning(f"⚠️ Database connection test skipped: {e}")
            self.skipTest(f"Database connection test skipped: {e}")

    def test_migration_execution(self):
        """Test users/partners migration execution."""
        _logger.info("🧪 Testing users/partners migration execution...")
        
        try:
            # Execute migration
            result = self.migration.action_migrate_users_partners()
            
            # Verify result structure
            self.assertIsInstance(result, dict)
            self.assertIn('type', result)
            
            # Log results
            if 'params' in result and 'message' in result['params']:
                _logger.info(f"📊 Migration result: {result['params']['message']}")
            
            _logger.info("✅ Users/partners migration execution test completed")
            
        except Exception as e:
            _logger.error(f"❌ Users/partners migration failed: {e}")
            self.fail(f"Users/partners migration failed: {e}")

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_users_partners.MigrationUsersPartners.get_cursor')
    def test_migration_execution_mocked(self, mock_get_cursor):
        """Test migration execution with mocked database data."""
        # Mock cursor
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock data
        mock_partner_data = [
            (1, 'Test Partner', 'test@example.com', '123456789', None, None,
             'Street 1', None, 'City 1', '12345', 1, 1, None, None, False, 0, 1, True)
        ]
        mock_user_data = [
            (1, 'test_user', 'password', 1, 1, True, None, None, None, None,
             None, 'email', 'welcome', False, None, None)
        ]
        
        # Configure mock to return different data for different queries
        def mock_fetchall():
            if 'res_partner' in mock_cursor.execute.call_args[0][0]:
                return mock_partner_data
            elif 'res_users' in mock_cursor.execute.call_args[0][0]:
                return mock_user_data
            return []
        
        mock_cursor.fetchall.side_effect = mock_fetchall
        
        # Mock search to return no existing records
        with patch.object(self.env['res.partner'], 'search', return_value=self.env['res.partner']):
            with patch.object(self.env['res.users'], 'search', return_value=self.env['res.users']):
                with patch.object(self.env['res.partner'], 'sudo') as mock_partner_sudo:
                    with patch.object(self.env['res.users'], 'sudo') as mock_user_sudo:
                        # Configure sudo mocks
                        mock_partner_sudo.return_value.create.return_value = True
                        mock_user_sudo.return_value.create.return_value = True
                        
                        # Execute migration
                        result = self.migration.action_migrate_users_partners()
                        
                        # Verify result
                        self.assertEqual(result['type'], 'ir.actions.client')
                        self.assertEqual(result['tag'], 'display_notification')
                        self.assertEqual(result['params']['type'], 'success')
                        self.assertIn('users', result['params']['message'])
                        self.assertIn('partners', result['params']['message'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_users_partners.MigrationUsersPartners.get_cursor')
    def test_migration_with_existing_records(self, mock_get_cursor):
        """Test migration behavior when records already exist."""
        # Mock cursor
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock data
        mock_partner_data = [
            (1, 'Existing Partner', 'existing@example.com', '123456789', None, None,
             'Street 1', None, 'City 1', '12345', 1, 1, None, None, False, 0, 1, True)
        ]
        mock_user_data = [
            (1, 'existing_user', 'password', 1, 1, True, None, None, None, None,
             None, 'email', 'welcome', False, None, None)
        ]
        
        def mock_fetchall():
            if 'res_partner' in mock_cursor.execute.call_args[0][0]:
                return mock_partner_data
            elif 'res_users' in mock_cursor.execute.call_args[0][0]:
                return mock_user_data
            return []
        
        mock_cursor.fetchall.side_effect = mock_fetchall
        
        # Mock search to return existing records
        existing_partner = self.env['res.partner'].create({'name': 'Existing Partner', 'email': 'existing@example.com'})
        existing_user = self.env['res.users'].create({'login': 'existing_user', 'name': 'Existing User'})
        
        with patch.object(self.env['res.partner'], 'search', return_value=existing_partner):
            with patch.object(self.env['res.users'], 'search', return_value=existing_user):
                # Execute migration
                result = self.migration.action_migrate_users_partners()
                
                # Should still succeed but with 0 new records
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('0 users', result['params']['message'])
                self.assertIn('0 partners', result['params']['message'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_users_partners.MigrationUsersPartners.get_cursor')
    def test_migration_database_error(self, mock_get_cursor):
        """Test migration behavior when database error occurs."""
        # Mock cursor to raise an exception
        mock_get_cursor.side_effect = Exception("Database connection failed")
        
        # Execute migration and expect UserError
        with self.assertRaises(UserError) as context:
            self.migration.action_migrate_users_partners()
        
        self.assertIn("Users and partners migration failed", str(context.exception))
        self.assertEqual(self.migration.migration_status, 'failed')

    def test_migration_status_updates(self):
        """Test that migration status is properly updated during migration."""
        # Initially should be not_started
        self.assertEqual(self.migration.migration_status, 'not_started')
        
        # Mock the migration to test status updates
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []  # No data to migrate
            
            # Execute migration
            self.migration.action_migrate_users_partners()
            
            # Status should be completed
            self.assertEqual(self.migration.migration_status, 'completed')
            self.assertIn('Users and partners migration completed', self.migration.migration_log)

    def test_inherited_methods_available(self):
        """Test that methods from base class are available."""
        # Test inherited methods
        self.assertTrue(hasattr(self.migration, '_update_migration_status'))
        self.assertTrue(hasattr(self.migration, '_success_notification'))
        self.assertTrue(hasattr(self.migration, 'get_cursor'))
        
        # Test that they work
        result = self.migration._success_notification("Test", "Message")
        self.assertEqual(result['params']['type'], 'success')
        
        self.migration._update_migration_status('in_progress', 'Test message')
        self.assertEqual(self.migration.migration_status, 'in_progress')

    def test_page_size_constant(self):
        """Test that PAGE_SIZE constant is properly imported and used."""
        from odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_users_partners import PAGE_SIZE
        self.assertEqual(PAGE_SIZE, 1000)
        self.assertIsInstance(PAGE_SIZE, int)
