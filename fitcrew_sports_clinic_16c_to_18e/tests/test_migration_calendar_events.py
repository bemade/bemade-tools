from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError
import os
import logging
from unittest.mock import patch, MagicMock
from datetime import datetime

_logger = logging.getLogger(__name__)


class TestMigrationCalendarEvents(TransactionCase):
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
        
        # Create calendar events migration record
        self.migration = self.env["migration.calendar.events"].create({
            "database_id": self.database_base.id
        })

    def test_migration_creation(self):
        """Test that calendar events migration record can be created properly."""
        self.assertTrue(self.migration.id)
        self.assertEqual(self.migration.database_id, self.database_base)
        
        # Test inherited fields from base
        self.assertEqual(self.migration.database_host, self.database_base.database_host)
        self.assertEqual(self.migration.migration_status, 'not_started')

    def test_migration_method_exists(self):
        """Test that migration method exists and is callable."""
        self.assertTrue(hasattr(self.migration, 'action_migrate_calendar_events_to_tasks'))
        self.assertTrue(callable(getattr(self.migration, 'action_migrate_calendar_events_to_tasks')))

    def test_helper_methods_exist(self):
        """Test that helper methods exist and are callable."""
        helper_methods = [
            '_get_or_create_migration_project',
            '_get_or_create_migration_tag',
            '_get_partner_name'
        ]
        
        for method in helper_methods:
            self.assertTrue(hasattr(self.migration, method))
            self.assertTrue(callable(getattr(self.migration, method)))

    def test_get_or_create_migration_project(self):
        """Test migration project creation and retrieval."""
        # First call should create the project
        project1 = self.migration._get_or_create_migration_project()
        self.assertTrue(project1.id)
        self.assertEqual(project1.name, 'Migrated Calendar Events')
        self.assertEqual(project1.privacy_visibility, 'employees')
        
        # Second call should return the same project
        project2 = self.migration._get_or_create_migration_project()
        self.assertEqual(project1.id, project2.id)

    def test_get_or_create_migration_tag(self):
        """Test migration tag creation and retrieval."""
        # First call should create the tag
        tag1 = self.migration._get_or_create_migration_tag()
        self.assertTrue(tag1.id)
        self.assertEqual(tag1.name, 'Migrated from Calendar')
        self.assertEqual(tag1.color, 5)
        
        # Second call should return the same tag
        tag2 = self.migration._get_or_create_migration_tag()
        self.assertEqual(tag1.id, tag2.id)

    def test_get_partner_name(self):
        """Test partner name retrieval."""
        # Create a test partner
        partner = self.env['res.partner'].create({'name': 'Test Partner'})
        
        # Test with existing partner
        name = self.migration._get_partner_name(partner.id)
        self.assertEqual(name, 'Test Partner')
        
        # Test with non-existing partner
        name = self.migration._get_partner_name(99999)
        self.assertEqual(name, 'Partner ID 99999')

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_calendar_events.MigrationCalendarEvents.get_cursor')
    def test_migration_with_mock_data(self, mock_get_cursor):
        """Test calendar events migration with mock data."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock calendar event data
        start_time = datetime(2024, 1, 15, 10, 0, 0)
        end_time = datetime(2024, 1, 15, 11, 0, 0)
        
        mock_event_data = [
            (1, 'Team Meeting', 'Weekly team sync', start_time, end_time, False,
             'Conference Room A', 'public', 'busy', 1, None, None, None, None, None),
            (2, 'Training Session', 'Player training', start_time, end_time, True,
             'Training Field', 'private', 'free', 2, None, None, None, None, None)
        ]
        
        # Mock attendee data
        mock_attendee_data = [
            (1, 'accepted', 'attendee1@example.com'),
            (2, 'tentative', 'attendee2@example.com')
        ]
        
        # Configure mock cursor behavior
        def mock_execute(query, params=None):
            if 'calendar_event' in query and 'calendar_attendee' not in query:
                mock_cursor.fetchall.return_value = mock_event_data
            elif 'calendar_attendee' in query:
                mock_cursor.fetchall.return_value = mock_attendee_data
        
        mock_cursor.execute.side_effect = mock_execute
        
        # Mock project.task search and create
        with patch.object(self.env['project.task'], 'search', return_value=self.env['project.task']):
            with patch.object(self.env['project.task'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute migration
                result = self.migration.action_migrate_calendar_events_to_tasks()
                
                # Verify result
                self.assertEqual(result['type'], 'ir.actions.client')
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('2 calendar events', result['params']['message'])
                
                # Verify task creation was called
                self.assertEqual(mock_create.call_count, 2)
                
                # Verify task data structure
                call_args = mock_create.call_args_list
                first_task = call_args[0][0][0]
                self.assertEqual(first_task['name'], 'Team Meeting')
                self.assertIn('Weekly team sync', first_task['description'])
                self.assertIn('Conference Room A', first_task['description'])
                self.assertIn('Original Event Start', first_task['description'])
                self.assertIn('Original Attendees', first_task['description'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_calendar_events.MigrationCalendarEvents.get_cursor')
    def test_migration_with_no_attendees(self, mock_get_cursor):
        """Test calendar events migration with events that have no attendees."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock event data
        mock_event_data = [
            (1, 'Solo Event', 'Personal event', datetime.now(), datetime.now(), False,
             None, 'public', 'busy', 1, None, None, None, None, None)
        ]
        
        # Configure mock cursor behavior
        def mock_execute(query, params=None):
            if 'calendar_event' in query and 'calendar_attendee' not in query:
                mock_cursor.fetchall.return_value = mock_event_data
            elif 'calendar_attendee' in query:
                mock_cursor.fetchall.return_value = []  # No attendees
        
        mock_cursor.execute.side_effect = mock_execute
        
        # Mock project.task operations
        with patch.object(self.env['project.task'], 'search', return_value=self.env['project.task']):
            with patch.object(self.env['project.task'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute migration
                result = self.migration.action_migrate_calendar_events_to_tasks()
                
                # Verify result
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('1 calendar events', result['params']['message'])
                
                # Verify task was created without assignees
                call_args = mock_create.call_args_list[0][0][0]
                self.assertFalse(call_args['user_ids'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_calendar_events.MigrationCalendarEvents.get_cursor')
    def test_migration_with_existing_tasks(self, mock_get_cursor):
        """Test migration behavior when tasks with same name already exist."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock event data
        mock_event_data = [
            (1, 'Existing Task', 'Description', datetime.now(), datetime.now(), False,
             None, 'public', 'busy', 1, None, None, None, None, None)
        ]
        
        def mock_execute(query, params=None):
            if 'calendar_event' in query and 'calendar_attendee' not in query:
                mock_cursor.fetchall.return_value = mock_event_data
            elif 'calendar_attendee' in query:
                mock_cursor.fetchall.return_value = []
        
        mock_cursor.execute.side_effect = mock_execute
        
        # Create existing task
        project = self.migration._get_or_create_migration_project()
        existing_task = self.env['project.task'].create({
            'name': 'Existing Task',
            'project_id': project.id
        })
        
        # Mock search to return existing task
        with patch.object(self.env['project.task'], 'search', return_value=existing_task):
            with patch.object(self.env['project.task'], 'create') as mock_create:
                # Execute migration
                result = self.migration.action_migrate_calendar_events_to_tasks()
                
                # Should succeed but not create new tasks
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('0 tasks created', result['params']['message'])
                mock_create.assert_not_called()

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_calendar_events.MigrationCalendarEvents.get_cursor')
    def test_migration_database_error(self, mock_get_cursor):
        """Test migration behavior when database error occurs."""
        # Mock cursor to raise an exception
        mock_get_cursor.side_effect = Exception("Database connection failed")
        
        # Execute migration and expect UserError
        with self.assertRaises(UserError) as context:
            self.migration.action_migrate_calendar_events_to_tasks()
        
        self.assertIn("Calendar events migration failed", str(context.exception))
        self.assertEqual(self.migration.migration_status, 'failed')

    def test_migration_status_updates(self):
        """Test that migration status is properly updated during migration."""
        # Initially should be not_started
        self.assertEqual(self.migration.migration_status, 'not_started')
        
        # Mock the migration to test status updates
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            
            def mock_execute(query, params=None):
                if 'calendar_event' in query and 'calendar_attendee' not in query:
                    mock_cursor.fetchall.return_value = []  # No events
                elif 'calendar_attendee' in query:
                    mock_cursor.fetchall.return_value = []  # No attendees
            
            mock_cursor.execute.side_effect = mock_execute
            
            # Execute migration
            self.migration.action_migrate_calendar_events_to_tasks()
            
            # Status should be completed
            self.assertEqual(self.migration.migration_status, 'completed')
            self.assertIn('Calendar events to tasks migration completed', self.migration.migration_log)

    def test_task_description_formatting(self):
        """Test that task descriptions are properly formatted with event details."""
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            
            # Mock event with all details
            start_time = datetime(2024, 1, 15, 10, 0, 0)
            end_time = datetime(2024, 1, 15, 11, 0, 0)
            
            mock_event_data = [
                (1, 'Test Event', 'Original description', start_time, end_time, True,
                 'Test Location', 'private', 'busy', 1, None, None, None, None, None)
            ]
            
            mock_attendee_data = [
                (1, 'accepted', 'test@example.com')
            ]
            
            def mock_execute(query, params=None):
                if 'calendar_event' in query and 'calendar_attendee' not in query:
                    mock_cursor.fetchall.return_value = mock_event_data
                elif 'calendar_attendee' in query:
                    mock_cursor.fetchall.return_value = mock_attendee_data
            
            mock_cursor.execute.side_effect = mock_execute
            
            with patch.object(self.env['project.task'], 'search', return_value=self.env['project.task']):
                with patch.object(self.env['project.task'], 'create') as mock_create:
                    mock_create.return_value = True
                    
                    # Execute migration
                    self.migration.action_migrate_calendar_events_to_tasks()
                    
                    # Check task description formatting
                    call_args = mock_create.call_args_list[0][0][0]
                    description = call_args['description']
                    
                    self.assertIn('Original Description: Original description', description)
                    self.assertIn('Location: Test Location', description)
                    self.assertIn('Privacy: private', description)
                    self.assertIn('Show As: busy', description)
                    self.assertIn('All Day Event: Yes', description)
                    self.assertIn('Original Attendees:', description)
                    self.assertIn('test@example.com (accepted)', description)

    def test_attendee_filtering(self):
        """Test that only accepted and tentative attendees become assignees."""
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            
            # Mock event data
            mock_event_data = [
                (1, 'Test Event', None, datetime.now(), datetime.now(), False,
                 None, 'public', 'busy', 1, None, None, None, None, None)
            ]
            
            # Mock attendee data with different states
            mock_attendee_data = [
                (1, 'accepted', 'accepted@example.com'),    # Should be included
                (2, 'tentative', 'tentative@example.com'),  # Should be included
                (3, 'declined', 'declined@example.com'),    # Should be excluded
                (4, 'needsAction', 'pending@example.com')   # Should be excluded
            ]
            
            def mock_execute(query, params=None):
                if 'calendar_event' in query and 'calendar_attendee' not in query:
                    mock_cursor.fetchall.return_value = mock_event_data
                elif 'calendar_attendee' in query:
                    # The query should filter for accepted/tentative only
                    self.assertIn("'accepted', 'tentative'", query)
                    mock_cursor.fetchall.return_value = mock_attendee_data[:2]  # Only accepted and tentative
            
            mock_cursor.execute.side_effect = mock_execute
            
            with patch.object(self.env['project.task'], 'search', return_value=self.env['project.task']):
                with patch.object(self.env['project.task'], 'create') as mock_create:
                    mock_create.return_value = True
                    
                    # Execute migration
                    self.migration.action_migrate_calendar_events_to_tasks()
                    
                    # Verify only accepted/tentative attendees are assignees
                    call_args = mock_create.call_args_list[0][0][0]
                    user_ids = call_args['user_ids']
                    self.assertEqual(user_ids, [(6, 0, [1, 2])])  # Only first two attendees
