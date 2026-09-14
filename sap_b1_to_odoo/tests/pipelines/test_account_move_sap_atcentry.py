#
#    Bemade Inc.
#
#    Copyright (C) 2026-September Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Invoice / credit-memo moves must carry SAP AtcEntry.

``ir.attachment.importer`` links an ATC1 row to the Odoo record whose
``sap_atcentry`` equals the row's ``absentry``.  ``account.move`` is one of its
ATTACHMENT_MODELS, but none of the journal pipelines ever wrote the field, so
every AR/AP invoice and credit-note attachment was silently skipped.

All four document pipelines (OINV / OPCH / ORIN / ORPC, plus the JDT1
enrichment path) build their header vals through
``AccountMoveCommon._get_move_vals``; these tests pin that method.

Acceptance criteria:

1. (test_get_move_vals_carries_sap_atcentry)
   A header row with ``atcentry`` set yields ``vals["sap_atcentry"]`` equal to
   it.

2. (test_get_move_vals_without_atcentry_is_falsy)
   A header row whose ``atcentry`` is NULL (no attachment in SAP) yields a
   falsy ``sap_atcentry`` rather than raising.

3. (test_created_move_is_resolvable_by_attachment_importer)
   A move created from those vals is what
   ``ir.attachment.importer._get_record_dict`` maps ``absentry`` to on the
   ``account_move`` table — i.e. the attachment pipeline can now find it.
"""

from datetime import datetime

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("-at_install", "post_install", "sap_account_move_atcentry")
class TestAccountMoveSapAtcEntry(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.common = cls.env["account.move.jdt1.importer"]
        cls.partner = cls.env["res.partner"].create({
            "name": "SAP AtcEntry Customer",
            "sap_card_code": "C_ATC355",
        })
        cls.lookups = {
            "users": {},
            "currencies": {},
            "company_currency_id": cls.env.company.currency_id.id,
        }

    def _header(self, atcentry):
        return {
            "docentry": 3550,
            "docnum": 3550,
            "cardcode": "C_ATC355",
            "docdate": datetime(2024, 1, 15),
            "docduedate": datetime(2024, 2, 15),
            "doccur": None,
            "docrate": 1.0,
            "numatcard": "INV-3550",
            "discsum": 0.0,
            "doctotal": 0.0,
            "slpcode": None,
            "atcentry": atcentry,
        }

    def _move_vals(self, atcentry):
        return self.common._get_move_vals(
            self._header(atcentry), self.partner.id, {}, "oinv", "inv1", {},
            self.lookups,
        )

    def test_get_move_vals_carries_sap_atcentry(self):
        vals = self._move_vals(42)
        self.assertEqual(vals.get("sap_atcentry"), 42)

    def test_get_move_vals_without_atcentry_is_falsy(self):
        vals = self._move_vals(None)
        self.assertFalse(vals.get("sap_atcentry"))

    def test_created_move_is_resolvable_by_attachment_importer(self):
        vals = self._move_vals(42)
        vals["move_type"] = "out_invoice"
        move = self.env["account.move"].create(vals)
        self.env.flush_all()

        importer = self.env["ir.attachment.importer"]
        record_dict = importer._get_record_dict(self.env, "account_move")
        self.assertEqual(record_dict.get(42), move.id)
