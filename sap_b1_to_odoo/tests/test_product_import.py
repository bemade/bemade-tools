from odoo.tests import TransactionCase
from odoo.addons.sap_b1_to_odoo.tests.test_common import TestSapImportCommon


class TestSapProductImport(TestSapImportCommon):
    def test_import_product_categories(self):
        with self.database.get_cursor() as cr:
            initial_categ_count = self.env["product.category"].search_count([])
            cr.execute(
                "SELECT count(*) FROM oitb where itmsgrpnam <> '' "
                "and itmsgrpnam is not null"
            )
            expected_categ_count = cr.fetchall()[0][0] + initial_categ_count
            self.env["sap.product.importer"]._import_oitb(cr)
            self.assertEqual(
                expected_categ_count, self.env["product.category"].search_count([])
            )

    def test_import_products(self):
        with self.database.get_cursor() as cr:
            initial_product_count = self.env["product.product"].search_count(
                [("active", "in", [True, False])]
            )
            cr.execute(
                "SELECT count(*) FROM oitm WHERE frgnname <> '' and frgnname is not null"
            )
            expected_product_count = cr.fetchall()[0][0] + initial_product_count
            self.env["sap.product.importer"].import_products(cr)
            self.assertEqual(
                expected_product_count,
                self.env["product.product"].search_count(
                    [("active", "in", [True, False])]
                ),
            )
