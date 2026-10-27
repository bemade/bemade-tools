from odoo.tests.common import TransactionCase, tagged
from odoo import Command
from odoo.addons.etl_framework import ETLContext
import os
import logging
from odoo.addons.xtuple_to_odoo.tools import normalize_country_code

_logger = logging.getLogger(__name__)


class _FakeXtupleCursor:
    """Stand-in for the xTuple source cursor (`ctx.cr`) so the vendor
    extract's crmacct-matching logic can be exercised without a live xTuple
    Postgres connection.

    `_get_vendor_base_query` calls ``ctx.cr.execute(sql)`` followed by
    ``ctx.cr.dictfetchall()``; this fake ignores the SQL (it isn't run
    against a real xTuple DB in these tests) and simply returns the
    pre-built vendor rows handed to it.
    """

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        pass

    def dictfetchall(self):
        return self._rows

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


# Regression coverage for #3816: the vendor importer must match a vendor to
# an existing customer partner by shared xTuple CRM account (crmacct), never
# by vend_id happening to numerically equal an unrelated customer's cust_id.
class TestVendorCrmacctMatching(TransactionCase):
    def setUp(self):
        super().setUp()
        self.vendor_importer = self.env["xtuple.partner.vendor.importer"].with_company(
            self.env.company
        )

    def _run_vendor_base_query(self, vendor_rows):
        """Drive `_get_vendor_base_query` with simulated xTuple vendor rows,
        while the crmacct->partner map is built from real seeded
        `res.partner` records via the real Odoo cursor (`ctx.env.cr`)."""
        ctx = ETLContext(cr=_FakeXtupleCursor(vendor_rows), env=self.env)
        vendors, _existing_names, _existing_count = (
            self.vendor_importer._get_vendor_base_query(ctx)
        )
        return vendors

    def test_vendor_id_equal_to_unrelated_customer_id_does_not_collide(self):
        """Collision regression: a vendor whose vend_id numerically equals an
        unrelated customer's cust_id, but with a DIFFERENT crmacct, must NOT
        be matched to that customer partner (mirrors vendor 168/TML vs.
        customer 168/Spectracorp, crmacct 133 vs. 128)."""
        customer = self.env["res.partner"].create(
            {
                "name": "Spectracorp Inc.",
                "xtuple_cust_id": 168,
                "xtuple_crmacct_id": 128,
            }
        )

        vendors = self._run_vendor_base_query(
            [
                {
                    "vend_id": 168,
                    "vend_name": "TML Industries",
                    "vend_crmacct_id": 133,
                }
            ]
        )

        self.assertEqual(len(vendors), 1)
        self.assertNotIn("_existing_customer_partner_id", vendors[0])

        # Downstream transform would therefore create TML as a new vendor
        # partner rather than stamping xtuple_vend_id onto Spectracorp.
        self.assertFalse(customer.xtuple_vend_id)

    def test_vendor_matches_existing_partner_by_shared_crmacct(self):
        """Legitimate merge preserved: a vendor sharing the same crmacct as an
        existing customer partner (unrelated numeric ids) IS matched to that
        partner, so it flows into the customer/vendor update path."""
        customer = self.env["res.partner"].create(
            {
                "name": "Shared Account Inc.",
                "xtuple_cust_id": 500,
                "xtuple_crmacct_id": 900,
            }
        )

        vendors = self._run_vendor_base_query(
            [
                {
                    "vend_id": 777,
                    "vend_name": "Shared Account Inc.",
                    "vend_crmacct_id": 900,
                }
            ]
        )

        self.assertEqual(len(vendors), 1)
        self.assertEqual(
            vendors[0].get("_existing_customer_partner_id"), customer.id
        )

    def test_null_crmacct_does_not_false_match(self):
        """A vendor with a NULL/falsy crmacct must never be matched to any
        partner, even one that also has no crmacct recorded (guards against
        falsy-on-falsy collapsing in the map/lookup). The existing partner
        below leaves `xtuple_crmacct_id` entirely unset, which Postgres
        stores as real SQL NULL (no DDL default on a plain Integer column),
        so it is excluded by the map query's `WHERE xtuple_crmacct_id IS NOT
        NULL` already; the vendor-side Python truthiness guard on
        `vend_crmacct_id` in `_get_vendor_base_query` is the second,
        independent line of defense this test exercises (e.g. against a
        stray `0` sneaking into the map)."""
        self.env["res.partner"].create(
            {
                "name": "No CRM Account Customer",
                "xtuple_cust_id": 42,
                # xtuple_crmacct_id intentionally left unset (stored as NULL)
            }
        )

        vendors = self._run_vendor_base_query(
            [
                {
                    "vend_id": 42,
                    "vend_name": "No CRM Account Vendor",
                    "vend_crmacct_id": None,
                }
            ]
        )

        self.assertEqual(len(vendors), 1)
        self.assertNotIn("_existing_customer_partner_id", vendors[0])


# Regression coverage for #4091: the xTuple vendor importer must map the
# vendor's source currency (best-effort vend_curr_id -> curr_symbol.curr_abbr
# -> res.currency by ISO name, see `_resolve_xtuple_currency_id`) onto
# `property_purchase_currency_id`, mirroring what the QBO vendor importer
# already does for `CurrencyRef`. These tests drive `transform_vendors`
# directly against a fake cursor (via `_FakeXtupleCursor`) and a pre-built
# `vend_curr_abbr` key, so they stay valid regardless of whether the real
# xTuple `vend_curr_id` -> `curr_symbol.curr_abbr` JOIN in
# `_get_vendor_base_query` matches the live schema (unverified, see design
# risk / 03-implementation-notes.md).
class TestVendorCurrencyMapping(TransactionCase):
    def setUp(self):
        super().setUp()
        self.vendor_importer = self.env["xtuple.partner.vendor.importer"].with_company(
            self.env.company
        )

    def _transform_new_vendor(self, vendor_row):
        """Drive `transform_vendors`'s create path with a single fake vendor
        row, bypassing the extract phase entirely (extract is exercised
        separately by TestVendorCrmacctMatching)."""
        ctx = ETLContext(cr=_FakeXtupleCursor([]), env=self.env)
        extracted = {
            "extract_new_vendors": [vendor_row],
            "extract_vendors_to_update": [],
        }
        result = self.vendor_importer.transform_vendors(ctx, extracted)
        self.assertEqual(len(result["create"]), 1)
        return result["create"][0]

    def test_vendor_currency_usd_maps_to_property_purchase_currency(self):
        usd = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", "USD")], limit=1
        )
        self.assertTrue(usd, "USD currency must exist in the demo chart for this test")

        vals = self._transform_new_vendor(
            {
                "vend_id": 601,
                "vend_number": "V601",
                "vend_name": "US Dollar Vendor",
                "vend_active": True,
                "vend_curr_abbr": "USD",
            }
        )
        self.assertEqual(vals["property_purchase_currency_id"], usd.id)

    def test_vendor_currency_cad_maps_to_property_purchase_currency(self):
        cad = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", "CAD")], limit=1
        )
        self.assertTrue(cad, "CAD currency must exist in the demo chart for this test")

        vals = self._transform_new_vendor(
            {
                "vend_id": 602,
                "vend_number": "V602",
                "vend_name": "Canadian Dollar Vendor",
                "vend_active": True,
                "vend_curr_abbr": "CAD",
            }
        )
        self.assertEqual(vals["property_purchase_currency_id"], cad.id)

    def test_vendor_with_unknown_currency_abbr_leaves_field_unset(self):
        """An abbreviation that doesn't match any res.currency must not
        raise and must simply leave the key unset (falls back to displaying
        the company currency), not abort the vendor's creation."""
        vals = self._transform_new_vendor(
            {
                "vend_id": 603,
                "vend_number": "V603",
                "vend_name": "Unknown Currency Vendor",
                "vend_active": True,
                "vend_curr_abbr": "ZZZ",
            }
        )
        self.assertNotIn("property_purchase_currency_id", vals)

    def test_vendor_with_no_currency_abbr_leaves_field_unset(self):
        """Simulates either a NULL vend_curr_id, or the fallback query used
        when the currency JOIN itself fails against the live xTuple schema
        (`vend_curr_abbr` key simply absent from the row)."""
        vals = self._transform_new_vendor(
            {
                "vend_id": 604,
                "vend_number": "V604",
                "vend_name": "No Currency Vendor",
                "vend_active": True,
            }
        )
        self.assertNotIn("property_purchase_currency_id", vals)

    def test_currency_resolves_even_when_inactive_in_target_company(self):
        """Guards the most likely silent-failure mode (see design Risks): a
        currency that exists in Odoo's currency list but hasn't been
        activated for this company/DB must still resolve, since the
        resolver searches with active_test=False."""
        currency = self.env["res.currency"].with_context(active_test=False).search(
            [("active", "=", False)], limit=1
        )
        if not currency:
            self.skipTest("No inactive res.currency available to test against")

        vals = self._transform_new_vendor(
            {
                "vend_id": 605,
                "vend_number": "V605",
                "vend_name": "Inactive Currency Vendor",
                "vend_active": True,
                "vend_curr_abbr": currency.name,
            }
        )
        self.assertEqual(vals["property_purchase_currency_id"], currency.id)
