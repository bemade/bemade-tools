"""Debug Partner Lookup - Investigate why patient-partner mapping fails."""
import logging
import os
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install', 'migration', 'partner_lookup_debug')
class TestPartnerLookupDebug(TransactionCase):
    """Test suite for debugging partner lookup issues in patient migration."""

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
            'name': 'Partner Lookup Debug Test',
            'database_host': db_host,
            'database_name': db_name,
            'database_username': db_user,
            'database_password': db_password,
            'database_port': db_port,
            'migration_status': 'not_started'
        })
        
        _logger.info("✅ Partner lookup debug test setup completed")
        _logger.info(f"📊 Database: {db_host}:{db_port}/{db_name}")

    def tearDown(self):
        """Clean up test environment."""
        try:
            if hasattr(self, 'coordinator') and self.coordinator:
                self.coordinator.unlink()
            _logger.info("✅ Partner lookup debug test cleanup completed")
        except Exception as e:
            _logger.warning(f"⚠️ Cleanup warning: {e}")
        super().tearDown()

    def test_partner_patient_relationship_analysis(self):
        """Analyze the relationship between patients and partners in source vs target."""
        _logger.info("🧪 Analyzing partner-patient relationships...")
        
        try:
            with self.coordinator.get_cursor() as cr:
                # Get sample patients with their partner_ids from source
                cr.execute("""
                    SELECT sp.id as patient_id, sp.first_name, sp.last_name, sp.partner_id,
                           rp.name as partner_name, rp.email as partner_email
                    FROM sports_patient sp
                    LEFT JOIN res_partner rp ON sp.partner_id = rp.id
                    ORDER BY sp.id
                    LIMIT 10
                """)
                source_data = cr.fetchall()
                
                _logger.info(f"📊 Found {len(source_data)} sample patients in source database")
                
                for patient_data in source_data:
                    patient_id, first_name, last_name, partner_id, partner_name, partner_email = patient_data
                    _logger.info(f"📋 Patient {patient_id}: {first_name} {last_name} -> Partner {partner_id}: {partner_name} ({partner_email})")
                    
                    if partner_id:
                        # Check if this partner exists in target database with odoo16_partner_id
                        target_partner = self.env['res.partner'].search([('odoo16_partner_id', '=', partner_id)], limit=1)
                        if target_partner:
                            _logger.info(f"  ✅ Found target partner {target_partner.id}: {target_partner.name}")
                        else:
                            _logger.warning(f"  ❌ Partner {partner_id} NOT found in target database")
                            
                            # Check if partner exists without odoo16_partner_id
                            similar_partners = self.env['res.partner'].search([
                                '|', ('name', '=', partner_name),
                                ('email', '=', partner_email)
                            ])
                            if similar_partners:
                                _logger.info(f"  🔍 Found similar partners: {[(p.id, p.name, getattr(p, 'odoo16_partner_id', None)) for p in similar_partners]}")
                    else:
                        _logger.warning(f"  ⚠️ Patient {patient_id} has no partner_id in source!")
                
            _logger.info("✅ Partner-patient relationship analysis completed")
            
        except Exception as e:
            _logger.error(f"❌ Partner-patient relationship analysis failed: {e}")
            import traceback
            _logger.error(f"Full traceback: {traceback.format_exc()}")
            self.fail(f"Partner-patient relationship analysis failed: {e}")

    def test_migrated_partners_analysis(self):
        """Analyze what partners have been migrated and their odoo16_partner_id values."""
        _logger.info("🧪 Analyzing migrated partners...")
        
        try:
            # Check how many partners have odoo16_partner_id
            partners_with_odoo16_id = self.env['res.partner'].search([('odoo16_partner_id', '!=', False)])
            _logger.info(f"📊 Found {len(partners_with_odoo16_id)} partners with odoo16_partner_id in target database")
            
            # Show sample migrated partners
            for partner in partners_with_odoo16_id[:10]:
                _logger.info(f"📋 Partner {partner.id}: {partner.name} (odoo16_partner_id: {partner.odoo16_partner_id})")
            
            # Check total partners in target
            total_partners = self.env['res.partner'].search_count([])
            _logger.info(f"📊 Total partners in target database: {total_partners}")
            
            # Check if there are partners without odoo16_partner_id
            partners_without_odoo16_id = self.env['res.partner'].search([('odoo16_partner_id', '=', False)])
            _logger.info(f"📊 Found {len(partners_without_odoo16_id)} partners WITHOUT odoo16_partner_id")
            
            _logger.info("✅ Migrated partners analysis completed")
            
        except Exception as e:
            _logger.error(f"❌ Migrated partners analysis failed: {e}")
            import traceback
            _logger.error(f"Full traceback: {traceback.format_exc()}")
            self.fail(f"Migrated partners analysis failed: {e}")

    def test_partner_migration_status(self):
        """Check if partners were actually migrated before patient migration."""
        _logger.info("🧪 Checking partner migration status...")
        
        try:
            # Check if users/partners migration was run
            users_partners_migration = self.coordinator.users_partners_migration_id
            if users_partners_migration:
                _logger.info(f"📊 Users/Partners migration component exists: {users_partners_migration.id}")
                _logger.info(f"📊 Partners migrated: {users_partners_migration.partners_migrated}")
                _logger.info(f"📊 Users migrated: {users_partners_migration.users_migrated}")
                
                if users_partners_migration.partners_migrated == 0:
                    _logger.warning("⚠️ No partners have been migrated! This explains the lookup failures.")
                    
                    # Try running partner migration first
                    _logger.info("📋 Attempting to run partner migration...")
                    result = users_partners_migration.action_migrate_users_partners()
                    _logger.info(f"📊 Partner migration result: {result}")
                    _logger.info(f"📊 Partners migrated after run: {users_partners_migration.partners_migrated}")
            else:
                _logger.error("❌ Users/Partners migration component not found!")
            
            _logger.info("✅ Partner migration status check completed")
            
        except Exception as e:
            _logger.error(f"❌ Partner migration status check failed: {e}")
            import traceback
            _logger.error(f"Full traceback: {traceback.format_exc()}")
            self.fail(f"Partner migration status check failed: {e}")
