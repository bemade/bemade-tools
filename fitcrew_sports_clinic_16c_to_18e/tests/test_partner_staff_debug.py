# -*- coding: utf-8 -*-
"""
Debug test to investigate missing partner for staff in team migration.
Specifically looking at partner ID 21 and staff 37.
"""

import logging
import os
import psycopg2
from odoo.tests.common import TransactionCase, tagged

_logger = logging.getLogger(__name__)


@tagged('partner_staff_debug')
class TestPartnerStaffDebug(TransactionCase):
    
    def setUp(self):
        super().setUp()
        self.source_db_config = {
            'host': os.getenv('ODOO16_HOST', 'localhost'),
            'database': os.getenv('ODOO16_DBNAME', '2025-08-01-medsportsuroit-prod'),
            'user': os.getenv('ODOO16_USER', 'odoo'),
            'password': os.getenv('ODOO16_PASSWORD', 'y@I^3eNg3*o!$NHA'),
            'port': int(os.getenv('ODOO16_PORT', '5432'))
        }

    def test_investigate_missing_partner_for_staff(self):
        """Investigate partner ID 21 and staff 37 in source database."""
        _logger.info("🔍 Investigating missing partner for staff migration...")
        
        try:
            # Connect to source database
            conn = psycopg2.connect(**self.source_db_config)
            cursor = conn.cursor()
            
            # 1. Check if partner ID 21 exists in source database
            _logger.info("1️⃣ Checking partner ID 21 in source database...")
            cursor.execute("""
                SELECT id, name, email, phone, mobile, is_company, active, 
                       create_date, write_date
                FROM res_partner 
                WHERE id = 21
            """)
            partner_21 = cursor.fetchone()
            
            if partner_21:
                _logger.info(f"✅ Partner 21 found: {partner_21}")
            else:
                _logger.warning("❌ Partner 21 NOT found in source database!")
            
            # 2. Check staff member 37 and what partner it references
            _logger.info("2️⃣ Checking staff member 37...")
            cursor.execute("""
                SELECT id, partner_id, team_id, create_date, write_date
                FROM sports_team_staff 
                WHERE id = 37
            """)
            staff_37 = cursor.fetchone()
            
            if staff_37:
                _logger.info(f"✅ Staff 37 found: {staff_37}")
                staff_partner_id = staff_37[1]
                
                # 3. Check what partner this staff actually references
                if staff_partner_id:
                    _logger.info(f"3️⃣ Staff 37 references partner_id: {staff_partner_id}")
                    cursor.execute("""
                        SELECT id, name, email, phone, mobile, is_company, active
                        FROM res_partner 
                        WHERE id = %s
                    """, (staff_partner_id,))
                    actual_partner = cursor.fetchone()
                    
                    if actual_partner:
                        _logger.info(f"✅ Actual partner for staff 37: {actual_partner}")
                    else:
                        _logger.warning(f"❌ Partner {staff_partner_id} referenced by staff 37 NOT found!")
                else:
                    _logger.warning("❌ Staff 37 has NULL partner_id!")
            else:
                _logger.warning("❌ Staff 37 NOT found in source database!")
            
            # 4. Check all staff records and their partner references
            _logger.info("4️⃣ Checking all staff records and their partner references...")
            cursor.execute("""
                SELECT s.id as staff_id, s.partner_id, s.team_id,
                       p.name as partner_name, p.email, p.active as partner_active
                FROM sports_team_staff s
                LEFT JOIN res_partner p ON s.partner_id = p.id
                ORDER BY s.id
                LIMIT 10
            """)
            staff_records = cursor.fetchall()
            
            _logger.info(f"📊 Found {len(staff_records)} staff records (showing first 10):")
            for record in staff_records:
                staff_id, partner_id, team_id, partner_name, email, partner_active = record
                if partner_id:
                    _logger.info(f"  Staff {staff_id} → Partner {partner_id} ({partner_name}, active: {partner_active})")
                else:
                    _logger.warning(f"  Staff {staff_id} → NULL partner_id")
            
            # 5. Check if there are any staff with missing partners
            _logger.info("5️⃣ Checking for staff with missing or inactive partners...")
            cursor.execute("""
                SELECT s.id as staff_id, s.partner_id, s.team_id
                FROM sports_team_staff s
                LEFT JOIN res_partner p ON s.partner_id = p.id
                WHERE s.partner_id IS NULL OR p.id IS NULL OR p.active = false
                ORDER BY s.id
            """)
            problematic_staff = cursor.fetchall()
            
            if problematic_staff:
                _logger.warning(f"⚠️ Found {len(problematic_staff)} staff with missing/inactive partners:")
                for record in problematic_staff:
                    staff_id, partner_id, team_id = record
                    _logger.warning(f"  Staff {staff_id} → Partner {partner_id} (missing or inactive)")
            else:
                _logger.info("✅ All staff have valid, active partners")
            
            # 6. Check partner migration criteria
            _logger.info("6️⃣ Checking partner migration criteria...")
            cursor.execute("""
                SELECT COUNT(*) as total_partners,
                       COUNT(CASE WHEN active = true THEN 1 END) as active_partners,
                       COUNT(CASE WHEN is_company = true THEN 1 END) as company_partners,
                       COUNT(CASE WHEN is_company = false THEN 1 END) as individual_partners
                FROM res_partner
            """)
            partner_stats = cursor.fetchone()
            _logger.info(f"📊 Partner statistics: Total: {partner_stats[0]}, Active: {partner_stats[1]}, Companies: {partner_stats[2]}, Individuals: {partner_stats[3]}")
            
            cursor.close()
            conn.close()
            
        except Exception as e:
            _logger.error(f"❌ Error investigating partner/staff issue: {str(e)}", exc_info=True)
            raise
        
        _logger.info("🔍 Partner/staff investigation completed")

    def test_check_target_database_partners(self):
        """Check what partners were actually migrated to target database."""
        _logger.info("🎯 Checking migrated partners in target database...")
        
        # Check if any partners with odoo16_partner_id exist
        partners = self.env['res.partner'].search([('odoo16_partner_id', '!=', False)])
        _logger.info(f"📊 Found {len(partners)} partners with odoo16_partner_id in target database")
        
        # Check specifically for partner with odoo16_partner_id = 21
        partner_21 = self.env['res.partner'].search([('odoo16_partner_id', '=', 21)], limit=1)
        if partner_21:
            _logger.info(f"✅ Found partner with odoo16_partner_id=21: {partner_21.name} (ID: {partner_21.id})")
        else:
            _logger.warning("❌ No partner with odoo16_partner_id=21 found in target database")
        
        # Show first 10 migrated partners
        if partners:
            _logger.info("📋 First 10 migrated partners:")
            for partner in partners[:10]:
                _logger.info(f"  Partner {partner.id}: {partner.name} (odoo16_partner_id: {partner.odoo16_partner_id})")
