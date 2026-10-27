from odoo.tests.common import TransactionCase, tagged
import os
import logging

_logger = logging.getLogger(__name__)

@tagged("-at_install", "xtuple")
class TestMultilevelBomImport(TransactionCase):
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
