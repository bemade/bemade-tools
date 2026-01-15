from odoo.tests.common import TransactionCase
import os
import logging

_logger = logging.getLogger(__name__)


class TestXtupleDatabase(TransactionCase):
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

    def test_database_connection(self):
        """Test that we can connect to the xTuple database."""
        cursor = None
        connection = None
        try:
            # Get a cursor using our model's method
            cursor = self.xtuple_db.get_cursor()

            # Test that we can execute a simple query
            cursor.execute("SELECT 1")
            result = cursor.fetchone()
            self.assertEqual(result[0], 1)

            # Test that we can access the xTuple schema
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s",
                (self.xtuple_db.database_schema,),
            )
            count = cursor.fetchone()[0]
            self.assertGreater(count, 0, "No tables found in the xTuple schema")

        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    def test_compute_display_name(self):
        """Test that the display name is computed correctly."""
        self.assertEqual(
            self.xtuple_db.display_name,
            f"{self.xtuple_db.database_host}/{self.xtuple_db.database_name}",
        )
