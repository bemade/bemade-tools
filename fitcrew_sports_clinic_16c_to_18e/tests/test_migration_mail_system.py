from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError
import os
import logging
from unittest.mock import patch, MagicMock

_logger = logging.getLogger(__name__)


class TestMigrationMailSystem(TransactionCase):
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
        
        # Create mail system migration record
        self.migration = self.env["migration.mail.system"].create({
            "database_id": self.database_base.id
        })

    def test_migration_creation(self):
        """Test that mail system migration record can be created properly."""
        self.assertTrue(self.migration.id)
        self.assertEqual(self.migration.database_id, self.database_base)
        
        # Test inherited fields from base
        self.assertEqual(self.migration.database_host, self.database_base.database_host)
        self.assertEqual(self.migration.migration_status, 'not_started')

    def test_migration_method_exists(self):
        """Test that main migration method exists and is callable."""
        self.assertTrue(hasattr(self.migration, 'action_migrate_mail_system'))
        self.assertTrue(callable(getattr(self.migration, 'action_migrate_mail_system')))

    def test_private_migration_methods_exist(self):
        """Test that all private migration methods exist."""
        methods = [
            '_migrate_mail_channels',
            '_migrate_mail_channel_members',
            '_migrate_mail_notifications',
            '_migrate_mail_reactions',
            '_migrate_mail_tracking',
            '_migrate_mail_followers'
        ]
        
        for method in methods:
            self.assertTrue(hasattr(self.migration, method))
            self.assertTrue(callable(getattr(self.migration, method)))

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_mail_system.MigrationMailSystem.get_cursor')
    def test_migrate_mail_channels(self, mock_get_cursor):
        """Test mail channels migration with mock data."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock channel data
        mock_channel_data = [
            (1, 'General Channel', 'General discussion', 'channel', 'public', None, 'uuid-1', True, None, None, None, None),
            (2, 'Private Channel', 'Private discussion', 'channel', 'private', 1, 'uuid-2', True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_channel_data
        
        # Mock search to return no existing channels
        with patch.object(self.env['mail.channel'], 'search', return_value=self.env['mail.channel']):
            with patch.object(self.env['mail.channel'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute channel migration
                count = self.migration._migrate_mail_channels()
                
                # Verify results
                self.assertEqual(count, 2)
                self.assertEqual(mock_create.call_count, 2)

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
