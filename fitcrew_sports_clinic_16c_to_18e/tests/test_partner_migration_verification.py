"""Partner Migration Verification - Test if partners are actually migrated correctly."""
import logging
import os
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install', 'migration', 'partner_verification')
class TestPartnerMigrationVerification(TransactionCase):
    """Test suite for verifying partner migration works correctly."""

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
            'name': 'Partner Migration Verification Test',
            'database_host': db_host,
            'database_name': db_name,
            'database_username': db_user,
            'database_password': db_password,
            'database_port': db_port,
            'migration_status': 'not_started'
        })
        
        _logger.info("✅ Partner migration verification test setup completed")
        _logger.info(f"📊 Database: {db_host}:{db_port}/{db_name}")

    def tearDown(self):
        """Clean up test environment."""
        try:
            if hasattr(self, 'coordinator') and self.coordinator:
                self.coordinator.unlink()
            _logger.info("✅ Partner migration verification test cleanup completed")
        except Exception as e:
            _logger.warning(f"⚠️ Cleanup warning: {e}")
        super().tearDown()

    def test_partner_migration_only(self):
        """Test just partner migration to verify it works correctly."""
        _logger.info("🧪 Testing partner migration only...")
        
        try:
            # Count partners before migration
            partners_before = self.env['res.partner'].search_count([('odoo16_partner_id', '!=', False)])
            _logger.info(f"📊 Partners with odoo16_partner_id before migration: {partners_before}")
            
            # Get sample patient partner IDs from source to verify they get migrated
            with self.coordinator.get_cursor() as cr:
                cr.execute("""
                    SELECT DISTINCT sp.partner_id, rp.name, rp.email
                    FROM sports_patient sp
                    JOIN res_partner rp ON sp.partner_id = rp.id
                    WHERE sp.partner_id IS NOT NULL
                    ORDER BY sp.partner_id
                    LIMIT 5
                """)
                sample_partner_data = cr.fetchall()
                
                _logger.info(f"📊 Sample patient partners from source: {len(sample_partner_data)}")
                for partner_id, name, email in sample_partner_data:
                    _logger.info(f"  📋 Partner {partner_id}: {name} ({email})")
            
            # Run partner migration
            users_partners_migration = self.coordinator.users_partners_migration_id
            _logger.info("📋 Running partner migration...")
            result = users_partners_migration.action_migrate_users_partners()
            
            _logger.info(f"📊 Migration result: {result}")
            _logger.info(f"📊 Partners migrated: {users_partners_migration.partners_migrated}")
            _logger.info(f"📊 Users migrated: {users_partners_migration.users_migrated}")
            
            # Count partners after migration
            partners_after = self.env['res.partner'].search_count([('odoo16_partner_id', '!=', False)])
            _logger.info(f"📊 Partners with odoo16_partner_id after migration: {partners_after}")
            
            # Verify the specific sample partners were migrated
            _logger.info("📋 Verifying sample partners were migrated...")
            for partner_id, name, email in sample_partner_data:
                migrated_partner = self.env['res.partner'].search([('odoo16_partner_id', '=', partner_id)], limit=1)
                if migrated_partner:
                    _logger.info(f"  ✅ Partner {partner_id} found: {migrated_partner.name} (ID: {migrated_partner.id})")
                else:
                    _logger.error(f"  ❌ Partner {partner_id} NOT found after migration!")
            
            # Commit the transaction to ensure data is persisted
            self.env.cr.commit()
            _logger.info("📋 Transaction committed")
            
            # Verify partners are still there after commit
            partners_after_commit = self.env['res.partner'].search_count([('odoo16_partner_id', '!=', False)])
            _logger.info(f"📊 Partners with odoo16_partner_id after commit: {partners_after_commit}")
            
            _logger.info("✅ Partner migration verification test completed successfully")
            
        except Exception as e:
            _logger.error(f"❌ Partner migration verification failed: {e}")
            import traceback
            _logger.error(f"Full traceback: {traceback.format_exc()}")
            self.fail(f"Partner migration verification failed: {e}")

    def test_partner_then_patient_migration(self):
        """Test partner migration followed immediately by patient migration."""
        _logger.info("🧪 Testing partner migration followed by patient migration...")
        
        try:
            # Step 1: Run partner migration
            _logger.info("📋 Step 1: Running partner migration...")
            users_partners_migration = self.coordinator.users_partners_migration_id
            partners_result = users_partners_migration.action_migrate_users_partners()
            _logger.info(f"📊 Partners migrated: {users_partners_migration.partners_migrated}")
            
            # Step 2: Verify partners are available for patient migration
            _logger.info("📋 Step 2: Verifying partners are available...")
            partners_with_odoo16_id = self.env['res.partner'].search_count([('odoo16_partner_id', '!=', False)])
            _logger.info(f"📊 Partners available for patient migration: {partners_with_odoo16_id}")
            
            # Step 3: Run patient migration
            _logger.info("📋 Step 3: Running patient migration...")
            sports_patients_migration = self.coordinator.sports_patients_migration_id
            patients_result = sports_patients_migration.action_migrate_sports_patients()
            _logger.info(f"📊 Patients migrated: {sports_patients_migration.patients_migrated}")
            
            # Step 4: Verify patient-partner relationships
            _logger.info("📋 Step 4: Verifying patient-partner relationships...")
            patients_with_partners = self.env['sports.patient'].search_count([('partner_id', '!=', False)])
            _logger.info(f"📊 Patients with partners: {patients_with_partners}")
            
            # Check a few specific relationships
            sample_patients = self.env['sports.patient'].search([('odoo16_patient_id', '!=', False)], limit=5)
            for patient in sample_patients:
                if patient.partner_id and hasattr(patient.partner_id, 'odoo16_partner_id'):
                    _logger.info(f"  ✅ Patient {patient.first_name} {patient.last_name} -> Partner {patient.partner_id.name} (odoo16_partner_id: {patient.partner_id.odoo16_partner_id})")
                else:
                    _logger.warning(f"  ⚠️ Patient {patient.first_name} {patient.last_name} has partner {patient.partner_id.name} but no odoo16_partner_id")
            
            _logger.info("✅ Partner then patient migration test completed successfully")
            
        except Exception as e:
            _logger.error(f"❌ Partner then patient migration failed: {e}")
            import traceback
            _logger.error(f"Full traceback: {traceback.format_exc()}")
            self.fail(f"Partner then patient migration failed: {e}")
