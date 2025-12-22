"""Vendor Bill ETL Pipeline

This module contains the ETL pipeline for importing vendor bills from SAP B1 (OPCH/PCH1).
It creates account.move records with move_type='in_invoice' and links them to purchase orders.
"""

import logging
from typing import Dict, List

from odoo import models
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.move.bill.importer",
    sap_source="opch",
    depends_on=[
        "account.journal.setup",
        "purchase.order.post.processor",  # Ensures POs and lines are ready
    ],
    multiprocessing_threshold=1000,
    chunk_size=50,
)
class AccountMoveBillETLImporter(models.AbstractModel):
    _name = "account.move.bill.importer"
    _description = "SAP Vendor Bill Importer (ETL, OPCH/PCH1)"
    _inherit = "sap.account.move.importer.mixin"

    @ETL.extract("opch")
    def extract_bills(self, ctx: ETLContext):
        """Extract vendor bills from SAP OPCH table."""
        # Get existing bills (idempotence) - include both invoices and refunds
        ctx.env.cr.execute(
            """
            SELECT DISTINCT sap_docnum 
            FROM account_move 
            WHERE sap_docnum IS NOT NULL 
            AND sap_table = 'opch'
            AND move_type IN ('in_invoice', 'in_refund')
            """
        )
        existing_docnums = tuple(row[0] for row in ctx.env.cr.fetchall())

        # Extract new bill headers
        sql = "SELECT * FROM opch"
        if existing_docnums:
            sql += " WHERE docnum NOT IN %s"
            ctx.cr.execute(sql, (existing_docnums,))
        else:
            ctx.cr.execute(sql)

        bills = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(bills)} vendor bills from SAP OPCH")
        return {"headers": bills}  # Use "headers" key for chunking

    @ETL.extract("pch1")
    def extract_bill_lines(self, ctx: ETLContext):
        """Extract vendor bill lines from SAP PCH1 table with account formatcode."""
        ctx.cr.execute(
            """
            SELECT 
                p.*,
                a.formatcode as acct_formatcode
            FROM pch1 p
            LEFT JOIN oact a ON p.acctcode = a.acctcode
        """
        )
        lines = ctx.cr.dictfetchall()

        # Group lines by bill for easier access
        lines_dict = {}
        for line in lines:
            lines_dict.setdefault(line["docentry"], []).append(line)

        _logger.info(f"Extracted {len(lines)} bill lines from SAP PCH1")
        return {"lines": lines_dict}

    @ETL.extract("por1")
    def extract_metadata(self, ctx: ETLContext):
        """Extract partners, order line links, and all lookups needed for transform."""
        # Get partners as ID dict (picklable for multiprocessing)
        partners = self.env["res.partner"].search_read(
            [("sap_card_code", "!=", False)],
            ["id", "sap_card_code"],
        )
        partners_dict = {p["sap_card_code"]: p["id"] for p in partners}

        order_lines_dict = self._get_order_line_links(ctx.cr)

        # Build all lookups once
        lookups = self._build_lookups()

        return {
            "partners": partners_dict,
            "order_lines": order_lines_dict,
            "lookups": lookups,
        }

    @ETL.transform()
    def transform_bills(self, ctx: ETLContext, extracted: Dict) -> Dict:
        """Transform SAP vendor bills into Odoo account.move values."""
        bills_data = extracted["extract_bills"]
        bills = bills_data.get("headers", [])

        lines_data = extracted["extract_bill_lines"]
        lines = lines_data.get("lines", {})

        if not bills:
            return {"move_vals": [], "lookups": {}}

        # Get partner and order line lookups from metadata
        metadata = extracted["extract_metadata"]
        partners_id_dict = metadata["partners"]
        order_lines_dict = metadata["order_lines"]
        lookups = metadata["lookups"]

        # Transform each bill
        bill_vals = []
        for bill in bills:
            docentry = bill["docentry"]
            bill_lines = lines.get(docentry, [])

            if not bill_lines:
                _logger.warning(f"Skipping bill docentry={docentry}: no lines found")
                continue

            # Get partner
            partner_id = partners_id_dict.get(bill["cardcode"])
            if not partner_id:
                _logger.warning(
                    f"Skipping bill docentry={docentry}: partner not found for cardcode={bill['cardcode']}"
                )
                continue

            # Create bill values using the mixin method
            vals = self._get_move_vals(
                bill,
                partner_id,
                lines,
                "opch",  # header table
                "pch1",  # line table
                order_lines_dict,
                lookups,
            )

            # Skip bills with no actual accounting lines
            if not vals.get("line_ids"):
                _logger.warning(
                    f"Skipping bill docentry={docentry}: no line_ids generated"
                )
                continue

            self._normalize_move_type(vals, "in_invoice", "in_refund")

            bill_vals.append(vals)

        return {"move_vals": bill_vals, "lookups": lookups}

    @ETL.load()
    def load_bills(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load vendor bills into Odoo."""
        data = transformed.get("transform_bills", {})
        bill_vals = data.get("move_vals", [])
        lookups = data.get("lookups", {})

        if not bill_vals:
            return

        # Batch-create any pending currency rates before creating moves
        self._create_pending_currency_rates(lookups)

        bills = ctx.env["account.move"].create(bill_vals)
        ctx.env.flush_all()
        bills.action_post()

    def _get_order_line_link_config(self):
        """Return configuration for linking bill lines to purchase order lines."""
        return {
            "invoice_line_table": "pch1",
            "order_line_table": "por1",
            "picking_table": "pdn1",  # Goods Receipts
            "picking_basetype": 20,  # Goods Receipts have BaseType = 20
            "order_basetype": 22,  # Purchase Orders have BaseType = 22
            "order_line_model": "purchase.order.line",
        }

    def _get_order_line_link_vals(self, order_line_id):
        """Return values for linking a bill line to a purchase order line."""
        return {"purchase_line_id": order_line_id}

    def _trigger_recomputation(self, ctx: ETLContext):
        """Trigger recomputation of invoiced quantities on purchase order lines."""
        # Get all purchase order lines that have been invoiced
        ctx.env.cr.execute(
            """
            SELECT DISTINCT pol.id
            FROM purchase_order_line pol
            INNER JOIN account_move_line aml ON aml.purchase_line_id = pol.id
            INNER JOIN account_move am ON am.id = aml.move_id
            WHERE am.sap_table = 'opch'
            AND am.move_type = 'in_invoice'
            """
        )
        line_ids = [row[0] for row in ctx.env.cr.fetchall()]

        if line_ids:
            _logger.info(
                f"Triggering recalculation of invoiced quantity for {len(line_ids)} purchase.order.line entries"
            )
            lines = ctx.env["purchase.order.line"].browse(line_ids)
            lines._compute_qty_invoiced()

        # Trigger invoice/billing status recomputation on purchase orders
        ctx.env.cr.execute(
            """
            SELECT DISTINCT po.id
            FROM purchase_order po
            INNER JOIN purchase_order_line pol ON pol.order_id = po.id
            INNER JOIN account_move_line aml ON aml.purchase_line_id = pol.id
            INNER JOIN account_move am ON am.id = aml.move_id
            WHERE am.sap_table = 'opch'
            AND am.move_type = 'in_invoice'
            """
        )
        order_ids = [row[0] for row in ctx.env.cr.fetchall()]

        if order_ids:
            _logger.info(
                f"Triggering recalculation of invoice/billing status for {len(order_ids)} purchase.order"
            )
            orders = ctx.env["purchase.order"].browse(order_ids)
            orders._compute_invoice()
