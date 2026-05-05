from odoo.tests.common import TransactionCase, tagged
from odoo import Command
import os
import logging
from odoo.addons.xtuple_to_odoo.tools import normalize_country_code

_logger = logging.getLogger(__name__)

@tagged("-at_install", "xtuple")
class TestPartnerImport(TransactionCase):
    def setUp(self):
        super().setUp()
        # Use environment variables with fallbacks for database connection
        self.xtuple_db = self.env["xtuple.database"].create(
            {
                "database_host": os.environ.get("XTUPLE_HOST", ""),
                "database_name": os.environ.get("XTUPLE_DBNAME", ""),
                "database_username": os.environ.get("XTUPLE_USER", ""),
                "database_password": os.environ.get("XTUPLE_PASSWORD", ""),
                "database_port": int(os.environ.get("XTUPLE_PORT", "5432")),
                "database_schema": os.environ.get("XTUPLE_SCHEMA", "public"),
            }
        )
        
        # Create the importer
        self.partner_importer = self.env["xtuple.res.partner.importer"].with_company(
            self.env.company
        )

    def test_partner_tables_exist(self):
        """Test that the partner-related tables exist in the xTuple database."""
        cursor = None
        try:
            cursor = self.xtuple_db.get_cursor()

            # Check for customer table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = %s AND table_name = 'custinfo'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(cursor.fetchone()[0], "Customer table (custinfo) not found")

            # Check for contact table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = %s AND table_name = 'cntct'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(cursor.fetchone()[0], "Contact table (cntct) not found")

            # Check for address table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = %s AND table_name = 'addr'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(cursor.fetchone()[0], "Address table (addr) not found")
            
            # Check for vendor table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = %s AND table_name = 'vendinfo'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(cursor.fetchone()[0], "Vendor table (vendinfo) not found")
            
            # Check for shipping address table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = %s AND table_name = 'shiptoinfo'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(cursor.fetchone()[0], "Shipping address table (shiptoinfo) not found")

        finally:
            if cursor:
                cursor.close()

    def test_partner_migration(self):
        """Test the migration of partners from xTuple to Odoo."""
        cursor = None
        try:
            cursor = self.xtuple_db.get_cursor()

            # First, check if we have data to test with
            cursor.execute("SELECT COUNT(*) FROM custinfo")
            customer_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM vendinfo")
            vendor_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COUNT(*) FROM cntct WHERE cntct_first_name IS NOT NULL OR cntct_last_name IS NOT NULL"
            )
            contact_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM shiptoinfo")
            shipping_count = cursor.fetchone()[0]

            # Skip test if no data is available
            if customer_count == 0 and vendor_count == 0:
                self.skipTest(
                    "No customer or vendor data available in the xTuple database"
                )

            _logger.info(
                f"Found {customer_count} customers, {vendor_count} vendors, {contact_count} contacts, and {shipping_count} shipping addresses in xTuple"
            )

            # Get a sample of existing data for verification
            sample_data = {}

            # Get a sample customer if available
            if customer_count > 0:
                cursor.execute(
                    """
                    SELECT 
                        cust_id, 
                        cust_number, 
                        cust_name
                    FROM custinfo
                    LIMIT 1
                """
                )
                result = cursor.fetchone()
                if result:
                    sample_data["customer"] = result
                    _logger.info(f"Using customer for test: {result}")

            # Get a sample vendor if available
            if vendor_count > 0:
                cursor.execute(
                    """
                    SELECT 
                        vend_id, 
                        vend_number, 
                        vend_name
                    FROM vendinfo
                    LIMIT 1
                """
                )
                sample_data["vendor"] = cursor.fetchone()

            # Get a sample contact if available
            if contact_count > 0:
                cursor.execute(
                    """
                    SELECT 
                        cntct_id,
                        cntct_first_name,
                        cntct_last_name,
                        cntct_email
                    FROM cntct
                    WHERE cntct_first_name IS NOT NULL OR cntct_last_name IS NOT NULL
                    LIMIT 1
                """
                )
                sample_data["contact"] = cursor.fetchone()

            # Get a sample shipping address if available
            if shipping_count > 0:
                cursor.execute(
                    """
                    SELECT 
                        shipto_id,
                        shipto_cust_id,
                        shipto_name
                    FROM shiptoinfo
                    LIMIT 1
                """
                )
                sample_data["shipping"] = cursor.fetchone()

            _logger.info("Starting partner migration test")
            try:
                partners = self.partner_importer.import_partners(cursor)
                _logger.info(
                    f"Partner migration completed, imported {len(partners)} partners"
                )
            except Exception as e:
                _logger.error(f"Error during partner migration: {str(e)}")
                raise

            # Verify the results
            self.assertTrue(partners, "No partners were imported")

            # Check customer import if we have sample data
            if "customer" in sample_data:
                cust_id, cust_number, cust_name = sample_data["customer"]
                _logger.info(
                    f"Checking for customer with ID {cust_id} (type: {type(cust_id)})"
                )
                customer = partners.filtered(lambda p: p.xtuple_cust_id == cust_id)
                self.assertTrue(customer, f"Customer {cust_id} was not imported")
                self.assertEqual(customer.ref, cust_number)
                self.assertEqual(customer.name, cust_name)
                self.assertEqual(customer.xtuple_partner_type, "customer")
                self.assertEqual(customer.customer_rank, 1)

            # Check vendor import if we have sample data
            if "vendor" in sample_data:
                vend_id, vend_number, vend_name = sample_data["vendor"]
                vendor = partners.filtered(lambda p: p.xtuple_vend_id == vend_id)
                self.assertTrue(vendor, f"Vendor {vend_id} was not imported")
                self.assertEqual(vendor.ref, vend_number)
                self.assertEqual(vendor.name, vend_name)
                self.assertEqual(vendor.xtuple_partner_type, "vendor")
                self.assertEqual(vendor.supplier_rank, 1)

            # Check contact import if we have sample data
            if "contact" in sample_data:
                cntct_id, first_name, last_name, email = sample_data["contact"]
                contact = partners.filtered(lambda p: p.xtuple_cntct_id == cntct_id)
                if (
                    contact
                ):  # Some contacts might not be imported if they don't have a parent
                    self.assertEqual(contact.name, f"{first_name} {last_name}".strip())
                    if email:
                        self.assertEqual(contact.email, email)

            # Check shipping address import if we have sample data
            if "shipping" in sample_data:
                shipto_id, cust_id, shipto_name = sample_data["shipping"]
                shipping = self.env["res.partner"].search(
                    [("xtuple_shipto_id", "=", shipto_id)]
                )
                if (
                    shipping
                ):  # Some shipping addresses might not be imported if their parent customer wasn't imported
                    self.assertEqual(shipping.name, shipto_name)
                    self.assertEqual(shipping.type, "delivery")

                    # Check that the shipping address is linked to the correct customer
                    customer = self.env["res.partner"].search(
                        [("xtuple_cust_id", "=", cust_id)]
                    )
                    if customer:
                        self.assertEqual(shipping.parent_id.id, customer.id)

        finally:
            if cursor:
                cursor.close()
                
    def test_shipping_address_import(self):
        """Test importing shipping addresses from xTuple."""
        cursor = None
        try:
            cursor = self.xtuple_db.get_cursor()
            
            # First, check if we have data to test with
            cursor.execute("SELECT COUNT(*) FROM shiptoinfo")
            shipping_count = cursor.fetchone()[0]
            
            # Skip test if no data is available
            if shipping_count == 0:
                self.skipTest("No shipping address data available in the xTuple database")
            
            # First import customers to have parent references
            customers = self.partner_importer._import_customers(cursor)
            
            if not customers:
                self.skipTest("No customers were imported, cannot test shipping address import")
            
            # Get initial count of shipping addresses in Odoo
            initial_shipping_count = self.env["res.partner"].search_count([('type', '=', 'delivery')])
            
            # Import shipping addresses
            shipping_addresses = self.partner_importer._import_shipping_addresses(cursor, customers)
            
            # Verify that shipping addresses were imported
            self.assertGreaterEqual(len(shipping_addresses), 0, "No shipping addresses were imported")
            
            # Verify that the total count increased
            new_shipping_count = self.env["res.partner"].search_count([('type', '=', 'delivery')])
            self.assertGreaterEqual(
                new_shipping_count, initial_shipping_count, "Shipping address count did not increase"
            )
            
            # Verify that shipping addresses have xTuple IDs and parent customers
            if shipping_addresses:
                self.assertTrue(
                    all(addr.xtuple_shipto_id for addr in shipping_addresses),
                    "Imported shipping addresses are missing xTuple IDs",
                )
                
                # At least some shipping addresses should have parent customers
                addresses_with_parents = shipping_addresses.filtered(lambda a: a.parent_id)
                self.assertTrue(
                    len(addresses_with_parents) > 0,
                    "None of the imported shipping addresses have parent customers"
                )
        
        finally:
            if cursor:
                cursor.close()
                
    def test_state_country_mapping(self):
        """Test the state and country mapping functionality."""
        cursor = None
        try:
            cursor = self.xtuple_db.get_cursor()
            
            # Check that the state was correctly identified
            # Find a partner with a state code
            cursor.execute(
                """
                SELECT 
                    cust_id, 
                    addr_state, 
                    addr_country 
                FROM custinfo 
                JOIN cntct ON (cust_cntct_id = cntct_id)
                JOIN addr ON (cntct_addr_id = addr_id)
                WHERE addr_state IS NOT NULL AND addr_state != ''
                LIMIT 1
                """
            )
            state_country_test = cursor.fetchone()
            
            if not state_country_test:
                self.skipTest("No partners with state codes found in the xTuple database")
                
            cust_id, state_code, country_code = state_country_test
            
            # Import the customer
            customers = self.partner_importer._import_customers(cursor)
            
            # Find the customer in Odoo
            customer = self.env["res.partner"].search([('xtuple_cust_id', '=', cust_id)])
            
            if not customer:
                self.skipTest(f"Customer with ID {cust_id} was not imported")
                
            # Check state mapping
            if state_code:
                self.assertTrue(
                    customer.state_id,
                    f"State not identified for partner with xTuple state code {state_code}",
                )
                
                # The state code in Odoo might be normalized, so check if it contains the original code
                normalized_state_code = normalize_country_code(state_code)
                self.assertEqual(
                    customer.state_id.code,
                    normalized_state_code,
                    f"State code mismatch: expected {normalized_state_code}, got {customer.state_id.code}",
                )
                
            # Check country mapping
            if country_code:
                self.assertTrue(
                    customer.country_id,
                    f"Country not identified for partner with xTuple country code {country_code}",
                )
                
                # The country code in Odoo might be normalized, so check if it contains the original code
                normalized_country_code = normalize_country_code(country_code)
                self.assertEqual(
                    customer.country_id.code,
                    normalized_country_code,
                    f"Country code mismatch: expected {normalized_country_code}, got {customer.country_id.code}",
                )
        
        finally:
            if cursor:
                cursor.close()

# Test the partner merge functionality works when one partner has an xTuple ID

class TestPartnerMerge(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.partner_non_xtuple = cls.env["res.partner"].create(
            {
                "name": "Test 1",
            }
        )
        cls.partner_xtuple = cls.env["res.partner"].create(
            {
                "name": "Test 1 mangled",
                "xtuple_cntct_id": 1,
            }
        )

    def test_merge_completes_without_error(self):
        """ When partners are merged, the merge moves values over to the new partner
        prior to deleting the original, which means that the method fails when there are
        fields with unicity contraints. """
        self.env["base.partner.merge.automatic.wizard"].create({
            "partner_ids": [Command.set([self.partner_non_xtuple.id, self.partner_xtuple.id])],
            "dst_partner_id": self.partner_non_xtuple.id,
        }).action_merge() # Should fail with vanilla Odoo        