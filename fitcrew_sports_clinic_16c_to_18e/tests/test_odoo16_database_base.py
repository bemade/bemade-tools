from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError
import os
import logging
import tempfile

_logger = logging.getLogger(__name__)


class TestOdoo16DatabaseBase(TransactionCase):
    def setUp(self):
        super().setUp()
        # Create test database base record with environment variables or defaults
        self.database_base = self.env["odoo16.database.base"].create({
            "database_host": os.environ.get("ODOO16_HOST", "localhost"),
            "database_name": os.environ.get("ODOO16_DBNAME", "test_db"),
            "database_username": os.environ.get("ODOO16_USER", "odoo"),
            "database_password": os.environ.get("ODOO16_PASSWORD", ""),
            "database_port": int(os.environ.get("ODOO16_PORT", "5432")),
        })

    def test_database_base_creation(self):
        """Test that database base record can be created with required fields."""
        self.assertTrue(self.database_base.id)
        self.assertEqual(self.database_base.database_host, os.environ.get("ODOO16_HOST", "localhost"))
        self.assertEqual(self.database_base.database_name, os.environ.get("ODOO16_DBNAME", "test_db"))
        self.assertEqual(self.database_base.database_username, os.environ.get("ODOO16_USER", "odoo"))
        self.assertEqual(self.database_base.database_port, int(os.environ.get("ODOO16_PORT", "5432")))

    def test_migration_status_default(self):
        """Test that migration status defaults to 'not_started'."""
        self.assertEqual(self.database_base.migration_status, 'not_started')

    def test_update_migration_status(self):
        """Test migration status update functionality."""
        # Test status update
        self.database_base._update_migration_status('in_progress', 'Starting test migration')
        self.assertEqual(self.database_base.migration_status, 'in_progress')
        self.assertIn('Starting test migration', self.database_base.migration_log)
        
        # Test another status update
        self.database_base._update_migration_status('completed', 'Test migration completed')
        self.assertEqual(self.database_base.migration_status, 'completed')
        self.assertIn('Test migration completed', self.database_base.migration_log)
        self.assertIn('Starting test migration', self.database_base.migration_log)

    def test_success_notification(self):
        """Test success notification method."""
        result = self.database_base._success_notification("Test Title", "Test Message")
        
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(result['params']['title'], "Test Title")
        self.assertEqual(result['params']['message'], "Test Message")
        self.assertEqual(result['params']['type'], 'success')
        self.assertFalse(result['params']['sticky'])

    def test_error_notification(self):
        """Test error notification method."""
        result = self.database_base._error_notification("Error Title", "Error Message")
        
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(result['params']['title'], "Error Title")
        self.assertEqual(result['params']['message'], "Error Message")
        self.assertEqual(result['params']['type'], 'danger')
        self.assertTrue(result['params']['sticky'])

    def test_filestore_path_validation_valid(self):
        """Test filestore path validation with valid path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Should not raise an error
            self.database_base.filestore_path = temp_dir
            self.database_base._constrain_filestore_path()

    def test_filestore_path_validation_invalid(self):
        """Test filestore path validation with invalid path."""
        with self.assertRaises(UserError):
            self.database_base.filestore_path = "/nonexistent/path/that/does/not/exist"
            self.database_base._constrain_filestore_path()

    def test_filestore_path_validation_empty(self):
        """Test filestore path validation with empty path (should be allowed)."""
        # Empty path should not raise an error
        self.database_base.filestore_path = ""
        self.database_base._constrain_filestore_path()
        
        # None path should not raise an error
        self.database_base.filestore_path = False
        self.database_base._constrain_filestore_path()

    def test_get_cursor_method_exists(self):
        """Test that get_cursor method exists and is callable."""
        self.assertTrue(hasattr(self.database_base, 'get_cursor'))
        self.assertTrue(callable(getattr(self.database_base, 'get_cursor')))

    def test_database_connection_with_invalid_credentials(self):
        """Test database connection with invalid credentials."""
        # Create a database base with invalid credentials
        invalid_db = self.env["odoo16.database.base"].create({
            "database_host": "nonexistent_host",
            "database_name": "nonexistent_db",
            "database_username": "invalid_user",
            "database_password": "invalid_password",
            "database_port": 9999,
        })
        
        # Should raise UserError when trying to connect
        with self.assertRaises(UserError):
            with invalid_db.get_cursor() as cr:
                cr.execute("SELECT 1")

    def test_migration_log_accumulation(self):
        """Test that migration log accumulates multiple entries."""
        self.database_base._update_migration_status('in_progress', 'First message')
        self.database_base._update_migration_status('in_progress', 'Second message')
        self.database_base._update_migration_status('completed', 'Third message')
        
        log = self.database_base.migration_log
        self.assertIn('First message', log)
        self.assertIn('Second message', log)
        self.assertIn('Third message', log)
        
        # Check that messages appear in chronological order
        first_pos = log.find('First message')
        second_pos = log.find('Second message')
        third_pos = log.find('Third message')
        
        self.assertLess(first_pos, second_pos)
        self.assertLess(second_pos, third_pos)
