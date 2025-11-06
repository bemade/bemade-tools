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
            cr.execute("SELECT count(*) FROM oitm")
            expected_product_count = cr.fetchall()[0][0] + initial_product_count
            initial_orderpoint_count = self.env[
                "stock.warehouse.orderpoint"
            ].search_count([])
            self.env["sap.product.importer"].import_products(cr)
            self.assertEqual(
                expected_product_count,
                self.env["product.product"].search_count(
                    [("active", "in", [True, False])]
                ),
            )
            # Make sure that weird names got cleared up
            overquoted_products = self.env["product.product"].search_count(
                [
                    ("name", "ilike", '"%""%"'),
                ]
            )
            self.assertEqual(overquoted_products, 0)
            cr.execute(
                "SELECT count(*) from oitm WHERE minlevel > 0 and " "validfor='Y'"
            )
            expected_orderpoint_count = cr.fetchall()[0][0] + initial_orderpoint_count
            orderpoint_count = self.env["stock.warehouse.orderpoint"].search_count([])
            self.assertEqual(orderpoint_count, expected_orderpoint_count)

            cr.execute("SELECT SUM(onhand) FROM oitm WHERE onhand<>0 and validfor='Y'")
            expected_stock = cr.fetchall()[0][0]
            stock = sum(
                [
                    q.quantity
                    for q in self.env["stock.quant"].search(
                        [("product_id.sap_item_code", "!=", False)]
                    )
                ]
            )
            self.assertEqual(expected_stock, stock)
