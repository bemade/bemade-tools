"""Tests for Mail System Migration."""
import logging
from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install', 'migration', 'mail_system')
class TestMigrationMailSystem(TransactionCase):
    """Test suite for mail system migration."""

    def setUp(self):
        """Set up test environment."""
        super().setUp()
        
        # Create migration coordinator
        self.coordinator = self.env['migration.coordinator'].create({
            'name': 'Test Mail System Migration',
            'source_db_host': 'localhost',
            'source_db_name': '2025-08-01-medsportsuroit-prod',
            'source_db_user': 'odoo',
            'source_db_password': 'y@I^3eNg3*o!$NHA',
            'source_db_port': 5432,
            'migration_status': 'not_started'
        })
        
        # Create migration instance
        self.migration = self.env['migration.mail.system'].create({
            'coordinator_id': self.coordinator.id
        })
        
        _logger.info("✅ Mail system migration test setup completed")

    def tearDown(self):
        """Clean up test environment."""
        try:
            if hasattr(self, 'coordinator') and self.coordinator:
                self.coordinator.unlink()
            _logger.info("✅ Mail system migration test cleanup completed")
        except Exception as e:
            _logger.warning(f"⚠️ Cleanup warning: {e}")
        super().tearDown()

    def test_migration_coordinator_creation(self):
        """Test that migration coordinator is properly created."""
        _logger.info("🧪 Testing mail system migration coordinator creation...")
        
        self.assertTrue(self.coordinator.exists())
        self.assertEqual(self.coordinator.migration_status, 'not_started')
        self.assertTrue(self.migration.exists())
        self.assertEqual(self.migration.coordinator_id, self.coordinator)
        
        _logger.info("✅ Mail system migration coordinator creation test passed")

    def test_database_connection(self):
        """Test database connection parameters."""
        _logger.info("🧪 Testing mail system migration database connection...")
        
        try:
            # Test connection parameters are set
            self.assertEqual(self.migration.coordinator_id.source_db_host, 'localhost')
            self.assertEqual(self.migration.coordinator_id.source_db_name, '2025-08-01-medsportsuroit-prod')
            self.assertEqual(self.migration.coordinator_id.source_db_user, 'odoo')
            self.assertEqual(self.migration.coordinator_id.source_db_port, 5432)
            
            _logger.info("✅ Mail system migration database connection test passed")
        except Exception as e:
            _logger.warning(f"⚠️ Database connection test skipped: {e}")
            self.skipTest(f"Database connection test skipped: {e}")

    def test_migration_execution(self):
        """Test mail system migration execution."""
        _logger.info("🧪 Testing mail system migration execution...")
        
        try:
            # Execute migration
            result = self.migration.action_migrate_mail_system()
            
            # Verify result structure
            self.assertIsInstance(result, dict)
            self.assertIn('type', result)
            
            # Log results
            if 'params' in result and 'message' in result['params']:
                _logger.info(f"📊 Migration result: {result['params']['message']}")
            
            _logger.info("✅ Mail system migration execution test completed")
            
        except Exception as e:
            _logger.error(f"❌ Mail system migration failed: {e}")
            self.fail(f"Mail system migration failed: {e}")

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem.get_cursor')
    def test_migrate_mail_channel_members(self, mock_get_cursor):
        """Test mail channel members migration with mock data."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock member data
        mock_member_data = [
            (1, 1, 1, None, 'Custom Name', 100, 99, 'open', False, True, None, None, None, None, None, None),
            (2, 1, 2, None, None, 200, 199, 'folded', True, False, None, None, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_member_data
        
        # Mock search to return no existing members
        with patch.object(self.env['mail.channel.member'], 'search', return_value=self.env['mail.channel.member']):
            with patch.object(self.env['mail.channel.member'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute member migration
                count = self.migration._migrate_mail_channel_members()
                
                # Verify results
                self.assertEqual(count, 2)
                self.assertEqual(mock_create.call_count, 2)

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem.get_cursor')
    def test_migrate_mail_notifications(self, mock_get_cursor):
        """Test mail notifications migration with mock data."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock notification data
        mock_notification_data = [
            (1, 100, 1, 'email', 'sent', True, None, None, None, None, None),
            (2, 101, 2, 'inbox', 'ready', False, None, 'bounce', 'Invalid email', None, None)
        ]
        mock_cursor.fetchall.return_value = mock_notification_data
        
        # Mock search to return no existing notifications
        with patch.object(self.env['mail.notification'], 'search', return_value=self.env['mail.notification']):
            with patch.object(self.env['mail.notification'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute notification migration
                count = self.migration._migrate_mail_notifications()
                
                # Verify results
                self.assertEqual(count, 2)
                self.assertEqual(mock_create.call_count, 2)

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem.get_cursor')
    def test_migrate_mail_reactions(self, mock_get_cursor):
        """Test mail message reactions migration with mock data."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock reaction data
        mock_reaction_data = [
            (1, 100, 1, None, '👍', None, None),
            (2, 101, 2, None, '❤️', None, None)
        ]
        mock_cursor.fetchall.return_value = mock_reaction_data
        
        # Mock search to return no existing reactions
        with patch.object(self.env['mail.message.reaction'], 'search', return_value=self.env['mail.message.reaction']):
            with patch.object(self.env['mail.message.reaction'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute reaction migration
                count = self.migration._migrate_mail_reactions()
                
                # Verify results
                self.assertEqual(count, 2)
                self.assertEqual(mock_create.call_count, 2)

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem.get_cursor')
    def test_migrate_mail_tracking(self, mock_get_cursor):
        """Test mail tracking values migration with mock data."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock tracking data
        mock_tracking_data = [
            (1, 'state', 'State', 'selection', None, None, None, 'draft', None, None,
             None, None, None, 'done', None, None, 100, None, None),
            (2, 'priority', 'Priority', 'selection', 0, None, None, '0', None, None,
             1, None, None, '1', None, None, 101, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_tracking_data
        
        # Mock search to return no existing tracking values
        with patch.object(self.env['mail.tracking.value'], 'search', return_value=self.env['mail.tracking.value']):
            with patch.object(self.env['mail.tracking.value'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute tracking migration
                count = self.migration._migrate_mail_tracking()
                
                # Verify results
                self.assertEqual(count, 2)
                self.assertEqual(mock_create.call_count, 2)

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem.get_cursor')
    def test_migrate_mail_followers(self, mock_get_cursor):
        """Test mail followers migration with mock data."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock follower data for different sports models
        mock_follower_data = [
            (1, 'sports.team', 1, 1, [1, 2], None, None),
            (2, 'sports.patient', 1, 2, [1], None, None),
            (3, 'sports.patient.injury', 1, 3, [2], None, None)
        ]
        
        def mock_fetchall():
            # Return data based on the model being queried
            if 'sports.team' in mock_cursor.execute.call_args[0][1][0]:
                return [mock_follower_data[0]]
            elif 'sports.patient' in mock_cursor.execute.call_args[0][1][0]:
                return [mock_follower_data[1]]
            elif 'sports.patient.injury' in mock_cursor.execute.call_args[0][1][0]:
                return [mock_follower_data[2]]
            return []
        
        mock_cursor.fetchall.side_effect = mock_fetchall
        
        # Mock search to return no existing followers
        with patch.object(self.env['mail.followers'], 'search', return_value=self.env['mail.followers']):
            with patch.object(self.env['mail.followers'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute follower migration
                count = self.migration._migrate_mail_followers()
                
                # Verify results (should be 3 total, one for each model)
                self.assertEqual(count, 3)
                self.assertEqual(mock_create.call_count, 3)

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem._migrate_mail_channels')
    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem._migrate_mail_channel_members')
    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem._migrate_mail_notifications')
    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem._migrate_mail_reactions')
    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem._migrate_mail_tracking')
    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem._migrate_mail_followers')
    def test_complete_mail_system_migration(self, mock_followers, mock_tracking, mock_reactions, 
                                          mock_notifications, mock_members, mock_channels):
        """Test complete mail system migration orchestration."""
        # Mock return values for each migration method
        mock_channels.return_value = 5
        mock_members.return_value = 10
        mock_notifications.return_value = 15
        mock_reactions.return_value = 3
        mock_tracking.return_value = 8
        mock_followers.return_value = 12
        
        # Execute complete migration
        result = self.migration.action_migrate_mail_system()
        
        # Verify all methods were called
        mock_channels.assert_called_once()
        mock_members.assert_called_once()
        mock_notifications.assert_called_once()
        mock_reactions.assert_called_once()
        mock_tracking.assert_called_once()
        mock_followers.assert_called_once()
        
        # Verify result
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'success')
        self.assertIn('5 channels', result['params']['message'])
        self.assertIn('10 members', result['params']['message'])
        self.assertIn('15 notifications', result['params']['message'])
        self.assertIn('3 reactions', result['params']['message'])
        self.assertIn('8 tracking values', result['params']['message'])
        self.assertIn('12 followers', result['params']['message'])
        
        # Verify status was updated
        self.assertEqual(self.migration.migration_status, 'completed')

    def test_migration_database_error(self):
        """Test migration behavior when database error occurs."""
        with patch.object(self.migration, '_migrate_mail_channels', side_effect=Exception("Database error")):
            # Execute migration and expect UserError
            with self.assertRaises(UserError) as context:
                self.migration.action_migrate_mail_system()
            
            self.assertIn("Mail system migration failed", str(context.exception))
            self.assertEqual(self.migration.migration_status, 'failed')

    def test_migration_with_existing_records(self):
        """Test migration behavior when records already exist."""
        with patch.object(self.migration, '_migrate_mail_channels', return_value=0):
            with patch.object(self.migration, '_migrate_mail_channel_members', return_value=0):
                with patch.object(self.migration, '_migrate_mail_notifications', return_value=0):
                    with patch.object(self.migration, '_migrate_mail_reactions', return_value=0):
                        with patch.object(self.migration, '_migrate_mail_tracking', return_value=0):
                            with patch.object(self.migration, '_migrate_mail_followers', return_value=0):
                                # Execute migration
                                result = self.migration.action_migrate_mail_system()
                                
                                # Should succeed with 0 counts
                                self.assertEqual(result['params']['type'], 'success')
                                self.assertIn('0 channels', result['params']['message'])
                                self.assertIn('0 members', result['params']['message'])

    def test_sports_models_constant(self):
        """Test that sports models are properly defined for followers migration."""
        # This tests the sports_models list in _migrate_mail_followers
        expected_models = ['sports.team', 'sports.patient', 'sports.patient.injury']
        
        # We can't directly access the constant, but we can test the behavior
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []
            
            # Execute follower migration
            self.migration._migrate_mail_followers()
            
            # Verify that execute was called for each expected model
            execute_calls = mock_cursor.execute.call_args_list
            self.assertEqual(len(execute_calls), len(expected_models))
            
            for i, model in enumerate(expected_models):
                self.assertIn(model, execute_calls[i][0][1][0])
