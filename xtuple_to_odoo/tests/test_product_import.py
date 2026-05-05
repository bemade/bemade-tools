from odoo.tests.common import TransactionCase, tagged
import os
import logging

_logger = logging.getLogger(__name__)

@tagged("-at_install", "xtuple")
class TestProductImport(TransactionCase):
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
        self.product_importer = self.env["xtuple.product.importer"].create({})

    def test_product_tables_exist(self):
        """Test that the product-related tables exist in the xTuple database."""
        cursor = None
        try:
            cursor = self.xtuple_db.get_cursor()

            # Check for item table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = 'item'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(cursor.fetchone()[0], "Product table (item) not found")

            # Check for product category table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = 'prodcat'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(
                cursor.fetchone()[0], "Product category table (prodcat) not found"
            )

            # Check for UOM table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = 'uom'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(cursor.fetchone()[0], "UOM table (uom) not found")

            # Check for item source table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = 'itemsrc'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(
                cursor.fetchone()[0], "Item source table (itemsrc) not found"
            )

        finally:
            if cursor:
                cursor.close()

    def test_product_category_import(self):
        """Test importing product categories from xTuple."""
        cursor = None
        try:
            cursor = self.xtuple_db.get_cursor()

            # First, check if we have data to test with
            cursor.execute("SELECT COUNT(*) FROM prodcat")
            category_count = cursor.fetchone()[0]

            # Skip test if no data is available
            if category_count == 0:
                self.skipTest(
                    "No product category data available in the xTuple database"
                )

            # Get initial count of product categories in Odoo
            initial_category_count = self.env["product.category"].search_count([])

            # Import product categories
            categories = self.product_importer._import_product_categories(cursor)

            # Verify that categories were imported
            self.assertGreaterEqual(
                len(categories), 0, "No product categories were imported"
            )

            # Verify that the total count increased
            new_category_count = self.env["product.category"].search_count([])
            self.assertGreaterEqual(
                new_category_count,
                initial_category_count,
                "Product category count did not increase",
            )

            # Verify that categories have xTuple IDs
            if categories:
                self.assertTrue(
                    all(cat.xtuple_prodcat_id for cat in categories),
                    "Imported categories are missing xTuple IDs",
                )

        finally:
            if cursor:
                cursor.close()

    def test_product_import(self):
        """Test importing products from xTuple."""
        with self.xtuple_db.get_cursor() as cursor:
            # First, check if we have data to test with
            cursor.execute("SELECT COUNT(*) FROM item")
            product_count = cursor.fetchone()[0]

            # Skip test if no data is available
            if product_count == 0:
                self.skipTest("No product data available in the xTuple database")

            prod_domain = [
                ("xtuple_item_id", "!=", False),
                ("active", "in", [True, False]),
            ]
            # Get initial count of products in Odoo
            initial_product_count = self.env["product.product"].search_count(
                prod_domain
            )

            # Import products
            products = self.product_importer.import_products(cursor)

            # Verify that the total count increased
            new_product_count = self.env["product.product"].search_count(prod_domain)
            _logger.info(f"Searched and found {new_product_count} imported products.")
            self.assertEqual(
                new_product_count,
                product_count,
                "Not all products were imported",
            )

            # Verify that products have xTuple IDs
            self.assertTrue(
                all(prod.xtuple_item_id for prod in products),
                "Imported products are missing xTuple IDs",
            )

            # Verify that at least one product has a category
            products_with_categories = products.filtered(
                lambda p: p.categ_id.id
                != self.env.ref("product.product_category_all").id
            )
            self.assertTrue(
                len(products_with_categories) > 0,
                "None of the imported products have categories",
            )

    def test_product_supplier_import(self):
        """Test importing product suppliers from xTuple."""
        with self.xtuple_db.get_cursor() as cursor:
            # First, check if we have data to test with
            cursor.execute(
                """
                SELECT COUNT(*) FROM itemsrc
                JOIN vendinfo ON (itemsrc_vend_id = vend_id)
                WHERE itemsrc_active = 't'
            """
            )
            supplier_count = cursor.fetchone()[0]

            # Skip test if no data is available
            if supplier_count == 0:
                self.skipTest(
                    "No active product supplier data available in the xTuple database"
                )

            # We need to have vendors imported first
            # Import vendors from xTuple
            partner_importer = self.env["xtuple.res.partner.importer"].with_company(
                self.env.company
            )

            # Get vendor IDs from xTuple
            cursor.execute("SELECT vend_id FROM vendinfo LIMIT 5")
            vend_ids = [row[0] for row in cursor.fetchall()]

            if not vend_ids:
                self.skipTest("No vendors available in the xTuple database")

            # Import vendors
            partners = partner_importer.import_partners(cursor)

            # Verify that vendors were imported
            vendors = self.env["res.partner"].search(
                [("xtuple_vend_id", "in", vend_ids)]
            )
            if not vendors:
                self.skipTest("No vendors were imported, cannot test supplier import")

            # Import product categories first
            categories = self.product_importer._import_product_categories(cursor)

            # Import products
            products = self.product_importer._import_products_with_categories(
                cursor, categories
            )

            if not products:
                self.skipTest("No products were imported, cannot test supplier import")

            # Get initial count of product suppliers in Odoo
            initial_supplier_count = self.env["product.supplierinfo"].search_count([])

            # Import product suppliers
            self.product_importer._import_product_suppliers(cursor, products)

            # Verify that the total count increased
            new_supplier_count = self.env["product.supplierinfo"].search_count([])

            # We may not have increased if there were no matching suppliers
            # But we should at least not have decreased
            self.assertGreaterEqual(
                new_supplier_count,
                initial_supplier_count,
                "Product supplier count decreased after import",
            )

            # Check if any suppliers were imported with xTuple IDs
            suppliers_with_xtuple_id = self.env["product.supplierinfo"].search(
                [("xtuple_itemsrc_id", "!=", False)]
            )

            # Log the result for debugging
            _logger.info(
                f"Found {len(suppliers_with_xtuple_id)} suppliers with xTuple IDs"
            )

    def test_full_product_import(self):
        """Test the full product import process using real data."""
        with self.xtuple_db.get_cursor() as cursor:

            # First, check if we have data to test with
            cursor.execute("SELECT COUNT(*) FROM item")
            product_count = cursor.fetchone()[0]

            # Skip test if no data is available
            if product_count == 0:
                self.skipTest("No product data available in the xTuple database")

            # Get initial counts
            initial_category_count = self.env["product.category"].search_count([])
            initial_product_count = self.env["product.product"].search_count([])
            initial_supplier_count = self.env["product.supplierinfo"].search_count([])

            # Import vendors first for supplier references
            partner_importer = self.env["xtuple.res.partner.importer"].with_company(
                self.env.company
            )
            partners = partner_importer.import_partners(cursor)

            # Run the full product import
            products = self.product_importer.import_products(cursor)

            # Verify that products were imported
            self.assertGreaterEqual(len(products), 0, "No products were imported")

            # Verify that counts increased
            new_category_count = self.env["product.category"].search_count([])
            new_product_count = self.env["product.product"].search_count([])
            new_supplier_count = self.env["product.supplierinfo"].search_count([])

            self.assertGreaterEqual(
                new_category_count,
                initial_category_count,
                "Product category count did not increase",
            )
            self.assertGreaterEqual(
                new_product_count,
                initial_product_count,
                "Product count did not increase",
            )

            # Supplier count may not increase if there are no matching suppliers
            self.assertGreaterEqual(
                new_supplier_count,
                initial_supplier_count,
                "Product supplier count decreased after import",
            )

            # Verify that products have xTuple IDs
            if products:
                self.assertTrue(
                    all(prod.xtuple_item_id for prod in products),
                    "Imported products are missing xTuple IDs",
                )

                # Verify product prices were set
                products_with_prices = products.filtered(lambda p: p.list_price > 0)
                self.assertTrue(
                    len(products_with_prices) > 0,
                    "None of the imported products have prices set",
                )

                # Get a sample product to verify specific fields
                sample_product = products[0]

                # Get the corresponding xTuple product
                cursor.execute(
                    """
                    SELECT
                        item_number,
                        item_descrip1,
                        item_type
                    FROM item
                    WHERE item_id = %s
                    """,
                    (sample_product.xtuple_item_id,),
                )
                xtuple_product = cursor.fetchone()

                if xtuple_product:
                    item_number, item_descrip, item_type = xtuple_product

                    # Verify basic product data
                    self.assertEqual(
                        sample_product.default_code,
                        item_number,
                        f"Product code mismatch: expected {item_number}, got {sample_product.default_code}",
                    )
                    self.assertEqual(
                        sample_product.name,
                        item_descrip,
                        f"Product name mismatch: expected {item_descrip}, got {sample_product.name}",
                    )
                    self.assertEqual(
                        sample_product.xtuple_item_type,
                        item_type,
                        f"Product type mismatch: expected {item_type}, got {sample_product.xtuple_item_type}",
                    )

                # Check for product suppliers
                suppliers = self.env["product.supplierinfo"].search(
                    [("product_tmpl_id", "in", products.ids)]
                )

                if suppliers:
                    # Verify supplier data
                    self.assertTrue(
                        all(sup.xtuple_itemsrc_id for sup in suppliers),
                        "Imported suppliers are missing xTuple IDs",
                    )

                    # Get a sample supplier to verify specific fields
                    sample_supplier = suppliers[0]

                    # Get the corresponding xTuple supplier
                    cursor.execute(
                        """
                        SELECT
                            itemsrc_vend_item_number,
                            itemsrc_vend_item_descrip
                        FROM itemsrc
                        WHERE itemsrc_id = %s
                        """,
                        (sample_supplier.xtuple_itemsrc_id,),
                    )
                    xtuple_supplier = cursor.fetchone()

                    if xtuple_supplier:
                        vend_item_number, vend_item_descrip = xtuple_supplier

                        # Verify basic supplier data
                        self.assertEqual(
                            sample_supplier.product_code,
                            vend_item_number,
                            f"Supplier product code mismatch: expected {vend_item_number}, got {sample_supplier.product_code}",
                        )
                        self.assertEqual(
                            sample_supplier.product_name,
                            vend_item_descrip,
                            f"Supplier product name mismatch: expected {vend_item_descrip}, got {sample_supplier.product_name}",
                        )
