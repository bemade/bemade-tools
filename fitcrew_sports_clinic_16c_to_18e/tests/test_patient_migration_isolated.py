"""Isolated Patient Migration Test - Focus on patient migration only."""
import logging
import os
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install', 'migration', 'patient_isolated')
class TestPatientMigrationIsolated(TransactionCase):
    """Test suite for isolated patient migration testing."""

    def setUp(self):
        """Set up test environment."""
        super().setUp()
        
        # Get database connection parameters from environment
        db_host = os.environ.get('ODOO16_HOST', 'localhost')
        db_name = os.environ.get('ODOO16_DBNAME', '2025-08-01-medsportsuroit-prod')
        db_user = os.environ.get('ODOO16_USER', 'odoo')
        db_password = os.environ.get('ODOO16_PASSWORD', 'y@I^3eNg3*o!$NHA')
        db_port = int(os.environ.get('ODOO16_PORT', '5432'))
        
        # Create migration coordinator
        self.coordinator = self.env['odoo16.database'].create({
            'name': 'Patient Migration Isolated Test',
            'database_host': db_host,
            'database_name': db_name,
            'database_username': db_user,
            'database_password': db_password,
            'database_port': db_port,
            'migration_status': 'not_started'
        })
        
        _logger.info("✅ Patient migration isolated test setup completed")
        _logger.info(f"📊 Database: {db_host}:{db_port}/{db_name}")

    def tearDown(self):
        """Clean up test environment."""
        try:
            if hasattr(self, 'coordinator') and self.coordinator:
                self.coordinator.unlink()
            _logger.info("✅ Patient migration isolated test cleanup completed")
        except Exception as e:
            _logger.warning(f"⚠️ Cleanup warning: {e}")
        super().tearDown()

    def test_patient_migration_direct_call(self):
        """Test patient migration by calling the migration component directly."""
        _logger.info("🧪 Testing patient migration direct call...")
        
        try:
            # Get the sports patients migration component
            sports_patients_migration = self.coordinator.sports_patients_migration_id
            self.assertTrue(sports_patients_migration, "Sports patients migration component should exist")
            
            _logger.info(f"📊 Sports patients migration component: {sports_patients_migration.id}")
            
            # Test database connection first
            with sports_patients_migration.get_cursor() as cr:
                cr.execute("SELECT COUNT(*) FROM sports_patient")
                patient_count = cr.fetchone()[0]
                _logger.info(f"📊 Source database has {patient_count} patients")
            
            # Call the migration method directly
            _logger.info("📋 Calling action_migrate_sports_patients directly...")
            result = sports_patients_migration.action_migrate_sports_patients()
            
            _logger.info(f"📊 Migration result: {result}")
            self.assertIsInstance(result, dict)
            self.assertIn('type', result)
            
            if 'params' in result and 'message' in result['params']:
                _logger.info(f"📊 Migration message: {result['params']['message']}")
            
            # Check migration statistics
            _logger.info(f"📊 Patients migrated: {sports_patients_migration.patients_migrated}")
            _logger.info(f"📊 Patient contacts migrated: {sports_patients_migration.patient_contacts_migrated}")
            _logger.info(f"📊 Team-patient relations migrated: {sports_patients_migration.team_patient_relations_migrated}")
            
            _logger.info("✅ Patient migration direct call test completed successfully")
            
        except Exception as e:
            _logger.error(f"❌ Patient migration direct call failed: {e}")
            import traceback
            _logger.error(f"Full traceback: {traceback.format_exc()}")
            self.fail(f"Patient migration direct call failed: {e}")

    def test_patient_migration_with_minimal_partners(self):
        """Test patient migration after creating minimal required partners."""
        _logger.info("🧪 Testing patient migration with minimal partners...")
        
        try:
            # Create a few basic partners first to avoid the partner creation overhead
            _logger.info("📋 Creating minimal partners for testing...")
            
            # Get first few patients from source to create corresponding partners
            with self.coordinator.get_cursor() as cr:
                cr.execute("""
                    SELECT DISTINCT partner_id, first_name, last_name, email, mobile
                    FROM sports_patient 
                    WHERE partner_id IS NOT NULL
                    ORDER BY id
                    LIMIT 5
                """)
                source_patient_partners = cr.fetchall()
                
                _logger.info(f"📊 Creating {len(source_patient_partners)} basic partners")
                
                for partner_data in source_patient_partners:
                    partner_id, first_name, last_name, email, mobile = partner_data
                    
                    # Check if partner already exists
                    existing_partner = self.env['res.partner'].search([('odoo16_partner_id', '=', partner_id)], limit=1)
                    if not existing_partner:
                        partner_name = f"{first_name or ''} {last_name or ''}".strip() or f"Partner {partner_id}"
                        self.env['res.partner'].create({
                            'name': partner_name,
                            'email': email,
                            'mobile': mobile,
                            'is_company': False,
                            'odoo16_partner_id': partner_id
                        })
                        _logger.info(f"📊 Created partner for ID {partner_id}: {partner_name}")
            
            # Now test patient migration
            sports_patients_migration = self.coordinator.sports_patients_migration_id
            
            _logger.info("📋 Running patient migration with minimal partners...")
            result = sports_patients_migration.action_migrate_sports_patients()
            
            _logger.info(f"📊 Migration result: {result}")
            self.assertIsInstance(result, dict)
            
            _logger.info("✅ Patient migration with minimal partners test completed successfully")
            
        except Exception as e:
            _logger.error(f"❌ Patient migration with minimal partners failed: {e}")
            import traceback
            _logger.error(f"Full traceback: {traceback.format_exc()}")
            self.fail(f"Patient migration with minimal partners failed: {e}")
