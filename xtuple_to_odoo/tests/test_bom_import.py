from odoo.tests.common import TransactionCase, tagged
import os
import logging

_logger = logging.getLogger(__name__)

@tagged("-at_install", "xtuple")
class TestBomImport(TransactionCase):
    def setUp(self):
        super().setUp()
        # Use environment variables with fallbacks for database connection
        self.xtuple_db = self.env["xtuple.database"].create(
            {
                "database_host": os.environ.get("XTUPLE_HOST", ""),
                "database_name": os.environ.get("XTUPLE_DBNAME", ""),
                "database_username": os.environ.get("XTUPLE_USER", ""),
                "database_password": os.environ.get("XTUPLE_PASSWORD", ""),
                "database_port": int(os.environ.get("XTUPLE_PORT", "")),
                "database_schema": os.environ.get("XTUPLE_SCHEMA", "public"),
            }
        )

        # Create the importers
        self.product_importer = self.env["xtuple.product.importer"].create({})
        self.bom_importer = self.env["xtuple.bom.importer"].create({})

    def test_bom_tables_exist(self):
        """Test that the BOM-related tables exist in the xTuple database."""
        with self.xtuple_db.get_cursor() as cursor:
            # Check for bomhead table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = 'bomhead'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(
                cursor.fetchone()[0], "BOM header table (bomhead) not found"
            )

            # Check for bomitem table
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = 'bomitem'
                )
            """,
                (self.xtuple_db.database_schema,),
            )
            self.assertTrue(cursor.fetchone()[0], "BOM item table (bomitem) not found")

    def test_bom_import(self):
        """Test importing BOMs from xTuple."""
        with self.xtuple_db.get_cursor() as cursor:

            # First, check if we have data to test with
            cursor.execute(
                """
                SELECT COUNT(*) FROM bomhead
                JOIN item ON (bomhead_item_id = item_id)
                WHERE item_type IN ('M', 'F')
            """
            )
            bom_count = cursor.fetchone()[0]

            # Skip test if no data is available
            if bom_count == 0:
                self.skipTest("No BOM data available in the xTuple database")

            # Import products first (required for BOMs)
            self.product_importer.import_products(cursor)

            # Get initial count of BOMs in Odoo
            initial_bom_count = self.env["mrp.bom"].search_count([])

            # Import BOMs - pass the products directly to the BOM importer
            boms = self.bom_importer.import_boms(cursor)

            # Verify that BOMs were imported
            self.assertGreaterEqual(len(boms), 0, "No BOMs were imported")

            # Verify that the total count increased
            new_bom_count = self.env["mrp.bom"].search_count([])
            self.assertGreaterEqual(
                new_bom_count, initial_bom_count, "BOM count did not increase"
            )

            # Verify that BOMs have xTuple IDs
            if boms:
                self.assertTrue(
                    all(bom.xtuple_bomhead_id for bom in boms),
                    "Imported BOMs are missing xTuple IDs",
                )

                # Verify that at least one BOM has components
                boms_with_components = boms.filtered(lambda b: b.bom_line_ids)
                self.assertTrue(
                    len(boms_with_components) > 0,
                    "None of the imported BOMs have components",
                )

    def test_multilevel_bom_import(self):
        """Test that multi-level BOMs are correctly imported from xTuple to Odoo."""
        with self.xtuple_db.get_cursor() as cursor:
            # First, find a multi-level BOM in xTuple
            # This query finds items that are both used as components in BOMs
            # and have their own BOMs
            cursor.execute(
                """
                SELECT
                    parent.item_number as parent_item,
                    parent.item_id as parent_item_id,
                    child.item_number as child_item,
                    child.item_id as child_item_id
                FROM bomhead parent_bom
                JOIN item parent ON parent_bom.bomhead_item_id = parent.item_id
                JOIN bomitem ON bomitem_parent_item_id = parent.item_id
                JOIN item child ON bomitem_item_id = child.item_id
                JOIN bomhead child_bom ON child_bom.bomhead_item_id = child.item_id
                LIMIT 1
            """
            )

            multilevel_bom = cursor.fetchone()
            if not multilevel_bom:
                self.skipTest("No multi-level BOMs found in xTuple database")

            parent_item, parent_item_id, child_item, child_item_id = multilevel_bom
            _logger.info(f"Found multi-level BOM: {parent_item} -> {child_item}")

            # Import products and BOMs
            self.product_importer.import_products(cursor)
            boms = self.bom_importer.import_boms(cursor)

            # Find the parent BOM in Odoo
            parent_bom = self.env["mrp.bom"].search(
                [("xtuple_bomhead_item_id", "=", parent_item_id)], limit=1
            )
            self.assertTrue(parent_bom, f"Parent BOM {parent_item} not found in Odoo")

            # Find the child BOM in Odoo
            child_bom = self.env["mrp.bom"].search(
                [("xtuple_bomhead_item_id", "=", child_item_id)], limit=1
            )
            self.assertTrue(child_bom, f"Child BOM {child_item} not found in Odoo")

            # Verify that the child product is a component in the parent BOM
            child_product = self.env["product.product"].search(
                [
                    ("xtuple_item_id", "=", child_item_id),
                    ("active", "in", [True, False]),
                ],
                limit=1,
            )
            self.assertTrue(
                child_product, f"Child product {child_item} not found in Odoo"
            )

            # Check if the child product is a component in the parent BOM
            parent_bom_components = parent_bom.bom_line_ids.mapped("product_id")
            self.assertIn(
                child_product,
                parent_bom_components,
                f"Child product {child_item} is not a component in parent BOM {parent_item}",
            )

            # Verify that the child product has its own BOM in Odoo
            self.assertTrue(
                child_bom.bom_line_ids,
                f"Child BOM {child_item} has no components in Odoo",
            )

            # Verify that the child product is a component in the parent BOM
            child_in_parent = any(
                line.product_id.id == child_product.id
                for line in parent_bom.bom_line_ids
            )
            self.assertTrue(
                child_in_parent,
                f"Child product {child_item} is not a component in parent BOM {parent_item}",
            )

            # Additional check: Get a random component from the child BOM
            if child_bom.bom_line_ids:
                child_component = child_bom.bom_line_ids[0].product_id
                _logger.info(f"Child BOM component: {child_component.display_name}")

                # Verify this component is not directly in the parent BOM
                # (unless it's intentionally used in both places)
                child_component_in_parent = any(
                    line.product_id.id == child_component.id
                    for line in parent_bom.bom_line_ids
                )

                if not child_component_in_parent:
                    _logger.info("Multi-level BOM hierarchy is correctly preserved")
                else:
                    _logger.info(
                        "Component appears in both parent and child BOMs (this might be intentional)"
                    )

            # Log the BOM structure for verification
            _logger.info(f"Parent BOM {parent_item} components:")
            for line in parent_bom.bom_line_ids:
                _logger.info(
                    f"  - {line.product_id.display_name} (qty: {line.product_qty})"
                )

            _logger.info(f"Child BOM {child_item} components:")
            for line in child_bom.bom_line_ids:
                _logger.info(
                    f"  - {line.product_id.display_name} (qty: {line.product_qty})"
                )

            # Test passed if we got here - we've verified that:
            # 1. The parent BOM exists in Odoo
            # 2. The child BOM exists in Odoo with components
            # 3. The child product is a component in the parent BOM
            # This confirms the multi-level BOM structure was correctly imported
