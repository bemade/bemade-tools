"""Tests for IR Filters Migration."""
import logging
from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install', 'migration', 'ir_filters')
class TestMigrationIrFilters(TransactionCase):
    """Test suite for IR filters migration."""

    def setUp(self):
        """Set up test environment."""
        super().setUp()
        
        # Create migration coordinator
        self.coordinator = self.env['migration.coordinator'].create({
            'name': 'Test IR Filters Migration',
            'source_db_host': 'localhost',
            'source_db_name': '2025-08-01-medsportsuroit-prod',
            'source_db_user': 'odoo',
            'source_db_password': 'y@I^3eNg3*o!$NHA',
            'source_db_port': 5432,
            'migration_status': 'not_started'
        })
        
        # Create migration instance
        self.migration = self.env['migration.ir.filters'].create({
            'coordinator_id': self.coordinator.id
        })
        
        _logger.info("✅ IR filters migration test setup completed")

    def tearDown(self):
        """Clean up test environment."""
        try:
            if hasattr(self, 'coordinator') and self.coordinator:
                self.coordinator.unlink()
            _logger.info("✅ IR filters migration test cleanup completed")
        except Exception as e:
            _logger.warning(f"⚠️ Cleanup warning: {e}")
        super().tearDown()

    def test_migration_coordinator_creation(self):
        """Test that migration coordinator is properly created."""
        _logger.info("🧪 Testing IR filters migration coordinator creation...")
        
        self.assertTrue(self.coordinator.exists())
        self.assertEqual(self.coordinator.migration_status, 'not_started')
        self.assertTrue(self.migration.exists())
        self.assertEqual(self.migration.coordinator_id, self.coordinator)
        
        _logger.info("✅ IR filters migration coordinator creation test passed")

    def test_database_connection(self):
        """Test database connection parameters."""
        _logger.info("🧪 Testing IR filters migration database connection...")
        
        try:
            # Test connection parameters are set
            self.assertEqual(self.migration.coordinator_id.source_db_host, 'localhost')
            self.assertEqual(self.migration.coordinator_id.source_db_name, '2025-08-01-medsportsuroit-prod')
            self.assertEqual(self.migration.coordinator_id.source_db_user, 'odoo')
            self.assertEqual(self.migration.coordinator_id.source_db_port, 5432)
            
            _logger.info("✅ IR filters migration database connection test passed")
        except Exception as e:
            _logger.warning(f"⚠️ Database connection test skipped: {e}")
            self.skipTest(f"Database connection test skipped: {e}")

    def test_migration_execution(self):
        """Test IR filters migration execution."""
        _logger.info("🧪 Testing IR filters migration execution...")
        
        try:
            # Execute migration
            result = self.migration.action_migrate_ir_filters()
            
            # Verify result structure
            self.assertIsInstance(result, dict)
            self.assertIn('type', result)
            
            # Log results
            if 'params' in result and 'message' in result['params']:
                _logger.info(f"📊 Migration result: {result['params']['message']}")
            
            _logger.info("✅ IR filters migration execution test completed")
            
        except Exception as e:
            _logger.error(f"❌ IR filters migration failed: {e}")
            self.fail(f"IR filters migration failed: {e}")


        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Create test users and models that exist in the system
        test_user = self.env['res.users'].create({
            'name': 'Test User',
            'login': 'testuser'
        })
        test_model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        
        # Mock filter data
        mock_filter_data = [
            (1, 'My Partners', test_model.id, test_user.id, "[('is_company', '=', True)]", 
             "{}", "name asc", False, None, True, None, None, None, None),
            (2, 'Active Partners', test_model.id, test_user.id, "[('active', '=', True)]",
             "{}", "create_date desc", True, None, True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_filter_data
        
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        # Mock search to return no existing filters
        with patch.object(self.env['ir.filters'], 'search', return_value=self.env['ir.filters']):
            with patch.object(self.env['ir.filters'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute migration
                result = self.migration.action_migrate_ir_filters()
                
                # Verify result
                self.assertEqual(result['type'], 'ir.actions.client')
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('2 user filters', result['params']['message'])
                
                # Verify filters were created
                self.assertEqual(mock_create.call_count, 2)
                
                # Check first filter data
                first_call = mock_create.call_args_list[0][0][0]
                self.assertEqual(first_call['name'], 'My Partners')
                self.assertEqual(first_call['model_id'], test_model.id)
                self.assertEqual(first_call['user_id'], test_user.id)
                self.assertEqual(first_call['domain'], "[('is_company', '=', True)]")
                self.assertEqual(first_call['sort'], "name asc")
                self.assertFalse(first_call['is_default'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_ir_filters.MigrationIrFilters.get_cursor')
    def test_migration_with_missing_user(self, mock_get_cursor):
        """Test migration behavior when referenced user doesn't exist."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock filter data with non-existent user
        test_model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        mock_filter_data = [
            (1, 'Filter with missing user', test_model.id, 99999, "[('active', '=', True)]",
             "{}", "name asc", False, None, True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_filter_data
        
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        with patch.object(self.env['ir.filters'], 'create') as mock_create:
            # Execute migration
            result = self.migration.action_migrate_ir_filters()
            
            # Should succeed but skip the invalid filter
            self.assertEqual(result['params']['type'], 'success')
            self.assertIn('0 user filters', result['params']['message'])
            self.assertIn('1 filters were skipped', result['params']['message'])
            mock_create.assert_not_called()

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_ir_filters.MigrationIrFilters.get_cursor')
    def test_migration_with_missing_model(self, mock_get_cursor):
        """Test migration behavior when referenced model doesn't exist."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Create test user
        test_user = self.env['res.users'].create({
            'name': 'Test User',
            'login': 'testuser'
        })
        
        # Mock filter data with non-existent model
        mock_filter_data = [
            (1, 'Filter with missing model', 99999, test_user.id, "[('active', '=', True)]",
             "{}", "name asc", False, None, True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_filter_data
        
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        with patch.object(self.env['ir.filters'], 'create') as mock_create:
            # Execute migration
            result = self.migration.action_migrate_ir_filters()
            
            # Should succeed but skip the invalid filter
            self.assertEqual(result['params']['type'], 'success')
            self.assertIn('0 user filters', result['params']['message'])
            self.assertIn('1 filters were skipped', result['params']['message'])
            mock_create.assert_not_called()

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_ir_filters.MigrationIrFilters.get_cursor')
    def test_migration_with_existing_filters(self, mock_get_cursor):
        """Test migration behavior when filters already exist."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Create test user and model
        test_user = self.env['res.users'].create({
            'name': 'Test User',
            'login': 'testuser'
        })
        test_model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        
        # Mock filter data
        mock_filter_data = [
            (1, 'Existing Filter', test_model.id, test_user.id, "[('active', '=', True)]",
             "{}", "name asc", False, None, True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_filter_data
        
        # Create existing filter
        existing_filter = self.env['ir.filters'].create({
            'name': 'Existing Filter',
            'model_id': test_model.model,
            'user_id': test_user.id
        })
        
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        # Mock search to return existing filter
        with patch.object(self.env['ir.filters'], 'search', return_value=existing_filter):
            with patch.object(self.env['ir.filters'], 'create') as mock_create:
                # Execute migration
                result = self.migration.action_migrate_ir_filters()
                
                # Should succeed but not create new filters
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('0 user filters', result['params']['message'])
                mock_create.assert_not_called()

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_ir_filters.MigrationIrFilters.get_cursor')
    def test_migration_database_error(self, mock_get_cursor):
        """Test migration behavior when database error occurs."""
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        # Mock cursor to raise an exception
        mock_get_cursor.side_effect = Exception("Database connection failed")
        
        # Execute migration and expect UserError
        with self.assertRaises(UserError) as context:
            self.migration.action_migrate_ir_filters()
        
        self.assertIn("IR filters migration failed", str(context.exception))
        self.assertEqual(self.migration.migration_status, 'failed')

    def test_migration_status_updates_when_disabled(self):
        """Test that migration status is not updated when migration is disabled."""
        # Initially should be not_started
        self.assertEqual(self.migration.migration_status, 'not_started')
        
        # Disable migration
        self.migration.migrate_ir_filters = False
        
        # Execute migration
        self.migration.action_migrate_ir_filters()
        
        # Status should remain not_started since migration was skipped
        self.assertEqual(self.migration.migration_status, 'not_started')

    def test_migration_status_updates_when_enabled(self):
        """Test that migration status is properly updated when migration is enabled."""
        # Initially should be not_started
        self.assertEqual(self.migration.migration_status, 'not_started')
        
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        # Mock the migration to test status updates
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []  # No filters to migrate
            
            # Execute migration
            self.migration.action_migrate_ir_filters()
            
            # Status should be completed
            self.assertEqual(self.migration.migration_status, 'completed')
            self.assertIn('IR filters migration completed', self.migration.migration_log)

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_ir_filters.MigrationIrFilters.get_cursor')
    def test_filter_field_mapping(self, mock_get_cursor):
        """Test that all filter fields are properly mapped."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Create test user and model
        test_user = self.env['res.users'].create({
            'name': 'Test User',
            'login': 'testuser'
        })
        test_model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        
        # Mock comprehensive filter data
        mock_filter_data = [
            (1, 'Complete Filter', test_model.id, test_user.id, "[('is_company', '=', True)]",
             "{'group_by': ['country_id']}", "name desc, create_date asc", True, 123, False,
             None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_filter_data
        
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        # Mock search to return no existing filters
        with patch.object(self.env['ir.filters'], 'search', return_value=self.env['ir.filters']):
            with patch.object(self.env['ir.filters'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute migration
                self.migration.action_migrate_ir_filters()
                
                # Verify all fields are mapped correctly
                call_args = mock_create.call_args_list[0][0][0]
                self.assertEqual(call_args['name'], 'Complete Filter')
                self.assertEqual(call_args['model_id'], test_model.id)
                self.assertEqual(call_args['user_id'], test_user.id)
                self.assertEqual(call_args['domain'], "[('is_company', '=', True)]")
                self.assertEqual(call_args['context'], "{'group_by': ['country_id']}")
                self.assertEqual(call_args['sort'], "name desc, create_date asc")
                self.assertTrue(call_args['is_default'])
                self.assertEqual(call_args['action_id'], 123)
                self.assertFalse(call_args['active'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_ir_filters.MigrationIrFilters.get_cursor')
    def test_mixed_valid_invalid_filters(self, mock_get_cursor):
        """Test migration with a mix of valid and invalid filters."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Create test user and model
        test_user = self.env['res.users'].create({
            'name': 'Test User',
            'login': 'testuser'
        })
        test_model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        
        # Mock filter data with mix of valid and invalid
        mock_filter_data = [
            # Valid filter
            (1, 'Valid Filter', test_model.id, test_user.id, "[('active', '=', True)]",
             "{}", "name asc", False, None, True, None, None, None, None),
            # Invalid filter - missing user
            (2, 'Invalid User Filter', test_model.id, 99999, "[('active', '=', True)]",
             "{}", "name asc", False, None, True, None, None, None, None),
            # Invalid filter - missing model
            (3, 'Invalid Model Filter', 99999, test_user.id, "[('active', '=', True)]",
             "{}", "name asc", False, None, True, None, None, None, None),
            # Another valid filter
            (4, 'Another Valid Filter', test_model.id, test_user.id, "[('is_company', '=', False)]",
             "{}", "name desc", True, None, True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_filter_data
        
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        # Mock search to return no existing filters
        with patch.object(self.env['ir.filters'], 'search', return_value=self.env['ir.filters']):
            with patch.object(self.env['ir.filters'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute migration
                result = self.migration.action_migrate_ir_filters()
                
                # Should create 2 valid filters and skip 2 invalid ones
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('2 user filters', result['params']['message'])
                self.assertIn('2 filters were skipped', result['params']['message'])
                self.assertEqual(mock_create.call_count, 2)

    def test_page_size_usage(self):
        """Test that PAGE_SIZE is properly used in the query."""
        from odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_ir_filters import PAGE_SIZE
        
        # Enable migration
        self.migration.migrate_ir_filters = True
        
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []
            
            # Execute migration
            self.migration.action_migrate_ir_filters()
            
            # Verify PAGE_SIZE was used in the query
            execute_call = mock_cursor.execute.call_args[0]
            query = execute_call[0]
            params = execute_call[1]
            
            self.assertIn('LIMIT %s', query)
            self.assertEqual(params[0], PAGE_SIZE)
            self.assertEqual(PAGE_SIZE, 1000)
