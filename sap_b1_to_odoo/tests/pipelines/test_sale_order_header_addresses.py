#
#    Bemade Inc.
#
#    Copyright (C) 2026 Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Sales order ship-to and bill-to come from SAP's own address codes.

The header importer fuzzy-matched ORDR.Address2 text against the customer's
addresses and fell back to the customer, so migrated orders lost SAP's
ship-to. ORDR.ShipToCode / PayToCode name the CRD1 row exactly; the address
importer keeps that row's (CardCode, LineNum) on the Odoo address.

Acceptance criteria:

1. (test_extract_adds_crd1_line_of_ship_and_bill_codes) The header extract
   brings, per order, the CRD1 LineNum its ShipToCode (type S) and PayToCode
   (type B) name, matched case- and space-insensitively as SAP does.
2. (test_addresses_resolve_from_sap_codes) The order's partner_shipping_id is
   the delivery address with that (sap_parent_card, sap_address_linenum), and
   partner_invoice_id the invoice address likewise, whatever the text says.
3. (test_customer_is_the_sap_contact_person) partner_id is the SAP contact
   person (CntctCode) even once it is linked to its company; the company is
   its commercial_partner_id.
4. (test_missing_address_warns_and_falls_back) A ShipToCode or PayToCode whose
   CRD1 row no longer exists (deleted in SAP) does not fail the import: the
   address falls back to the customer and a report warning names the order.
   An order with no code at all falls back silently.
"""

import datetime
from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

CARD = "C900100"


@tagged("-at_install", "post_install")
class TestSaleOrderHeaderAddresses(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env["res.partner"]
        cls.company = Partner.create({
            "name": "Northwind Steel", "is_company": True, "sap_card_code": CARD,
        })
        cls.contact = Partner.create({
            "name": "Jane Doe", "type": "contact", "parent_id": cls.company.id,
            "sap_parent_card": CARD, "sap_cntct_code": 900101,
        })
        cls.ship_main = Partner.create({
            "name": "MAIN PLANT", "type": "delivery", "parent_id": cls.company.id,
            "sap_parent_card": CARD, "sap_address_linenum": 0,
            "street": "1 Main St", "city": "Springfield", "zip": "62701",
        })
        cls.ship_riverside = Partner.create({
            "name": "RIVERSIDE", "type": "delivery", "parent_id": cls.company.id,
            "sap_parent_card": CARD, "sap_address_linenum": 1,
            "street": "9 Mill Rd", "city": "Riverside", "zip": "92501",
        })
        cls.bill = Partner.create({
            "name": "ACCOUNTS PAYABLE", "type": "invoice", "parent_id": cls.company.id,
            "sap_parent_card": CARD, "sap_address_linenum": 0,
            "street": "PO Box 1", "city": "Madison", "zip": "53703",
        })
        cls.importer = cls.env["sale.order.header.importer"]

    def setUp(self):
        super().setUp()
        cr = self.env.cr
        # The SAP source tables the extract reads, as temp tables in the test
        # transaction (the importer reads SAP through ctx.cr).
        cr.execute("""
            CREATE TEMP TABLE crd1 (cardcode text, address text, adrestype text,
                                    linenum int) ON COMMIT DROP;
            CREATE TEMP TABLE ordr (docnum int, docentry int, atcentry int,
                                    cardcode text, cntctcode int, shiptocode text,
                                    paytocode text, address text, address2 text,
                                    slpcode int, groupnum int, doccur text,
                                    docdate timestamp, docduedate timestamp,
                                    numatcard text, partsupply text, trnspcode int,
                                    docstatus text) ON COMMIT DROP;
            -- LineNum is only unique per (CardCode, AdresType): the bill-to
            -- row 0 and the ship-to row 0 are different addresses.
            INSERT INTO crd1 VALUES ('c900100', 'Main Plant', 'S', 0),
                                    ('C900100', 'RIVERSIDE ', 'S', 1),
                                    ('C900100', 'ACCOUNTS PAYABLE', 'B', 0);
        """)
        self.ctx = MagicMock()
        self.ctx.cr = cr
        self.ctx.env = self.env

    def _order(self, docnum, shiptocode=None, paytocode=None, cntctcode=None,
               address2="MAIN PLANT\r\n1 Main St\r\nSpringfield IL 62701"):
        self.env.cr.execute(
            """INSERT INTO ordr VALUES (%s, %s, NULL, %s, %s, %s, %s, 'BILL BLOCK', %s,
                                        NULL, NULL, 'USD', %s, %s, 'PO', 'Y', NULL, 'O')""",
            (docnum, docnum, CARD.lower(), cntctcode, shiptocode, paytocode, address2,
             datetime.datetime(2026, 7, 17), datetime.datetime(2026, 7, 31)),
        )

    def _run(self):
        extracted = self.importer.extract_headers(self.ctx)
        vals = self.importer.transform_headers(self.ctx, {"extract_headers": extracted})
        return extracted, {v["sap_docnum"]: v for v in vals}

    # 1 ------------------------------------------------------------------
    def test_extract_adds_crd1_line_of_ship_and_bill_codes(self):
        self._order(1, shiptocode="riverside", paytocode="Accounts Payable")
        self._order(2, shiptocode="GONE")
        extracted, _ = self._run()
        rows = {h["docnum"]: h for h in extracted.records}
        self.assertEqual((rows[1]["etl_ship_linenum"], rows[1]["etl_bill_linenum"]), (1, 0))
        self.assertIsNone(rows[2]["etl_ship_linenum"])
        self.assertIsNone(rows[2]["etl_bill_linenum"])

    # 2 ------------------------------------------------------------------
    def test_addresses_resolve_from_sap_codes(self):
        # The printed text names MAIN PLANT; ShipToCode names RIVERSIDE, which wins.
        self._order(1, shiptocode="RIVERSIDE", paytocode="ACCOUNTS PAYABLE")
        _, vals = self._run()
        self.assertEqual(vals[1]["partner_shipping_id"], self.ship_riverside.id)
        self.assertEqual(vals[1]["partner_invoice_id"], self.bill.id)

    # 3 ------------------------------------------------------------------
    def test_customer_is_the_sap_contact_person(self):
        self._order(1, shiptocode="MAIN PLANT", cntctcode=900101)
        self._order(2, shiptocode="MAIN PLANT")
        _, vals = self._run()
        self.assertEqual(vals[1]["partner_id"], self.contact.id)
        self.assertEqual(self.contact.commercial_partner_id, self.company)
        self.assertEqual(vals[2]["partner_id"], self.company.id, "no CntctCode: the company")
        self.assertEqual(vals[1]["partner_shipping_id"], self.ship_main.id)

    # 4 ------------------------------------------------------------------
    def test_missing_address_warns_and_falls_back(self):
        self._order(1, shiptocode="DELETED SITE", paytocode="OLD AP", cntctcode=900101)
        self._order(2)
        _, vals = self._run()
        self.assertEqual(vals[1]["partner_shipping_id"], self.contact.id)
        self.assertEqual(vals[1]["partner_invoice_id"], self.contact.id)
        self.assertEqual(vals[2]["partner_shipping_id"], self.company.id)
        refs = [c.kwargs.get("source_ref") for c in self.ctx.report.warning.call_args_list]
        self.assertEqual(refs.count("ordr:1"), 2, "one warning each for ship-to and bill-to")
        self.assertNotIn("ordr:2", refs, "no code in SAP: nothing to warn about")
