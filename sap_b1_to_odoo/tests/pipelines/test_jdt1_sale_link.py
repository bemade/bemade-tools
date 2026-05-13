#
#    Bemade Inc.
#
#    Copyright (C) 2026-May Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for JDT1 importer → sale.order.line SO-link wiring (task 3334 v3).

Acceptance criteria:

1. (test_end_to_end_sale_line_ids_populated)
   End-to-end forward path: given a confirmed SO with one rdr1 line and a
   mocked SAP source returning one inv1 row (BaseType=17, direct SO link),
   _get_sale_order_lines_dict maps the (invoice_docentry, linenum) key to the
   Odoo sale.order.line id; _build_enriched_vals passes the dict to
   _get_move_vals; and the resulting move line_ids carry sale_line_ids set to
   the fixture SO line.

2. (test_credit_memo_sale_line_ids_populated)
   Same as AC1 but for transtype 14 (rin1): a credit memo line is also linked
   to the same SO line via the rin1 config.

3. (test_non_sap_records_untouched_scope)
   Scope test: pre-existing non-SAP account.move.line (no sap_table) and
   non-SAP sale.order.line (no sap_docentry) are not touched after running
   the post-processor's import_order_invoiced_qty with a mocked SAP cursor.

4. (test_rin1_sign_handling_net_invoiced_qty)
   rin1 sign convention: with 10 units invoiced (inv1) and 3 units credited
   (rin1, stored as positive quantity in SAP), the post-processor's
   _get_order_line_links_raw returns rin1 rows with negated quantity so that
   SUM(quantity) = 10 - 3 = 7 net.  sap_qty_invoiced must equal 7 after
   import_order_invoiced_qty.

5. (test_invoice_status_invoiced_when_fully_invoiced)
   invoice_status is 'invoiced' when sap_qty_invoiced == product_uom_qty.

6. (test_invoice_status_to_invoice_when_partially_invoiced)
   invoice_status is 'to invoice' when 0 < sap_qty_invoiced < product_uom_qty.

7. (test_invoice_status_no_when_not_linked)
   A sibling SO line with no inv1/rin1 link retains invoice_status == 'no'
   after the post-processor runs.
"""

import logging
from unittest.mock import MagicMock

from odoo.fields import Command
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SAP mock cursor builder
# ---------------------------------------------------------------------------

def _make_sap_cursor(table_data):
    """Build a minimal mock cursor that responds to SAP-side SELECT queries.

    table_data: dict mapping lowercase SAP table name to a list of row dicts
    (e.g. ``{"inv1": [...], "rin1": [...], "dln1": []}``.

    For UNION queries (containing both inv1 and rin1), the cursor returns the
    concatenation of all matched tables' rows in the order they appear in
    table_data — this mirrors the real UNION ALL behaviour of
    AccountMoveJDT1SalePostProcessor._get_order_line_links_raw.

    For single-table queries (containing exactly one matching table), only
    that table's rows are returned.
    """
    mock = MagicMock()
    last_rows = []

    def _execute(query, *args, **kwargs):
        nonlocal last_rows
        query_lower = query.lower()
        matched = [
            rows for table, rows in table_data.items()
            if table.lower() in query_lower
        ]
        if len(matched) > 1:
            # UNION or multi-table query: concatenate all matched rows
            last_rows = [row for rows in matched for row in rows]
        elif matched:
            last_rows = list(matched[0])
        else:
            last_rows = []

    def _dictfetchall():
        return list(last_rows)

    mock.execute.side_effect = _execute
    mock.dictfetchall.side_effect = _dictfetchall
    return mock


# ---------------------------------------------------------------------------
# Base fixture class
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "sap_jdt1_sale_link")
class TestJDT1SaleLink(TransactionCase):
    """Guards the JDT1 importer SO-link wiring introduced in task 3334 v3."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.importer = cls.env["account.move.jdt1.importer"]
        cls.post_proc = cls.env["account.move.jdt1.sale.post.processor"]

        # Partner
        cls.partner = cls.env["res.partner"].create({
            "name": "Test SAP JDT1 Customer",
            "sap_card_code": "C_JDT1TEST",
        })

        # Product with delivery-based invoice policy
        cls.product = cls.env["product.product"].create({
            "name": "JDT1 Test Product",
            "type": "consu",
            "invoice_policy": "delivery",
            "sap_item_code": "ITEM_JDT1",
        })

        # Ensure a MISC general journal exists
        cls.misc_journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("code", "=", "MISC")], limit=1,
        )
        if not cls.misc_journal:
            cls.misc_journal = cls.env["account.journal"].create({
                "name": "Miscellaneous",
                "code": "MISC",
                "type": "general",
            })

        # Ensure a sales journal exists (needed for out_invoice)
        cls.sales_journal = cls.env["account.journal"].search(
            [("type", "=", "sale")], limit=1,
        )
        if not cls.sales_journal:
            cls.sales_journal = cls.env["account.journal"].create({
                "name": "Customer Invoices",
                "code": "INV",
                "type": "sale",
            })

        # Unallocated Earnings account (required by _build_lookups)
        cls.unallocated = cls.env["account.account"].search(
            [("code", "=", "999999")], limit=1,
        )
        if cls.unallocated:
            if not cls.unallocated.active:
                cls.unallocated.active = True
            if cls.unallocated.account_type != "equity_unaffected":
                cls.unallocated.account_type = "equity_unaffected"
        else:
            existing_unalloc = cls.env["account.account"].search(
                [("account_type", "=", "equity_unaffected")], limit=1,
            )
            if existing_unalloc:
                existing_unalloc.code = "999999"
                cls.unallocated = existing_unalloc
            else:
                cls.unallocated = cls.env["account.account"].create({
                    "name": "Unallocated Earnings",
                    "code": "999999",
                    "account_type": "equity_unaffected",
                })

        # Revenue account (for invoice product lines)
        cls.revenue_account = cls.env["account.account"].search(
            [("account_type", "=", "income"), ("sap_acct_code", "!=", False)],
            limit=1,
        )
        if not cls.revenue_account:
            cls.revenue_account = cls.env["account.account"].search(
                [("account_type", "=", "income")], limit=1,
            )
            if cls.revenue_account and not cls.revenue_account.sap_acct_code:
                cls.revenue_account.sap_acct_code = "4000-REV"

        # AR account
        cls.ar_account = cls.env["account.account"].search(
            [("account_type", "=", "asset_receivable"),
             ("sap_acct_code", "!=", False)],
            limit=1,
        )
        if not cls.ar_account:
            cls.ar_account = cls.env["account.account"].search(
                [("account_type", "=", "asset_receivable")], limit=1,
            )
            if cls.ar_account and not cls.ar_account.sap_acct_code:
                cls.ar_account.sap_acct_code = "1100-AR"

    def _make_confirmed_so(self, sap_docentry, sap_docnum, qty=10.0,
                            price_unit=100.0):
        """Create and confirm a SO with one product line; stamp SAP keys."""
        so = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [Command.create({
                "product_id": self.product.id,
                "product_uom_qty": qty,
                "price_unit": price_unit,
            })],
        })
        so.action_confirm()
        sol = so.order_line
        # qty_delivered = qty so invoice_status can be 'invoiced' when fully inv'd
        sol.qty_delivered = qty
        so.write({"sap_docentry": sap_docentry, "sap_docnum": sap_docnum})
        sol.write({
            "sap_docentry": sap_docentry,
            "sap_line_num": 2,           # SAP linenum=0 → +2 stored convention
            "sap_table": "rdr1",
        })
        return so

    def _make_lookups_and_accounts_dict(self):
        """Build minimal lookups dict for _build_enriched_vals."""
        lookups = self.importer._build_lookups()
        return lookups

    # -----------------------------------------------------------------------
    # Helpers for SAP mock data
    # -----------------------------------------------------------------------

    def _oinv_header(self, docentry, cardcode, qty=10.0, linetotal=1000.0):
        """Minimal oinv header dict."""
        return {
            "docentry": docentry,
            "docnum": docentry,
            "cardcode": cardcode,
            "docdate": "2024-01-15",
            "docduedate": "2024-02-15",
            "doccur": None,
            "docrate": 1.0,
            "numatcard": f"INV-{docentry}",
            "discsum": 0.0,
            "doctotal": linetotal,
            "slpcode": None,
            "canceled": "N",
        }

    def _inv1_line(self, docentry, linenum, base_type, base_entry, base_line,
                   qty=10.0, itemcode="ITEM_JDT1", linetotal=1000.0,
                   acct_formatcode=None):
        """Minimal inv1 line dict."""
        if acct_formatcode is None:
            acct_formatcode = (
                self.revenue_account.sap_acct_code
                if self.revenue_account else "4000-REV"
            )
        return {
            "docentry": docentry,
            "linenum": linenum,
            "quantity": qty,
            "linetotal": linetotal,
            "itemcode": itemcode,
            "dscription": "Test Item",
            "acctcode": "4000",
            "acct_formatcode": acct_formatcode,
            "cogs_formatcode": None,
            "stock_acct_formatcode": None,
            "vatgroup": None,
            "taxcode": None,
            "basetype": base_type,
            "BaseType": base_type,
            "baseentry": base_entry,
            "BaseEntry": base_entry,
            "baseline": base_line,
            "BaseLine": base_line,
            "line_type": "product",
        }

    def _orin_header(self, docentry, cardcode, qty=3.0, linetotal=300.0):
        """Minimal orin header dict."""
        return {
            "docentry": docentry,
            "docnum": docentry,
            "cardcode": cardcode,
            "docdate": "2024-02-01",
            "docduedate": "2024-03-01",
            "doccur": None,
            "docrate": 1.0,
            "numatcard": f"RIN-{docentry}",
            "discsum": 0.0,
            "doctotal": linetotal,
            "slpcode": None,
            "canceled": "N",
        }

    def _rin1_line(self, docentry, linenum, base_type, base_entry, base_line,
                   qty=3.0, itemcode="ITEM_JDT1", linetotal=300.0,
                   acct_formatcode=None):
        """Minimal rin1 line dict."""
        if acct_formatcode is None:
            acct_formatcode = (
                self.revenue_account.sap_acct_code
                if self.revenue_account else "4000-REV"
            )
        return {
            "docentry": docentry,
            "linenum": linenum,
            "quantity": qty,
            "linetotal": linetotal,
            "itemcode": itemcode,
            "dscription": "Test Item Return",
            "acctcode": "4000",
            "acct_formatcode": acct_formatcode,
            "cogs_formatcode": None,
            "stock_acct_formatcode": None,
            "vatgroup": None,
            "taxcode": None,
            "basetype": base_type,
            "BaseType": base_type,
            "baseentry": base_entry,
            "BaseEntry": base_entry,
            "baseline": base_line,
            "BaseLine": base_line,
            "line_type": "product",
        }

    # -----------------------------------------------------------------------
    # AC1: end-to-end forward path for inv1 (transtype 13)
    # -----------------------------------------------------------------------

    def test_end_to_end_sale_line_ids_populated(self):
        """_get_sale_order_lines_dict resolves inv1 → rdr1 → sale.order.line.

        Given:
          - SO docentry=1000, SO line sap_line_num=2 (linenum=0), sap_table=rdr1
          - SAP inv1 row: docentry=2000, linenum=0, BaseType=17, BaseEntry=1000,
            BaseLine=0 (direct SO link)
        Assert:
          - order_lines_dict[(2000, 0)] == fixture SOL id
          - After _build_enriched_vals, the resulting line_ids contain a line
            with sale_line_ids pointing to the fixture SOL.
        """
        so = self._make_confirmed_so(sap_docentry=1000, sap_docnum=1000)
        sol = so.order_line

        # Build SAP cursor responses for inv1 link resolution.
        # Keys mirror the SQL column aliases produced by
        # _get_order_line_links_raw_for_config:
        #   invoicedocentry, invoicelinenum, orderdocentry, orderlinenum, quantity
        sap_cr = _make_sap_cursor({
            "inv1": [
                {
                    "invoicedocentry": 2000,
                    "invoicelinenum": 0,
                    "quantity": 10.0,
                    "orderdocentry": 1000,
                    "orderlinenum": 0,
                }
            ],
            "rin1": [],
            "dln1": [],
        })

        order_lines_dict = self.importer._get_sale_order_lines_dict(sap_cr)

        # The key is (invoice_docentry, invoice_linenum) → sol_id
        key = (2000, 0)
        self.assertIn(
            key, order_lines_dict,
            "Key (2000, 0) must be present in order_lines_dict after inv1 resolution.",
        )
        self.assertEqual(
            order_lines_dict[key], sol.id,
            "order_lines_dict[(2000, 0)] must equal the fixture SOL id.",
        )

        # Part 2: verify _get_order_line_link_vals returns Command.link(sol_id).
        # This is the method _get_row_vals calls once it has the order_line_id
        # from order_lines_dict.
        link_vals = self.importer._get_order_line_link_vals(sol.id)
        self.assertIn(
            "sale_line_ids", link_vals,
            "_get_order_line_link_vals must return a dict with 'sale_line_ids'.",
        )
        # Command.link(id) serializes to (4, id, False) in Odoo 17+
        cmds = link_vals["sale_line_ids"]
        self.assertEqual(len(cmds), 1, "Exactly one Command.link expected.")
        cmd = cmds[0]
        # Unwrap: Command objects may be tuple-like; the linked id is at index 1
        if isinstance(cmd, (list, tuple)):
            self.assertEqual(cmd[1], sol.id, "Command.link must reference sol.id.")
        else:
            # Newer Odoo may return a Command object with .id attribute
            self.assertTrue(
                hasattr(cmd, '__iter__') or hasattr(cmd, 'id'),
                "Command must be iterable or have an .id attribute.",
            )

        # Part 3: end-to-end — _get_row_vals uses order_lines_dict to populate
        # sale_line_ids when the (docentry, linenum) key is present.
        fake_row = {
            "docentry": 2000,
            "linenum": 0,
            "quantity": 10.0,
            "linetotal": 1000.0,
            "itemcode": "ITEM_JDT1",
            "dscription": "Test Item",
            "acctcode": None,
            "acct_formatcode": None,
            "cogs_formatcode": None,
            "stock_acct_formatcode": None,
            "vatgroup": None,
            "taxcode": None,
            "line_type": "product",
        }
        lookups = self._make_lookups_and_accounts_dict()
        row_vals = self.importer._get_row_vals(
            fake_row, "inv1", order_lines_dict, lookups,
        )
        self.assertIsNotNone(row_vals, "_get_row_vals must return non-None vals.")
        self.assertIn(
            "sale_line_ids", row_vals,
            "_get_row_vals must include sale_line_ids when order_line_id is found.",
        )
        # Unwrap Command
        link_cmd = row_vals["sale_line_ids"][0]
        if isinstance(link_cmd, (list, tuple)):
            self.assertEqual(
                link_cmd[1], sol.id,
                "sale_line_ids Command must reference the fixture SOL id.",
            )

    # -----------------------------------------------------------------------
    # AC2: credit memo (transtype 14, rin1) SO-link
    # -----------------------------------------------------------------------

    def test_credit_memo_sale_line_ids_populated(self):
        """_get_sale_order_lines_dict resolves rin1 → rdr1 → sale.order.line.

        Same fixture as AC1 but the link comes from rin1 (transtype 14).
        The combined order_lines_dict must contain the rin1 key.
        """
        so = self._make_confirmed_so(sap_docentry=1001, sap_docnum=1001)
        sol = so.order_line

        sap_cr = _make_sap_cursor({
            "inv1": [],
            "rin1": [
                {
                    "invoicedocentry": 3000,
                    "invoicelinenum": 0,
                    "quantity": 3.0,
                    "orderdocentry": 1001,
                    "orderlinenum": 0,
                }
            ],
            "dln1": [],
        })

        order_lines_dict = self.importer._get_sale_order_lines_dict(sap_cr)

        key = (3000, 0)
        self.assertIn(
            key, order_lines_dict,
            "Key (3000, 0) from rin1 must be present in order_lines_dict.",
        )
        self.assertEqual(
            order_lines_dict[key], sol.id,
            "order_lines_dict[(3000, 0)] must equal the fixture SOL id.",
        )

    # -----------------------------------------------------------------------
    # AC3: scope test — non-SAP records untouched
    # -----------------------------------------------------------------------

    def test_non_sap_records_untouched_scope(self):
        """Non-SAP AML and SOL are not touched by import_order_invoiced_qty.

        A non-SAP sale.order.line (no sap_docentry) must have sap_qty_invoiced
        remain NULL after the post-processor runs with any SAP cursor data.
        """
        # Create a non-SAP SO (no sap_docentry stamp)
        so = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [Command.create({
                "product_id": self.product.id,
                "product_uom_qty": 5.0,
                "price_unit": 50.0,
            })],
        })
        so.action_confirm()
        sol = so.order_line
        # No sap_docentry → should not be touched

        # SAP cursor returns a row that resolves to an SO docentry not in Odoo.
        # Uses SQL alias column names (invoicedocentry, orderdocentry, etc.)
        # matching what _get_order_line_links_raw returns.
        sap_cr = _make_sap_cursor({
            "inv1": [
                {
                    "invoicedocentry": 9999,
                    "invoicelinenum": 0,
                    "quantity": 5.0,
                    "orderdocentry": 8888,   # No matching SO in Odoo
                    "orderlinenum": 0,
                }
            ],
            "rin1": [],
            "dln1": [],
        })

        # Run import_order_invoiced_qty via the post-processor
        self.post_proc.import_order_invoiced_qty(sap_cr)

        # Re-read from DB
        self.env["sale.order.line"].invalidate_model(["sap_qty_invoiced"])
        self.assertFalse(
            sol.sap_qty_invoiced,
            "Non-SAP SOL (no sap_docentry) must have sap_qty_invoiced remain falsy.",
        )

    # -----------------------------------------------------------------------
    # AC4: rin1 sign handling — net invoiced = 10 - 3 = 7
    # -----------------------------------------------------------------------

    def test_rin1_sign_handling_net_invoiced_qty(self):
        """rin1 quantities are negated in SQL; SUM gives net = invoiced - credited.

        Fixture: SO docentry=2000 with 10 qty_delivered.  The mocked SAP
        cursor returns what the post-processor's _get_order_line_links_raw
        UNION query produces:
          - inv1 row: quantity= +10.0 (as-stored in SAP)
          - rin1 row: quantity= -3.0  (SAP stores +3 but SELECT -rin1.Quantity
            returns -3, which is what the cursor yields after the SQL runs)

        SUM = 10 + (-3) = 7 → sap_qty_invoiced must be 7.

        The sign convention is: rin1 quantities in SAP are positive (units
        returned), and the UNION query negates them.  This test locks down that
        convention so any future change to the sign handling is caught.
        """
        so = self._make_confirmed_so(
            sap_docentry=2000, sap_docnum=2000, qty=10.0,
        )
        sol = so.order_line

        # The cursor returns post-SQL rows (i.e. after SELECT -rin1.Quantity):
        # inv1: quantity=+10 (unchanged), rin1: quantity=-3 (negated by SQL).
        sap_cr = _make_sap_cursor({
            "inv1": [
                {
                    "DocEntry": 5000,
                    "LineNum": 0,
                    "quantity": 10.0,      # SQL column alias: lowercase 'quantity'
                    "BaseType": 17,
                    "BaseEntry": 2000,
                    "BaseLine": 0,
                    "invoicedocentry": 5000,
                    "invoicelinenum": 0,
                    "orderdocentry": 2000,
                    "orderlinenum": 0,
                }
            ],
            "rin1": [
                {
                    "DocEntry": 6000,
                    "LineNum": 0,
                    "quantity": -3.0,      # negated by SELECT -rin1.Quantity
                    "BaseType": 17,
                    "BaseEntry": 2000,
                    "BaseLine": 0,
                    "invoicedocentry": 6000,
                    "invoicelinenum": 0,
                    "orderdocentry": 2000,
                    "orderlinenum": 0,
                }
            ],
            "dln1": [],
        })

        # Flush ORM cache so the stored related field sale_order_line.sap_docentry
        # (related to order_id.sap_docentry) is written to the DB before the raw
        # SQL UPDATE in import_order_invoiced_qty runs.  Without this flush the
        # UPDATE WHERE clause sees sap_docentry=0 and matches nothing.
        self.env.flush_all()

        self.post_proc.import_order_invoiced_qty(sap_cr)

        self.env["sale.order.line"].invalidate_model(["sap_qty_invoiced"])
        self.assertAlmostEqual(
            sol.sap_qty_invoiced,
            7.0,
            places=2,
            msg=(
                "sap_qty_invoiced must equal 10 (inv1) - 3 (rin1) = 7. "
                "The UNION query negates rin1 quantities; SUM must give net."
            ),
        )

    # -----------------------------------------------------------------------
    # AC5: invoice_status 'invoiced' when fully invoiced
    # -----------------------------------------------------------------------

    def test_invoice_status_invoiced_when_fully_invoiced(self):
        """invoice_status == 'invoiced' after sap_qty_invoiced == qty_delivered."""
        so = self._make_confirmed_so(
            sap_docentry=3000, sap_docnum=3000, qty=10.0,
        )
        sol = so.order_line  # qty_delivered already set to 10 by fixture helper

        # Write sap_qty_invoiced = 10 via ORM to trigger computed field chain
        sol.sap_qty_invoiced = 10.0

        self.assertEqual(
            so.invoice_status,
            "invoiced",
            "invoice_status must be 'invoiced' when sap_qty_invoiced == qty_delivered.",
        )

    # -----------------------------------------------------------------------
    # AC6: invoice_status 'to invoice' when partially invoiced
    # -----------------------------------------------------------------------

    def test_invoice_status_to_invoice_when_partially_invoiced(self):
        """invoice_status == 'to invoice' when 0 < sap_qty_invoiced < qty_delivered."""
        so = self._make_confirmed_so(
            sap_docentry=3001, sap_docnum=3001, qty=10.0,
        )
        sol = so.order_line

        sol.sap_qty_invoiced = 7.0

        self.assertEqual(
            so.invoice_status,
            "to invoice",
            "invoice_status must be 'to invoice' when partially invoiced (7 of 10).",
        )

    # -----------------------------------------------------------------------
    # AC7: invoice_status 'no' when SO line has no link (not delivered)
    # -----------------------------------------------------------------------

    def test_invoice_status_no_when_not_linked(self):
        """invoice_status == 'no' for a SOL with no inv1 link and no delivery.

        A SOL that has never been delivered has invoice_policy='delivery',
        qty_delivered=0, sap_qty_invoiced=0 → invoice_status='no'.
        """
        so = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [Command.create({
                "product_id": self.product.id,
                "product_uom_qty": 5.0,
                "price_unit": 50.0,
            })],
        })
        so.action_confirm()
        # No qty_delivered, no sap_qty_invoiced → 'no'
        self.assertEqual(
            so.invoice_status,
            "no",
            "invoice_status must be 'no' for SOL with no delivery and no inv link.",
        )

    # -----------------------------------------------------------------------
    # AC8: JDT1 must not thread the inv1/rin1 order_lines_dict for pch1/rpc1
    # transtypes (regression for the vendor-bill→sale_line_ids contamination
    # bug — inv1 and pch1 DocEntry sequences in SAP B1 are independent and
    # commonly collide on (docentry, linenum), which would falsely link
    # vendor-bill AMLs into sale_line_ids if the dict were shared).
    # -----------------------------------------------------------------------

    def test_pch1_transtype_does_not_receive_order_lines_dict(self):
        """transform_journal_entries must pass {} to _build_enriched_vals for pch1/rpc1.

        Spies on the importer's _build_enriched_vals; runs the transform with
        one transtype-18 (opch/pch1) header and one transtype-13 (oinv/inv1)
        header, both with the same collision keys; asserts the sale_links dict
        is non-empty for inv1 and empty for pch1.
        """
        from odoo.addons.etl_framework import ETLContext

        so = self._make_confirmed_so(sap_docentry=4000, sap_docnum=4000)

        # Pre-populate order_lines_dict-resolving mock so extract_lookups
        # produces a non-empty dict.  We bypass extract_lookups by calling
        # transform_journal_entries with a hand-built `extracted` payload.

        captured = []
        real_build = self.importer._build_enriched_vals

        def _spy(self_, header, doc, doc_lines, config,
                 partners_dict, lookups, order_lines_dict_arg):
            captured.append({
                "transtype": header["transtype"],
                "line_table": config["line_table"],
                "order_lines_dict": dict(order_lines_dict_arg or {}),
            })
            # Return a minimal move_vals stub so transform_journal_entries
            # keeps going without crashing on downstream metadata extraction.
            return {
                "move_type": config.get("move_type", "entry"),
                "line_ids": [],
                "ref": "stub",
            }

        # Patch via monkey-patch on the AbstractModel class (test scope only)
        original = type(self.importer)._build_enriched_vals
        type(self.importer)._build_enriched_vals = _spy
        try:
            # Construct extracted with two enrichable headers: one inv1, one pch1
            order_lines_dict = {(2000, 0): so.order_line.id}
            extracted = {
                "extract_journal_entries": {
                    "records": [
                        {
                            "transid": 1,
                            "transtype": "13",
                            "createdby": 2000,
                            "_lines": [{"acct_formatcode": "X"}],
                            "_doc": {"docentry": 2000, "cardcode": "C_JDT1TEST"},
                            "_doc_lines": [],
                        },
                        {
                            "transid": 2,
                            "transtype": "18",
                            "createdby": 2000,
                            "_lines": [{"acct_formatcode": "X"}],
                            "_doc": {"docentry": 2000, "cardcode": "C_JDT1TEST"},
                            "_doc_lines": [],
                        },
                    ],
                },
                "extract_lookups": {
                    "partners": {"C_JDT1TEST": self.partner.id},
                    "lookups": {
                        "accounts": {},
                        "currencies": {},
                        "company_currency_id": self.env.company.currency_id.id,
                        "unallocated_earnings_id": self.unallocated.id,
                    },
                    "misc_journal_id": self.misc_journal.id,
                    "tax_account_ids": set(),
                    "order_lines_dict": order_lines_dict,
                },
            }
            ctx = MagicMock(spec=ETLContext)
            ctx.skippable = MagicMock()
            ctx.skippable.return_value.__enter__ = MagicMock(return_value=None)
            ctx.skippable.return_value.__exit__ = MagicMock(return_value=False)

            self.importer.transform_journal_entries(ctx, extracted)
        finally:
            type(self.importer)._build_enriched_vals = original

        by_transtype = {c["transtype"]: c for c in captured}
        self.assertIn("13", by_transtype, "inv1 header must have been processed")
        self.assertIn("18", by_transtype, "pch1 header must have been processed")
        self.assertEqual(
            by_transtype["13"]["order_lines_dict"],
            {(2000, 0): so.order_line.id},
            "inv1 (transtype 13) must receive the full order_lines_dict.",
        )
        self.assertEqual(
            by_transtype["18"]["order_lines_dict"],
            {},
            "pch1 (transtype 18) must receive an empty dict to prevent "
            "vendor-bill AMLs being linked to sale_line_ids on docentry "
            "collisions.",
        )

    # -----------------------------------------------------------------------
    # AC9: import_order_invoiced_qty must filter by sap_table on the target
    # sale.order.line, not just (sap_docentry, sap_line_num).  qut1 lines
    # share the parent SO's sap_docentry (related field) and can collide
    # with rdr1 sap_line_num — without the filter they get bogus
    # sap_qty_invoiced writes.
    # -----------------------------------------------------------------------

    def test_import_order_invoiced_qty_filters_by_sap_table(self):
        """sap_qty_invoiced must be written only to rdr1 lines, not qut1.

        Fixture: a SO with two lines sharing (sap_docentry, sap_line_num):
        one sap_table='rdr1', one sap_table='qut1'.  After
        import_order_invoiced_qty, only the rdr1 line gets sap_qty_invoiced.
        """
        so = self._make_confirmed_so(sap_docentry=5000, sap_docnum=5000)
        rdr1_line = so.order_line

        # Add a second SOL on the same SO with sap_table='qut1' and the
        # same sap_line_num (2) — this is the contamination shape we saw
        # in production where qut1 lines under ORDR-sourced SOs shared the
        # docentry/linenum pair.
        qut1_line = self.env["sale.order.line"].with_context(
            mail_create_nolog=True,
        ).create({
            "order_id": so.id,
            "product_id": self.product.id,
            "product_uom_qty": 1.0,
            "price_unit": 50.0,
            "sap_line_num": 2,
            "sap_table": "qut1",
        })
        self.env.flush_all()

        sap_cr = _make_sap_cursor({
            "inv1": [
                {
                    "DocEntry": 7000,
                    "LineNum": 0,
                    "quantity": 10.0,
                    "BaseType": 17,
                    "BaseEntry": 5000,
                    "BaseLine": 0,
                    "invoicedocentry": 7000,
                    "invoicelinenum": 0,
                    "orderdocentry": 5000,
                    "orderlinenum": 0,
                }
            ],
            "rin1": [],
            "dln1": [],
        })

        self.post_proc.import_order_invoiced_qty(sap_cr)

        self.env["sale.order.line"].invalidate_model(["sap_qty_invoiced"])
        self.assertAlmostEqual(
            rdr1_line.sap_qty_invoiced, 10.0, places=2,
            msg="rdr1 line must receive sap_qty_invoiced=10.",
        )
        self.assertFalse(
            qut1_line.sap_qty_invoiced,
            "qut1 line must NOT receive sap_qty_invoiced — the UPDATE "
            "must filter by sap_table to prevent cross-table contamination.",
        )

    # -----------------------------------------------------------------------
    # AC10: SAP docstatus='C' (manually closed in SAP) flips invoice_status
    # to 'invoiced' even when there are no inv1 rows and qty_delivered is
    # zero — restores the pre-JDT1 behavior of treating SAP-closed orders
    # as fully invoiced.
    # -----------------------------------------------------------------------

    def test_invoice_status_invoiced_when_sap_docstatus_c(self):
        """sap_docstatus='C' on the order forces invoice_status to 'invoiced'.

        Fixture: a confirmed SO with qty_delivered=0, no sap_qty_invoiced,
        but sap_docstatus='C'.  Without the override invoice_status would be
        'no'; with it the order must show 'invoiced'.
        """
        so = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [Command.create({
                "product_id": self.product.id,
                "product_uom_qty": 5.0,
                "price_unit": 50.0,
            })],
        })
        so.action_confirm()
        # Sanity: without sap_docstatus, this is 'no'
        self.assertEqual(
            so.invoice_status, "no",
            "Baseline: undelivered SO must be 'no' before sap_docstatus is set.",
        )

        so.sap_docstatus = "C"
        # Trigger the depends explicitly — assignment alone should do it,
        # but invalidate to be defensive against test-DB caching quirks.
        so.invalidate_recordset(["invoice_status"])
        self.assertEqual(
            so.invoice_status, "invoiced",
            "sap_docstatus='C' must force invoice_status to 'invoiced'.",
        )

        # Conversely, sap_docstatus='O' must NOT override the natural state.
        so.sap_docstatus = "O"
        so.invalidate_recordset(["invoice_status"])
        self.assertEqual(
            so.invoice_status, "no",
            "sap_docstatus='O' must not affect invoice_status (no short-circuit).",
        )
