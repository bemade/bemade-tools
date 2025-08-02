from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError
import os
import logging
from unittest.mock import patch, MagicMock

_logger = logging.getLogger(__name__)


class TestMigrationUsersPartners(TransactionCase):
    def setUp(self):
        super().setUp()
        # Create database base record
        self.database_base = self.env["odoo16.database.base"].create({
            "database_host": os.environ.get("ODOO16_HOST", "localhost"),
            "database_name": os.environ.get("ODOO16_DBNAME", "test_db"),
            "database_username": os.environ.get("ODOO16_USER", "odoo"),
            "database_password": os.environ.get("ODOO16_PASSWORD", ""),
            "database_port": int(os.environ.get("ODOO16_PORT", "5432")),
        })
        
        # Create users & partners migration record
        self.migration = self.env["migration.users.partners"].create({
            "database_id": self.database_base.id
        })

    def test_migration_creation(self):
        """Test that migration record can be created properly."""
        self.assertTrue(self.migration.id)
        self.assertEqual(self.migration.database_id, self.database_base)
        
        # Test inherited fields from base
        self.assertEqual(self.migration.database_host, self.database_base.database_host)
        self.assertEqual(self.migration.database_name, self.database_base.database_name)
        self.assertEqual(self.migration.migration_status, 'not_started')

    def test_migration_method_exists(self):
        """Test that migration method exists and is callable."""
        self.assertTrue(hasattr(self.migration, 'action_migrate_users_partners'))
        self.assertTrue(callable(getattr(self.migration, 'action_migrate_users_partners')))

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_users_partners.MigrationUsersPartners.get_cursor')
    def test_migration_with_mock_data(self, mock_get_cursor):
        """Test migration with mocked database data."""
        # Mock cursor and database data
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock partner data
        mock_partner_data = [
            (1, 'Test Partner 1', 'test1@example.com', '123456789', None, None, 
             'Street 1', None, 'City 1', '12345', 1, 1, None, None, False, 0, 1, True),
            (2, 'Test Partner 2', 'test2@example.com', '987654321', None, None,
             'Street 2', None, 'City 2', '54321', 1, 1, None, None, True, 1, 0, True)
        ]
        
        # Mock user data
        mock_user_data = [
            (1, 'user1', 'password1', 1, 1, True, None, None, None, None, 
             None, 'email', 'welcome', False, None, None),
            (2, 'user2', 'password2', 2, 1, True, None, None, None, None,
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
