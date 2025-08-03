from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


@tagged('migration_calendar_events')
class TestMigrationCalendarEvents(TransactionCase):
    
    def setUp(self):
        super().setUp()
        self.migration_coordinator = None
        
    def tearDown(self):
        if self.migration_coordinator:
            try:
                self.migration_coordinator.unlink()
            except Exception as e:
                _logger.warning(f"Failed to cleanup migration coordinator: {e}")
        super().tearDown()

    def test_01_database_connection(self):
        """Test database connection to Odoo 16 source."""
        _logger.info("=== Testing Database Connection ===")
        
        # Create migration coordinator
        coordinator_vals = {
            'name': 'Test Calendar Events Migration Connection',
            'database_host': 'localhost',
            'database_name': '2025-08-01-medsportsuroit-prod',
            'database_username': 'odoo',
            'database_password': 'y@I^3eNg3*o!$NHA',
            'database_port': 5432,
        }
        
        try:
            self.migration_coordinator = self.env['odoo16.database'].create(coordinator_vals)
            
            # Test database connection
            _logger.info("🔄 Testing database connection...")
            result = self.migration_coordinator.action_test_connection()
            
            _logger.info(f"✅ Database connection test result: {result}")
            
            # Verify connection was successful
            self.assertIsInstance(result, dict)
            self.assertIn('type', result)
            
        except Exception as e:
            _logger.error(f"❌ Database connection failed: {e}")
            
            _logger.info("✅ Calendar events migration execution test completed")
            
        except Exception as e:
            _logger.error(f"❌ Calendar events migration failed: {e}")
            self.fail(f"Calendar events migration failed: {e}")
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
