"""Debug Patient Migration Test - Focused debugging of patient migration issues."""
import logging
import os
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install', 'migration', 'patient_debug')
class TestPatientMigrationDebug(TransactionCase):
    """Test suite for debugging patient migration issues."""

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
            'name': 'Patient Migration Debug Test',
            'database_host': db_host,
            'database_name': db_name,
            'database_username': db_user,
            'database_password': db_password,
            'database_port': db_port,
            'migration_status': 'not_started'
        })
        
        _logger.info("✅ Patient migration debug test setup completed")
        _logger.info(f"📊 Database: {db_host}:{db_port}/{db_name}")

    def tearDown(self):
        """Clean up test environment."""
        try:
            if hasattr(self, 'coordinator') and self.coordinator:
                self.coordinator.unlink()
            _logger.info("✅ Patient migration debug test cleanup completed")
        except Exception as e:
            _logger.warning(f"⚠️ Cleanup warning: {e}")
        super().tearDown()

    def test_database_connection_only(self):
        """Test just the database connection without migration."""
        _logger.info("🧪 Testing database connection only...")
        
        try:
            # Test connection using the base class method
            with self.coordinator.get_cursor() as cr:
                cr.execute("SELECT COUNT(*) FROM sports_patient")
                count = cr.fetchone()[0]
                _logger.info(f"📊 Found {count} patients in source database")
                
            _logger.info("✅ Database connection test passed")
        except Exception as e:
            _logger.error(f"❌ Database connection failed: {e}")
            self.fail(f"Database connection failed: {e}")

    def test_patient_migration_step_by_step(self):
        """Test patient migration with detailed step-by-step debugging."""
        _logger.info("🧪 Testing patient migration step by step...")
        
        try:
            # Step 1: Test database connection
            _logger.info("📋 Step 1: Testing database connection...")
            with self.coordinator.get_cursor() as cr:
                cr.execute("SELECT COUNT(*) FROM sports_patient")
                patient_count = cr.fetchone()[0]
                _logger.info(f"📊 Source database has {patient_count} patients")
            
            # Step 2: Test sports.patient model access
            _logger.info("📋 Step 2: Testing sports.patient model access...")
            existing_patients = self.env['sports.patient'].search_count([])
            _logger.info(f"📊 Target database has {existing_patients} existing patients")
            
            # Step 3: Test creating a simple patient record
            _logger.info("📋 Step 3: Testing simple patient creation...")
            test_patient = self.env['sports.patient'].create({
                'first_name': 'Test',
                'last_name': 'Patient',
                'odoo16_patient_id': 99999  # Use a high ID to avoid conflicts
            })
            _logger.info(f"📊 Created test patient: {test_patient.id}")
            
            # Step 4: Test the actual migration method with limited data
            _logger.info("📋 Step 4: Testing migration with limited data...")
            self._test_limited_patient_migration()
            
            _logger.info("✅ Patient migration step-by-step test completed")
            
        except Exception as e:
            _logger.error(f"❌ Patient migration step-by-step test failed: {e}")
            import traceback
            _logger.error(f"Full traceback: {traceback.format_exc()}")
            self.fail(f"Patient migration step-by-step test failed: {e}")

    def _test_limited_patient_migration(self):
        """Test migration with just the first few patients to isolate issues."""
        _logger.info("🔍 Testing limited patient migration...")
        
        try:
            with self.coordinator.get_cursor() as cr:
                # Get just the first 3 patients for testing
                cr.execute("""
                    SELECT id, first_name, last_name, email, partner_id, date_of_birth, mobile,
                           match_status, practice_status, predicted_return_date, return_date, 
                           last_consultation_date, allergies, team_info_notes,
                           create_date, create_uid, write_date, write_uid
                    FROM sports_patient 
                    ORDER BY id
                    LIMIT 3
                """)
                source_patients = cr.fetchall()
                
                _logger.info(f"📊 Testing with {len(source_patients)} patients")
                
                for i, patient_data in enumerate(source_patients):
                    try:
                        _logger.info(f"📋 Processing patient {i+1}/{len(source_patients)}")
                        
                        (patient_id, first_name, last_name, email, partner_id, date_of_birth, mobile,
                         match_status, practice_status, predicted_return_date, return_date,
                         last_consultation_date, allergies, team_info_notes,
                         create_date, create_uid, write_date, write_uid) = patient_data
                        
                        _logger.info(f"📊 Patient data: ID={patient_id}, Name={first_name} {last_name}, Partner ID={partner_id}")
                        
                        # Check if patient already exists
                        existing_patient = self.env['sports.patient'].search([('odoo16_patient_id', '=', patient_id)], limit=1)
                        if existing_patient:
                            _logger.info(f"📊 Patient {patient_id} already exists, skipping")
                            continue
                        
                        # Find or create corresponding partner (MANDATORY!)
                        partner = None
                        if partner_id:
                            partner = self.env['res.partner'].with_context(active_test=False).search([('odoo16_partner_id', '=', partner_id)], limit=1)
                            if partner:
                                _logger.info(f"📊 Found existing partner {partner.id} for patient {patient_id}")
                            else:
                                _logger.warning(f"⚠️ Partner with odoo16_partner_id {partner_id} not found, creating basic partner")
                        
                        if not partner:
                            # Create a basic partner for this patient (partner_id is MANDATORY)
                            partner_name = f"{first_name or ''} {last_name or ''}".strip() or f"Patient {patient_id}"
                            partner = self.env['res.partner'].create({
                                'name': partner_name,
                                'email': email,
                                'mobile': mobile,
                                'is_company': False,
                                'odoo16_partner_id': partner_id or (100000 + patient_id)  # Use offset if no partner_id
                            })
                            _logger.info(f"📊 Created basic partner {partner.id} for patient {patient_id}")
                        
                        # Prepare patient values with MANDATORY partner_id
                        patient_vals = {
                            'first_name': first_name or '',
                            'last_name': last_name or '',
                            'partner_id': partner.id,  # MANDATORY!
                            'date_of_birth': date_of_birth,
                            'match_status': match_status or 'yes',
                            'practice_status': practice_status or 'yes',
                            'predicted_return_date': predicted_return_date,
                            'return_date': return_date,
                            'last_consultation_date': last_consultation_date,
                            'allergies': allergies,
                            'team_info_notes': team_info_notes,
                            'odoo16_patient_id': patient_id,
                        }
                        
                        _logger.info(f"📊 Creating patient with partner_id: {partner.id}")
                        
                        # Create patient in target database
                        new_patient = self.env['sports.patient'].create(patient_vals)
                        _logger.info(f"✅ Successfully created patient: {first_name} {last_name} (ID: {patient_id} -> {new_patient.id})")
                        
                    except Exception as e:
                        _logger.error(f"❌ Failed to migrate patient {patient_id} ({first_name} {last_name}): {str(e)}")
                        import traceback
                        _logger.error(f"Full traceback: {traceback.format_exc()}")
                        raise  # Re-raise to see the full error
                        
        except Exception as e:
            _logger.error(f"❌ Limited patient migration failed: {e}")
            raise
