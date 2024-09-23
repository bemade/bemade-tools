""" Run tests on an actual SAP B1 database running on a local postgresql server. """

from odoo.addons.sap_b1_to_odoo.tests.test_common import TestSapImportCommon


class TestSapBomImport(TestSapImportCommon):
    def test_import_boms(self):
        with self.database.get_cursor() as cr:
            self.env["sap.product.importer"].import_products(cr)
            self.env["sap.bom.importer"].import_boms(cr)

            imported_boms_count = self.env["mrp.bom"].search_count(
                [
                    ("sap_code", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
            cr.execute(f"SELECT count(*) from oitt")
            sap_bom_count = cr.fetchall()[0][0]
            self.assertEqual(sap_bom_count, imported_boms_count)
            cr.execute(f"SELECT count(*) from itt1")
            sap_bom_line_count = cr.fetchall()[0][0]
            import_bom_lines_count = self.env["mrp.bom.line"].search_count(
                [
                    ("bom_id.sap_code", "!=", False),
                ]
            )
            self.assertEqual(sap_bom_line_count, import_bom_lines_count)
