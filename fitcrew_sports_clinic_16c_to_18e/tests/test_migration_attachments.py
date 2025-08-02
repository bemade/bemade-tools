from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError
import os
import logging
from unittest.mock import patch, MagicMock
import base64

_logger = logging.getLogger(__name__)


class TestMigrationAttachments(TransactionCase):
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
        
        # Create attachments migration record
        self.migration = self.env["migration.attachments"].create({
            "database_id": self.database_base.id
        })

    def test_migration_creation(self):
        """Test that attachments migration record can be created properly."""
        self.assertTrue(self.migration.id)
        self.assertEqual(self.migration.database_id, self.database_base)
        
        # Test inherited fields from base
        self.assertEqual(self.migration.database_host, self.database_base.database_host)
        self.assertEqual(self.migration.migration_status, 'not_started')

    def test_skip_filestore_default(self):
        """Test that skip_filestore defaults to True."""
        self.assertTrue(self.migration.skip_filestore)

    def test_migration_method_exists(self):
        """Test that migration method exists and is callable."""
        self.assertTrue(hasattr(self.migration, 'action_migrate_attachments'))
        self.assertTrue(callable(getattr(self.migration, 'action_migrate_attachments')))

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_attachments.MigrationAttachments.get_cursor')
    def test_migration_with_filestore_skip(self, mock_get_cursor):
        """Test attachments migration with filestore skip enabled."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock attachment data
        test_file_data = base64.b64encode(b'test file content').decode()
        mock_attachment_data = [
            (1, 'test_file.txt', 'test_file.txt', 'Test file description', 'res.partner', 1, None,
             1, 'binary', None, False, 'token123', test_file_data, '/path/to/file',
             100, 'checksum123', 'text/plain', 'test content', True, None, None, None, None),
            (2, 'image.png', 'image.png', 'Test image', 'res.partner', 2, None,
             1, 'binary', None, False, 'token456', test_file_data, '/path/to/image',
             200, 'checksum456', 'image/png', None, True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_attachment_data
        
        # Ensure skip_filestore is True
        self.migration.skip_filestore = True
        
        # Mock search to return no existing attachments
        with patch.object(self.env['ir.attachment'], 'search', return_value=self.env['ir.attachment']):
            with patch.object(self.env['ir.attachment'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute migration
                result = self.migration.action_migrate_attachments()
                
                # Verify result
                self.assertEqual(result['type'], 'ir.actions.client')
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('2 attachments', result['params']['message'])
                self.assertIn('File content was skipped', result['params']['message'])
                
                # Verify attachments were created with nullified file data
                self.assertEqual(mock_create.call_count, 2)
                
                # Check first attachment call
                first_call = mock_create.call_args_list[0][0][0]
                self.assertEqual(first_call['name'], 'test_file.txt')
                self.assertEqual(first_call['res_model'], 'res.partner')
                self.assertEqual(first_call['res_id'], 1)
                self.assertFalse(first_call['datas'])  # Should be nullified
                self.assertFalse(first_call['store_fname'])  # Should be nullified
                self.assertFalse(first_call['checksum'])  # Should be nullified
                self.assertIn('[NOTE: Original file content not migrated', first_call['description'])

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_attachments.MigrationAttachments.get_cursor')
    def test_migration_without_filestore_skip(self, mock_get_cursor):
        """Test attachments migration with filestore skip disabled."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock attachment data
        test_file_data = base64.b64encode(b'test file content').decode()
        mock_attachment_data = [
            (1, 'test_file.txt', 'test_file.txt', 'Test file description', 'res.partner', 1, None,
             1, 'binary', None, False, 'token123', test_file_data, '/path/to/file',
             100, 'checksum123', 'text/plain', 'test content', True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_attachment_data
        
        # Disable skip_filestore
        self.migration.skip_filestore = False
        
        # Mock search to return no existing attachments
        with patch.object(self.env['ir.attachment'], 'search', return_value=self.env['ir.attachment']):
            with patch.object(self.env['ir.attachment'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute migration
                result = self.migration.action_migrate_attachments()
                
                # Verify result
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('1 attachments', result['params']['message'])
                self.assertNotIn('File content was skipped', result['params']['message'])
                
                # Verify attachment was created with original file data
                call_args = mock_create.call_args_list[0][0][0]
                self.assertEqual(call_args['datas'], test_file_data)  # Should preserve original data
                self.assertEqual(call_args['store_fname'], '/path/to/file')  # Should preserve store_fname
                self.assertEqual(call_args['checksum'], 'checksum123')  # Should preserve checksum
                self.assertNotIn('[NOTE: Original file content not migrated', call_args.get('description', ''))

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_attachments.MigrationAttachments.get_cursor')
    def test_migration_with_existing_attachments(self, mock_get_cursor):
        """Test migration behavior when attachments already exist."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock attachment data
        mock_attachment_data = [
            (1, 'existing_file.txt', 'existing_file.txt', 'Existing file', 'res.partner', 1, None,
             1, 'binary', None, False, 'token123', 'data', '/path/to/file',
             100, 'checksum123', 'text/plain', 'content', True, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_attachment_data
        
        # Create existing attachment
        existing_attachment = self.env['ir.attachment'].create({
            'name': 'existing_file.txt',
            'res_model': 'res.partner',
            'res_id': 1
        })
        
        # Mock search to return existing attachment
        with patch.object(self.env['ir.attachment'], 'search', return_value=existing_attachment):
            with patch.object(self.env['ir.attachment'], 'create') as mock_create:
                # Execute migration
                result = self.migration.action_migrate_attachments()
                
                # Should succeed but not create new attachments
                self.assertEqual(result['params']['type'], 'success')
                self.assertIn('0 attachments', result['params']['message'])
                mock_create.assert_not_called()

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_attachments.MigrationAttachments.get_cursor')
    def test_migration_database_error(self, mock_get_cursor):
        """Test migration behavior when database error occurs."""
        # Mock cursor to raise an exception
        mock_get_cursor.side_effect = Exception("Database connection failed")
        
        # Execute migration and expect UserError
        with self.assertRaises(UserError) as context:
            self.migration.action_migrate_attachments()
        
        self.assertIn("Attachments migration failed", str(context.exception))
        self.assertEqual(self.migration.migration_status, 'failed')

    def test_migration_status_updates(self):
        """Test that migration status is properly updated during migration."""
        # Initially should be not_started
        self.assertEqual(self.migration.migration_status, 'not_started')
        
        # Mock the migration to test status updates
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []  # No attachments to migrate
            
            # Execute migration
            self.migration.action_migrate_attachments()
            
            # Status should be completed
            self.assertEqual(self.migration.migration_status, 'completed')
            self.assertIn('Attachments migration completed', self.migration.migration_log)

    @patch('odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_attachments.MigrationAttachments.get_cursor')
    def test_attachment_field_mapping(self, mock_get_cursor):
        """Test that all attachment fields are properly mapped."""
        mock_cursor = MagicMock()
        mock_get_cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock comprehensive attachment data
        mock_attachment_data = [
            (1, 'test.pdf', 'test.pdf', 'Test PDF', 'project.task', 5, 'attachment_ids',
             2, 'url', 'http://example.com/file.pdf', True, 'public_token', None, None,
             1024, None, 'application/pdf', 'PDF content', False, None, None, None, None)
        ]
        mock_cursor.fetchall.return_value = mock_attachment_data
        
        # Mock search to return no existing attachments
        with patch.object(self.env['ir.attachment'], 'search', return_value=self.env['ir.attachment']):
            with patch.object(self.env['ir.attachment'], 'create') as mock_create:
                mock_create.return_value = True
                
                # Execute migration
                self.migration.action_migrate_attachments()
                
                # Verify all fields are mapped correctly
                call_args = mock_create.call_args_list[0][0][0]
                self.assertEqual(call_args['name'], 'test.pdf')
                self.assertEqual(call_args['datas_fname'], 'test.pdf')
                self.assertEqual(call_args['description'], 'Test PDF')
                self.assertEqual(call_args['res_model'], 'project.task')
                self.assertEqual(call_args['res_id'], 5)
                self.assertEqual(call_args['res_field'], 'attachment_ids')
                self.assertEqual(call_args['company_id'], 2)
                self.assertEqual(call_args['type'], 'url')
                self.assertEqual(call_args['url'], 'http://example.com/file.pdf')
                self.assertTrue(call_args['public'])
                self.assertEqual(call_args['access_token'], 'public_token')
                self.assertEqual(call_args['file_size'], 1024)
                self.assertEqual(call_args['mimetype'], 'application/pdf')
                self.assertEqual(call_args['index_content'], 'PDF content')
                self.assertFalse(call_args['active'])

    def test_skip_filestore_description_modification(self):
        """Test that description is properly modified when skipping filestore."""
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            
            # Mock attachment with existing description
            mock_attachment_data = [
                (1, 'test.txt', 'test.txt', 'Original description', 'res.partner', 1, None,
                 1, 'binary', None, False, 'token', 'data', '/path',
                 100, 'checksum', 'text/plain', 'content', True, None, None, None, None)
            ]
            mock_cursor.fetchall.return_value = mock_attachment_data
            
            # Enable skip_filestore
            self.migration.skip_filestore = True
            
            with patch.object(self.env['ir.attachment'], 'search', return_value=self.env['ir.attachment']):
                with patch.object(self.env['ir.attachment'], 'create') as mock_create:
                    mock_create.return_value = True
                    
                    # Execute migration
                    self.migration.action_migrate_attachments()
                    
                    # Check description was modified
                    call_args = mock_create.call_args_list[0][0][0]
                    description = call_args['description']
                    self.assertIn('Original description', description)
                    self.assertIn('[NOTE: Original file content not migrated', description)

    def test_skip_filestore_with_empty_description(self):
        """Test filestore skip behavior with empty description."""
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            
            # Mock attachment with no description
            mock_attachment_data = [
                (1, 'test.txt', 'test.txt', None, 'res.partner', 1, None,
                 1, 'binary', None, False, 'token', 'data', '/path',
                 100, 'checksum', 'text/plain', 'content', True, None, None, None, None)
            ]
            mock_cursor.fetchall.return_value = mock_attachment_data
            
            # Enable skip_filestore
            self.migration.skip_filestore = True
            
            with patch.object(self.env['ir.attachment'], 'search', return_value=self.env['ir.attachment']):
                with patch.object(self.env['ir.attachment'], 'create') as mock_create:
                    mock_create.return_value = True
                    
                    # Execute migration
                    self.migration.action_migrate_attachments()
                    
                    # Check description was created with note
                    call_args = mock_create.call_args_list[0][0][0]
                    description = call_args['description']
                    self.assertIn('[NOTE: Original file content not migrated', description)

    def test_page_size_usage(self):
        """Test that PAGE_SIZE is properly used in the query."""
        from odoo.addons.fitcrew_sports_clinic_16c_to_18e.models.migration_attachments import PAGE_SIZE
        
        with patch.object(self.migration, 'get_cursor') as mock_get_cursor:
            mock_cursor = MagicMock()
            mock_get_cursor.return_value.__enter__.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []
            
            # Execute migration
            self.migration.action_migrate_attachments()
            
            # Verify PAGE_SIZE was used in the query
            execute_call = mock_cursor.execute.call_args[0]
            query = execute_call[0]
            params = execute_call[1]
            
            self.assertIn('LIMIT %s', query)
            self.assertEqual(params[0], PAGE_SIZE)
            self.assertEqual(PAGE_SIZE, 1000)
