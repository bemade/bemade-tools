from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError
import os
import logging
from unittest.mock import patch, MagicMock

_logger = logging.getLogger(__name__)


class TestOdoo16DatabaseCoordinator(TransactionCase):
    def setUp(self):
        super().setUp()
        # Create coordinator record (this will auto-create all components)
        self.coordinator = self.env["odoo16.database"].create({
            "name": "Test Migration",
            "database_host": os.environ.get("ODOO16_HOST", "localhost"),
            "database_name": os.environ.get("ODOO16_DBNAME", "test_db"),
            "database_username": os.environ.get("ODOO16_USER", "odoo"),
            "database_password": os.environ.get("ODOO16_PASSWORD", ""),
            "database_port": int(os.environ.get("ODOO16_PORT", "5432")),
        })

    def test_coordinator_creation(self):
        """Test that coordinator record can be created with all components."""
        self.assertTrue(self.coordinator.id)
        self.assertEqual(self.coordinator.name, "Test Migration")
        
        # Test that database_base was created
        self.assertTrue(self.coordinator.database_id)
        self.assertEqual(self.coordinator.database_host, os.environ.get("ODOO16_HOST", "localhost"))
        
        # Test that all migration components were created
        self.assertTrue(self.coordinator.users_partners_migration_id)
        self.assertTrue(self.coordinator.mail_system_migration_id)
        self.assertTrue(self.coordinator.calendar_events_migration_id)
        self.assertTrue(self.coordinator.attachments_migration_id)
        self.assertTrue(self.coordinator.ir_filters_migration_id)

    def test_configuration_field_delegation(self):
        """Test that configuration fields are properly delegated to components."""
        # Test skip_filestore delegation
        self.coordinator.skip_filestore = False
        self.assertFalse(self.coordinator.attachments_migration_id.skip_filestore)
        
        # Test migrate_ir_filters delegation
        self.coordinator.migrate_ir_filters = True
        self.assertTrue(self.coordinator.ir_filters_migration_id.migrate_ir_filters)

    def test_migration_delegation_methods_exist(self):
        """Test that all migration delegation methods exist and are callable."""
        delegation_methods = [
            'action_migrate_users_partners',
            'action_migrate_mail_system',
            'action_migrate_calendar_events_to_tasks',
            'action_migrate_attachments',
            'action_migrate_ir_filters'
        ]
        
        for method in delegation_methods:
            self.assertTrue(hasattr(self.coordinator, method))
            self.assertTrue(callable(getattr(self.coordinator, method)))

    def test_placeholder_methods_exist(self):
        """Test that placeholder methods exist for sports clinic migrations."""
        placeholder_methods = [
            'action_migrate_teams',
            'action_migrate_patients',
            'action_migrate_injuries',
            'action_migrate_activities'
        ]
        
        for method in placeholder_methods:
            self.assertTrue(hasattr(self.coordinator, method))
            self.assertTrue(callable(getattr(self.coordinator, method)))

    def test_utility_methods_exist(self):
        """Test that utility methods exist and are callable."""
        utility_methods = [
            'action_migrate_all',
            'action_validate_source_data',
            'action_test_connection'
        ]
        
        for method in utility_methods:
            self.assertTrue(hasattr(self.coordinator, method))
            self.assertTrue(callable(getattr(self.coordinator, method)))

    def test_users_partners_migration_delegation(self):
        """Test delegation to users & partners migration."""
        with patch.object(self.coordinator.users_partners_migration_id, 'action_migrate_users_partners') as mock_method:
            mock_method.return_value = {'type': 'ir.actions.client', 'params': {'type': 'success'}}
            
            result = self.coordinator.action_migrate_users_partners()
            
            mock_method.assert_called_once()
            self.assertEqual(result['params']['type'], 'success')

    def test_mail_system_migration_delegation(self):
        """Test delegation to mail system migration."""
        with patch.object(self.coordinator.mail_system_migration_id, 'action_migrate_mail_system') as mock_method:
            mock_method.return_value = {'type': 'ir.actions.client', 'params': {'type': 'success'}}
            
            result = self.coordinator.action_migrate_mail_system()
            
            mock_method.assert_called_once()
            self.assertEqual(result['params']['type'], 'success')

    def test_calendar_events_migration_delegation(self):
        """Test delegation to calendar events migration."""
        with patch.object(self.coordinator.calendar_events_migration_id, 'action_migrate_calendar_events_to_tasks') as mock_method:
            mock_method.return_value = {'type': 'ir.actions.client', 'params': {'type': 'success'}}
            
            result = self.coordinator.action_migrate_calendar_events_to_tasks()
            
            mock_method.assert_called_once()
            self.assertEqual(result['params']['type'], 'success')

    def test_attachments_migration_delegation(self):
        """Test delegation to attachments migration."""
        with patch.object(self.coordinator.attachments_migration_id, 'action_migrate_attachments') as mock_method:
            mock_method.return_value = {'type': 'ir.actions.client', 'params': {'type': 'success'}}
            
            result = self.coordinator.action_migrate_attachments()
            
            mock_method.assert_called_once()
            self.assertEqual(result['params']['type'], 'success')

    def test_ir_filters_migration_delegation(self):
        """Test delegation to IR filters migration."""
        with patch.object(self.coordinator.ir_filters_migration_id, 'action_migrate_ir_filters') as mock_method:
            mock_method.return_value = {'type': 'ir.actions.client', 'params': {'type': 'success'}}
            
            result = self.coordinator.action_migrate_ir_filters()
            
            mock_method.assert_called_once()
            self.assertEqual(result['params']['type'], 'success')

    def test_placeholder_methods_return_success(self):
        """Test that placeholder methods return success notifications."""
        placeholder_methods = [
            'action_migrate_teams',
            'action_migrate_patients',
            'action_migrate_injuries',
            'action_migrate_activities'
        ]
        
        for method_name in placeholder_methods:
            method = getattr(self.coordinator, method_name)
            result = method()
            
            self.assertEqual(result['type'], 'ir.actions.client')
            self.assertEqual(result['params']['type'], 'success')
            self.assertIn('not yet implemented', result['params']['message'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.odoo16_database_coordinator.Odoo16Database.get_cursor')
    def test_validate_source_data_success(self, mock_get_cursor):
        """Test successful source data validation."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock successful validation responses
        def mock_execute(query, params=None):
            if 'ir_module_module' in query:
                mock_cursor.fetchone.return_value = ('bemade_sports_clinic', 'installed')
            elif 'information_schema.tables' in query:
                mock_cursor.fetchone.return_value = (1,)  # Table exists
        
        mock_cursor.execute.side_effect = mock_execute
        
        result = self.coordinator.action_validate_source_data()
        
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'success')
        self.assertIn('validated successfully', result['params']['message'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.odoo16_database_coordinator.Odoo16Database.get_cursor')
    def test_validate_source_data_missing_module(self, mock_get_cursor):
        """Test source data validation with missing module."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # Module not found
        
        with self.assertRaises(UserError) as context:
            self.coordinator.action_validate_source_data()
        
        self.assertIn('bemade_sports_clinic module was not found', str(context.exception))

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.odoo16_database_coordinator.Odoo16Database.get_cursor')
    def test_validate_source_data_module_not_installed(self, mock_get_cursor):
        """Test source data validation with uninstalled module."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = ('bemade_sports_clinic', 'uninstalled')
        
        with self.assertRaises(UserError) as context:
            self.coordinator.action_validate_source_data()
        
        self.assertIn('bemade_sports_clinic module is not installed', str(context.exception))

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.odoo16_database_coordinator.Odoo16Database.get_cursor')
    def test_validate_source_data_missing_table(self, mock_get_cursor):
        """Test source data validation with missing required table."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        def mock_execute(query, params=None):
            if 'ir_module_module' in query:
                mock_cursor.fetchone.return_value = ('bemade_sports_clinic', 'installed')
            elif 'information_schema.tables' in query:
                mock_cursor.fetchone.return_value = None  # Table not found
        
        mock_cursor.execute.side_effect = mock_execute
        
        with self.assertRaises(UserError) as context:
            self.coordinator.action_validate_source_data()
        
        self.assertIn('Required table', str(context.exception))
        self.assertIn('not found', str(context.exception))

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.odoo16_database_coordinator.Odoo16Database.get_cursor')
    def test_test_connection_success(self, mock_get_cursor):
        """Test successful database connection test."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock database responses
        mock_cursor.fetchone.side_effect = [
            ('PostgreSQL 13.0',),  # version()
            (42,)  # COUNT(*) from calendar_event
        ]
        
        result = self.coordinator.action_test_connection()
        
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'success')
        self.assertIn('Connected to database successfully', result['params']['message'])
        self.assertIn('PostgreSQL 13.0', result['params']['message'])
        self.assertIn('Calendar Events Found: 42', result['params']['message'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.odoo16_database_coordinator.Odoo16Database.get_cursor')
    def test_test_connection_failure(self, mock_get_cursor):
        """Test database connection test failure."""
        mock_get_cursor.side_effect = Exception("Connection failed")
        
        result = self.coordinator.action_test_connection()
        
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('Failed to connect', result['params']['message'])
        self.assertIn('Connection failed', result['params']['message'])

    def test_complete_migration_orchestration(self):
        """Test that complete migration calls all individual migrations in sequence."""
        # Mock all migration methods
        with patch.object(self.coordinator, 'action_migrate_users_partners') as mock_users:
            with patch.object(self.coordinator, 'action_migrate_teams') as mock_teams:
                with patch.object(self.coordinator, 'action_migrate_patients') as mock_patients:
                    with patch.object(self.coordinator, 'action_migrate_injuries') as mock_injuries:
                        with patch.object(self.coordinator, 'action_migrate_activities') as mock_activities:
                            with patch.object(self.coordinator, 'action_migrate_calendar_events_to_tasks') as mock_calendar:
                                with patch.object(self.coordinator, 'action_migrate_mail_system') as mock_mail:
                                    with patch.object(self.coordinator, 'action_migrate_attachments') as mock_attachments:
                                        
                                        # Execute complete migration
                                        result = self.coordinator.action_migrate_all()
                                        
                                        # Verify all methods were called in sequence
                                        mock_users.assert_called_once()
                                        mock_teams.assert_called_once()
                                        mock_patients.assert_called_once()
                                        mock_injuries.assert_called_once()
                                        mock_activities.assert_called_once()
                                        mock_calendar.assert_called_once()
                                        mock_mail.assert_called_once()
                                        mock_attachments.assert_called_once()
                                        
                                        # Verify result
                                        self.assertEqual(result['params']['type'], 'success')
                                        self.assertIn('All data has been successfully migrated', result['params']['message'])

    def test_complete_migration_failure(self):
        """Test complete migration behavior when a step fails."""
        # Mock first migration to fail
        with patch.object(self.coordinator, 'action_migrate_users_partners', side_effect=Exception("Migration failed")):
            with self.assertRaises(UserError) as context:
                self.coordinator.action_migrate_all()
            
            self.assertIn("Migration failed", str(context.exception))
            self.assertEqual(self.coordinator.migration_status, 'failed')

    def test_inherited_methods_available(self):
        """Test that methods from base class are available through coordinator."""
        # Test inherited methods
        self.assertTrue(hasattr(self.coordinator, '_update_migration_status'))
        self.assertTrue(hasattr(self.coordinator, '_success_notification'))
        self.assertTrue(hasattr(self.coordinator, '_error_notification'))
        self.assertTrue(hasattr(self.coordinator, 'get_cursor'))
        
        # Test that they work
        result = self.coordinator._success_notification("Test", "Message")
        self.assertEqual(result['params']['type'], 'success')
        
        self.coordinator._update_migration_status('in_progress', 'Test message')
        self.assertEqual(self.coordinator.migration_status, 'in_progress')

    def test_component_creation_on_coordinator_creation(self):
        """Test that all migration components are created when coordinator is created."""
        # Create another coordinator to test component creation
        new_coordinator = self.env["odoo16.database"].create({
            "name": "Another Test Migration",
            "database_host": "localhost",
            "database_name": "another_test_db",
            "database_username": "odoo",
            "database_password": "",
            "database_port": 5432,
        })
        
        # Verify all components were created
        self.assertTrue(new_coordinator.database_id)
        self.assertTrue(new_coordinator.users_partners_migration_id)
        self.assertTrue(new_coordinator.mail_system_migration_id)
        self.assertTrue(new_coordinator.calendar_events_migration_id)
        self.assertTrue(new_coordinator.attachments_migration_id)
        self.assertTrue(new_coordinator.ir_filters_migration_id)
        
        # Verify components reference the same database base
        self.assertEqual(new_coordinator.users_partners_migration_id.database_id, new_coordinator.database_id)
        self.assertEqual(new_coordinator.mail_system_migration_id.database_id, new_coordinator.database_id)
        self.assertEqual(new_coordinator.calendar_events_migration_id.database_id, new_coordinator.database_id)
        self.assertEqual(new_coordinator.attachments_migration_id.database_id, new_coordinator.database_id)
        self.assertEqual(new_coordinator.ir_filters_migration_id.database_id, new_coordinator.database_id)

    def test_migration_status_tracking(self):
        """Test that migration status is properly tracked through the coordinator."""
        # Initially should be not_started
        self.assertEqual(self.coordinator.migration_status, 'not_started')
        
        # Update status through coordinator
        self.coordinator._update_migration_status('in_progress', 'Starting migration')
        self.assertEqual(self.coordinator.migration_status, 'in_progress')
        self.assertIn('Starting migration', self.coordinator.migration_log)
        
        # Status should be reflected in database base
        self.assertEqual(self.coordinator.database_id.migration_status, 'in_progress')
        self.assertIn('Starting migration', self.coordinator.database_id.migration_log)
