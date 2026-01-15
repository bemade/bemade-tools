from odoo.tests.common import TransactionCase
import os
import logging

_logger = logging.getLogger(__name__)


class TestMultilevelBomImport(TransactionCase):
    def setUp(self):
        super().setUp()
        # Use environment variables with fallbacks for database connection
        self.xtuple_db = self.env["xtuple.database"].create(
            {
                "database_host": os.environ.get("XTUPLE_HOST", "192.168.3.10"),
                "database_name": os.environ.get("XTUPLE_DBNAME", "vera_production"),
                "database_username": os.environ.get("XTUPLE_USER", "postgres"),
                "database_password": os.environ.get("XTUPLE_PASSWORD", "q2w3e4"),
                "database_port": int(os.environ.get("XTUPLE_PORT", "5432")),
                "database_schema": os.environ.get("XTUPLE_SCHEMA", "public"),
            }
        )

        # Create the importers
        self.product_importer = self.env["xtuple.product.importer"].create({})
        self.bom_importer = self.env["xtuple.bom.importer"].create({})
