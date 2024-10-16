from .test_common import TestSapImportCommon


class TestSaleOrderImport(TestSapImportCommon):
    def test_import_sale_orders(self):
        with self.database.get_cursor() as cr:
            initial_order_count = self.env["sale.order"].search_count([])
            initial_order_line_count = self.env["sale.order.line"].search_count([])
            cr.execute("SELECT count(*) FROM ordr")
            sap_order_count = cr.fetchall()[0][0]
            cr.execute("SELECT count(*) FROM rdr1")
            sap_order_line_count = cr.fetchall()[0][0]
            self.env["sap.res.partner.importer"].import_partners(cr)
            self.env["sap.product.importer"].import_products(cr)
            self.env["sap.bom.importer"].import_boms(cr)
            self.env["sap.sale.order.importer"].import_sales_orders(cr)

            imported_order_count = (
                self.env["sale.order"].search_count([]) - initial_order_count
            )
            imported_lines_count = (
                self.env["sale.order.line"].search_count([]) - initial_order_line_count
            )

            self.assertEqual(imported_order_count, sap_order_count)
            self.assertEqual(imported_lines_count, sap_order_line_count)
