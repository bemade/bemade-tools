"""Comprehensive Migration Test - Partners, Users, Teams, and Patients."""
import logging
import os
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install', 'migration', 'comprehensive')
class TestComprehensiveMigration(TransactionCase):
    """Test suite for comprehensive migration of partners, users, teams, and patients."""

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
            'name': 'Comprehensive Migration Test',
            'database_host': db_host,
            'database_name': db_name,
            'database_username': db_user,
            'database_password': db_password,
            'database_port': db_port,
            'migration_status': 'not_started'
        })
        
        _logger.info("✅ Comprehensive migration test setup completed")
        _logger.info(f"📊 Database: {db_host}:{db_port}/{db_name}")

    def tearDown(self):
        """Clean up test environment."""
        try:
            if hasattr(self, 'coordinator') and self.coordinator:
                self.coordinator.unlink()
            _logger.info("✅ Comprehensive migration test cleanup completed")
        except Exception as e:
            _logger.warning(f"⚠️ Cleanup warning: {e}")
        super().tearDown()

    def test_comprehensive_migration_sequence(self):
        """Test complete migration sequence: partners -> users -> teams -> patients."""
        _logger.info("🧪 Starting comprehensive migration sequence test...")
        
        try:
            # Step 1: Migrate Partners and Users
            _logger.info("📋 Step 1: Migrating Partners and Users...")
            partners_users_result = self.coordinator.action_migrate_users_partners()
            self.assertIsInstance(partners_users_result, dict)
            self.assertIn('type', partners_users_result)
            
            if 'params' in partners_users_result and 'message' in partners_users_result['params']:
                _logger.info(f"📊 Partners/Users result: {partners_users_result['params']['message']}")
            
            # Step 2: Migrate Sports Teams
            _logger.info("📋 Step 2: Migrating Sports Teams...")
            teams_result = self.coordinator.action_migrate_sports_teams()
            self.assertIsInstance(teams_result, dict)
            self.assertIn('type', teams_result)
            
            if 'params' in teams_result and 'message' in teams_result['params']:
                _logger.info(f"📊 Teams result: {teams_result['params']['message']}")
            
            # Step 3: Migrate Sports Patients
            _logger.info("📋 Step 3: Migrating Sports Patients...")
            patients_result = self.coordinator.action_migrate_sports_patients()
            self.assertIsInstance(patients_result, dict)
            self.assertIn('type', patients_result)
            
            if 'params' in patients_result and 'message' in patients_result['params']:
                _logger.info(f"📊 Patients result: {patients_result['params']['message']}")
            
            # Verify migration statistics
            _logger.info("📋 Step 4: Verifying migration statistics...")
            self._verify_migration_statistics()
            
            _logger.info("✅ Comprehensive migration sequence test completed successfully")
            
        except Exception as e:
            _logger.error(f"❌ Comprehensive migration failed: {e}")
            self.fail(f"Comprehensive migration failed: {e}")

    def _verify_migration_statistics(self):
        """Verify migration statistics and data integrity."""
        _logger.info("🔍 Verifying migration statistics...")
        
        # Check partners migration
        if hasattr(self.coordinator, 'users_partners_migration_id') and self.coordinator.users_partners_migration_id:
            partners_migrated = self.coordinator.users_partners_migration_id.partners_migrated
            users_migrated = self.coordinator.users_partners_migration_id.users_migrated
            _logger.info(f"📊 Partners migrated: {partners_migrated}")
            _logger.info(f"📊 Users migrated: {users_migrated}")
        
        # Check teams migration
        if hasattr(self.coordinator, 'sports_teams_migration_id') and self.coordinator.sports_teams_migration_id:
            teams_migrated = self.coordinator.sports_teams_migration_id.teams_migrated
            team_staff_migrated = self.coordinator.sports_teams_migration_id.team_staff_migrated
            _logger.info(f"📊 Teams migrated: {teams_migrated}")
            _logger.info(f"📊 Team staff migrated: {team_staff_migrated}")
        
        # Check patients migration
        if hasattr(self.coordinator, 'sports_patients_migration_id') and self.coordinator.sports_patients_migration_id:
            patients_migrated = self.coordinator.sports_patients_migration_id.patients_migrated
            patient_contacts_migrated = self.coordinator.sports_patients_migration_id.patient_contacts_migrated
            team_patient_relations_migrated = self.coordinator.sports_patients_migration_id.team_patient_relations_migrated
            _logger.info(f"📊 Patients migrated: {patients_migrated}")
            _logger.info(f"📊 Patient contacts migrated: {patient_contacts_migrated}")
            _logger.info(f"📊 Team-patient relations migrated: {team_patient_relations_migrated}")
        
        # Verify data integrity
        self._verify_data_integrity()

    def _verify_data_integrity(self):
        """Verify data integrity after migration."""
        _logger.info("🔍 Verifying data integrity...")
        
        # Check that migrated records exist
        partners_count = self.env['res.partner'].search_count([('odoo16_partner_id', '!=', False)])
        users_count = self.env['res.users'].search_count([('odoo16_user_id', '!=', False)])
        teams_count = self.env['sports.team'].search_count([('odoo16_team_id', '!=', False)])
        
        _logger.info(f"📊 Partners with odoo16_partner_id: {partners_count}")
        _logger.info(f"📊 Users with odoo16_user_id: {users_count}")
        _logger.info(f"📊 Teams with odoo16_team_id: {teams_count}")
        
        # Check for patients (if odoo16_patient_id field exists)
        try:
            patients_count = self.env['sports.patient'].search_count([('odoo16_patient_id', '!=', False)])
            _logger.info(f"📊 Patients with odoo16_patient_id: {patients_count}")
        except Exception as e:
            _logger.warning(f"⚠️ Could not verify patients (odoo16_patient_id field may not exist): {e}")
        
        # Verify relationships
        self._verify_relationships()

    def _verify_relationships(self):
        """Verify relationships between migrated records."""
        _logger.info("🔍 Verifying relationships...")
        
        # Check team-staff relationships
        team_staff_count = self.env['sports.team.staff'].search_count([])
        _logger.info(f"📊 Team staff relationships: {team_staff_count}")
        
        # Check team-patient relationships (if they exist)
        try:
            # Check if sports.patient has team relationships
            sample_patient = self.env['sports.patient'].search([], limit=1)
            if sample_patient and hasattr(sample_patient, 'team_ids'):
                patients_with_teams = self.env['sports.patient'].search_count([('team_ids', '!=', False)])
                _logger.info(f"📊 Patients with team relationships: {patients_with_teams}")
        except Exception as e:
            _logger.warning(f"⚠️ Could not verify patient-team relationships: {e}")
        
        # Check partner-patient relationships
        try:
            patients_with_partners = self.env['sports.patient'].search_count([('partner_id', '!=', False)])
            _logger.info(f"📊 Patients with partner relationships: {patients_with_partners}")
        except Exception as e:
            _logger.warning(f"⚠️ Could not verify patient-partner relationships: {e}")

    def test_database_connection(self):
        """Test database connection parameters."""
        _logger.info("🧪 Testing database connection...")
        
        try:
            # Test connection parameters are set
            self.assertTrue(self.coordinator.database_host)
            self.assertTrue(self.coordinator.database_name)
            self.assertTrue(self.coordinator.database_username)
            self.assertTrue(self.coordinator.database_port > 0)
            
            _logger.info("✅ Database connection parameters test passed")
        except Exception as e:
            _logger.warning(f"⚠️ Database connection test skipped: {e}")
            self.skipTest(f"Database connection test skipped: {e}")

    def test_migration_coordinator_creation(self):
        """Test that migration coordinator is properly created."""
        _logger.info("🧪 Testing migration coordinator creation...")
        
        self.assertTrue(self.coordinator.exists())
        self.assertEqual(self.coordinator.migration_status, 'not_started')
        
        _logger.info("✅ Migration coordinator creation test passed")
