""" Run tests on an actual SAP B1 database running on a local postgresql server. """

from odoo.addons.sap_b1_to_odoo.tests.test_common import TestSapImportCommon


class TestSapPartnerImport(TestSapImportCommon):
    def test_import_partners(self):
        with self.database.get_cursor() as cr:
            self.env["sap.res.partner.importer"].import_partners(cr)
            imported_partner_count = self.env["res.partner"].search_count(
                [
                    "|",
                    ("sap_card_code", "!=", False),
                    ("sap_cntct_code", "!=", False),
                    ("active", "in", [True, False]),
                ]
            )
            cr.execute(
                f"SELECT count(*) from OCRD "
                f"WHERE cardname is not null and cardname <> ''"
            )
            sap_partners_count = cr.fetchall()[0][0]
            cr.execute(
                f"select count(*) from OCPR WHERE name is not null and name <> '' "
            )
            sap_partners_count += cr.fetchall()[0][0]
            self.assertEqual(imported_partner_count, sap_partners_count)

    def test_parent_child_count_matches(self):
        with self.database.get_cursor() as cr:
            self.env["sap.res.partner.importer"].import_partners(cr)
            cr.execute(
                "SELECT count(*) from OCRD WHERE fathercard is not null and fathercard <> '' "
                "AND cardname is not null and cardname <> ''"
            )
            child_count_sap = cr.fetchall()[0][0]
            cr.execute(
                "SELECT count(*) from OCPR WHERE name is not null and name <> '' "
                "AND cardcode is not null and cardcode <> ''"
            )
            child_count_sap += cr.fetchall()[0][0]
            child_count_odoo = self.env["res.partner"].search_count(
                [
                    "|",
                    ("sap_card_code", "!=", False),
                    ("sap_cntct_code", "!=", False),
                    ("parent_id", "!=", False),
                    ("active", "in", [True, False]),
                ]
            )
            self.assertEqual(child_count_sap, child_count_odoo)

    def test_import_all_runs_to_completion(self):
        self.database.action_import_all()
