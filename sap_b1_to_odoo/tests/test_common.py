""" Run tests on an actual SAP B1 database running on a local postgresql server. """

from odoo.tests import TransactionCase


class TestSapImportCommon(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.database = cls.env["sap.database"].create(
            {
                "database_host": "localhost",
                "database_port": 5433,
                "database_name": "pneuprod",
                "database_schema": "dbo",
                "database_username": "postgres",
                "database_password": "pgpassword",
            }
        )
