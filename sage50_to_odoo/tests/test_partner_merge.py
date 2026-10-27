"""How Sage customers and vendors become partners.

Acceptance criteria
-------------------
- A vendor whose folded name matches a customer is merged into it: one
  partner carrying both Sage ids (unchanged behaviour).
- A partner carries ONE Sage vendor id. When a second vendor folds onto the
  same customer ("Acme" and "Acme Inc" as two vendor records), it becomes a
  partner of its own instead of overwriting the first vendor's id, so the
  first vendor's bills still find their partner.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _row(lid, name, **extra):
    row = {
        "lId": lid, "sName": name, "sCntcName": "", "sStreet1": "",
        "sStreet2": "", "sCity": "", "sProvState": "", "sCountry": "",
        "sPostalZip": "", "sPhone1": "", "sPhone2": "", "sEmail": "",
        "sWebSite": "", "bInactive": 0, "nNetDay": 0,
    }
    row.update(extra)
    return row


def _customer(lid, name):
    return _row(lid, name, dCrLimit=0.0, lPrcListId=0)


def _vendor(lid, name):
    return _row(lid, name, sTaxId="", lTaxCode=0)


@tagged("post_install", "-at_install")
class TestPartnerMerge(TransactionCase):

    def _transform(self, customers, vendors):
        ctx = ETLContext(cr=None, env=self.env, source_config={})
        return self.env["sage.partner.importer"].transform_partners(ctx, {
            "extract_customers": customers,
            "extract_vendors": vendors,
        })

    def test_vendor_merges_into_matching_customer(self):
        records = self._transform(
            [_customer(10, "Acme")], [_vendor(20, "Acme")]
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["sage_customer_id"], 10)
        self.assertEqual(records[0]["sage_vendor_id"], 20)
        self.assertEqual(records[0]["supplier_rank"], 1)

    def test_second_vendor_on_the_same_customer_keeps_its_own_partner(self):
        records = self._transform(
            [_customer(10, "Acme")],
            [_vendor(20, "Acme"), _vendor(21, "Acme Inc")],
        )
        vendor_ids = sorted(r["sage_vendor_id"] for r in records)
        self.assertEqual(vendor_ids, [20, 21])
        merged = next(r for r in records if r["sage_customer_id"] == 10)
        self.assertEqual(merged["sage_vendor_id"], 20)
        alone = next(r for r in records if r["sage_vendor_id"] == 21)
        self.assertEqual(alone["sage_customer_id"], 0)
        self.assertEqual(alone["customer_rank"], 0)
