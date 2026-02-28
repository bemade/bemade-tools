"""ETL Pipeline for importing credit memos from SAP B1 into Odoo.

SAP Tables:
- ORIN: A/R Credit Memo headers (customer refunds)
- RIN1: A/R Credit Memo lines
- ORPC: A/P Credit Memo headers (vendor refunds)
- RPC1: A/P Credit Memo lines

Odoo Mapping:
- account.move with move_type='out_refund' <- ORIN (A/R Credit Memo)
- account.move with move_type='in_refund' <- ORPC (A/P Credit Memo)
"""

import logging

from odoo import api, models
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.etl_framework.utils import post_lock

_logger = logging.getLogger(__name__)


# =============================================================================
# A/R Credit Memo (Customer Refunds) - ORIN/RIN1
# =============================================================================


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.move.credit.memo.importer",
    sap_source="orin",
    depends_on=[
        "account.journal.setup",
        "account.tax.importer",
        "res.partner.company.importer",
        "product.product.importer",
        "res.users.importer",
        "account.move.invoice.importer",  # Run after invoices
    ],
    multiprocessing_threshold=1000,
    chunk_size=50,
)
class AccountMoveCreditMemoETLImporter(models.AbstractModel):
    _name = "account.move.credit.memo.importer"
    _description = "SAP A/R Credit Memo Importer (ETL, ORIN/RIN1)"
    _inherit = "sap.account.move.importer.mixin"

    @api.model
    def _get_import_config(self):
        return {
            "header_table": "orin",
            "line_table": "rin1",
            "move_type": "out_refund",
        }

    @api.model
    def _get_order_line_link_config(self):
        """Credit memos may link back to original invoices."""
        return {
            "invoice_line_table": "rin1",
            "order_line_table": "rdr1",
            "picking_table": "dln1",
            "picking_basetype": 15,
            "order_basetype": 17,
            "order_line_model": "sale.order.line",
        }

    @api.model
    def _get_order_line_link_vals(self, order_line_id):
        """Return values to link credit memo line to sale order line."""
        return {"sale_line_ids": [(4, order_line_id)]}

    @ETL.extract("orin")
    def extract_credit_memos(self, ctx: ETLContext):
        """Extract all SAP A/R credit memos and their lines."""
        already_imported = ctx.env["account.move"].search(
            [
                ("sap_docnum", "!=", False),
                ("sap_table", "=", "orin"),
            ]
        )

        where = ""
        args = []
        if already_imported:
            where = "WHERE docentry not in %s"
            args = [tuple(already_imported.mapped("sap_docentry"))]

        sql = f"SELECT * FROM orin {where}" if where else "SELECT * FROM orin"
        ctx.cr.execute(SQL(sql, *args) if args else sql)
        docs = ctx.cr.dictfetchall()

        if not docs:
            return {"headers": []}

        # Embed lines into each header so chunking keeps data together
        lines = self._get_lines(ctx.cr, "rin1", docs)
        lines_by_doc = {}
        for line in lines:
            lines_by_doc.setdefault(line["docentry"], []).append(line)
        for doc in docs:
            doc["_lines"] = lines_by_doc.get(doc["docentry"], [])

        _logger.info(f"Extracted {len(docs)} A/R credit memos from SAP ORIN")
        return {"headers": docs}

    @ETL.extract("metadata")
    def extract_metadata(self, ctx: ETLContext):
        """Extract partners and lookups needed for transform."""
        partners = self.env["res.partner"].search_read(
            [("sap_card_code", "!=", False)],
            ["id", "sap_card_code"],
        )
        partners_dict = {p["sap_card_code"]: p["id"] for p in partners}

        order_lines_dict = self._get_order_line_links(ctx.cr)
        lookups = self._build_lookups()

        return {
            "partners": partners_dict,
            "order_lines": order_lines_dict,
            "lookups": lookups,
        }

    @ETL.transform()
    def transform_credit_memos(self, ctx: ETLContext, extracted):
        """Transform SAP credit memo headers/lines into account.move create vals."""
        data = extracted["extract_credit_memos"]
        headers = data.get("headers", [])

        if not headers:
            return {"move_vals": [], "lookups": {}}

        # Rebuild lines dict from embedded _lines for _get_move_vals
        lines_dict = {doc["docentry"]: doc.pop("_lines", []) for doc in headers}

        metadata = extracted["extract_metadata"]
        partners_id_dict = metadata["partners"]
        order_lines_dict = metadata["order_lines"]
        lookups = metadata["lookups"]

        moves_vals = []
        for doc in headers:
            partner_id = partners_id_dict.get(doc["cardcode"])
            if not partner_id:
                _logger.warning(
                    "Could not find partner with cardcode %s for credit memo",
                    doc["cardcode"],
                )
                continue

            vals = self._get_move_vals(
                doc, partner_id, lines_dict, "orin", "rin1", order_lines_dict, lookups
            )

            if not vals.get("line_ids"):
                _logger.warning(
                    f"Skipping credit memo docentry={doc['docentry']}: no line_ids generated"
                )
                continue

            # For credit memos: positive total = refund, negative total = invoice
            # (opposite of invoice logic, so we swap the arguments)
            self._normalize_move_type(vals, "out_refund", "out_invoice")
            moves_vals.append(vals)

        _logger.info(f"Transformed {len(moves_vals)} A/R credit memos")
        return {"move_vals": moves_vals, "lookups": lookups}

    @ETL.load()
    def load_credit_memos(self, ctx: ETLContext, transformed):
        """Create and post A/R credit memos."""
        data = transformed.get("transform_credit_memos", {})
        move_vals = data.get("move_vals", [])
        lookups = data.get("lookups", {})

        if not move_vals:
            return

        self._create_pending_currency_rates(lookups)

        # Create moves one-at-a-time so a single bad record doesn't kill the chunk
        moves = ctx.env["account.move"]
        for vals in move_vals:
            ref = f"A/R credit memo SAP#{vals.get('sap_docnum', '?')}"
            with ctx.skippable(ref):
                moves |= ctx.env["account.move"].create(vals)

        if not moves:
            return

        # Post one-at-a-time grouped by journal under advisory lock
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post A/R credit memo SAP#{move.sap_docnum or '?'}"):
                        move.action_post()


# =============================================================================
# A/P Credit Memo (Vendor Refunds) - ORPC/RPC1
# =============================================================================


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.move.vendor.credit.memo.importer",
    sap_source="orpc",
    depends_on=[
        "account.journal.setup",
        "account.tax.importer",
        "res.partner.company.importer",
        "product.product.importer",
        "res.users.importer",
        "account.move.bill.importer",  # Run after bills
    ],
    multiprocessing_threshold=1000,
    chunk_size=50,
)
class AccountMoveVendorCreditMemoETLImporter(models.AbstractModel):
    _name = "account.move.vendor.credit.memo.importer"
    _description = "SAP A/P Credit Memo Importer (ETL, ORPC/RPC1)"
    _inherit = "sap.account.move.importer.mixin"

    @api.model
    def _get_import_config(self):
        return {
            "header_table": "orpc",
            "line_table": "rpc1",
            "move_type": "in_refund",
        }

    @api.model
    def _get_order_line_link_config(self):
        """Vendor credit memos may link back to original bills."""
        return {
            "invoice_line_table": "rpc1",
            "order_line_table": "por1",
            "picking_table": "pdn1",
            "picking_basetype": 20,  # Goods Receipt PO
            "order_basetype": 22,  # Purchase Orders
            "order_line_model": "purchase.order.line",
        }

    @api.model
    def _get_order_line_link_vals(self, order_line_id):
        """Return values to link credit memo line to purchase order line."""
        return {"purchase_line_id": order_line_id}

    @ETL.extract("orpc")
    def extract_vendor_credit_memos(self, ctx: ETLContext):
        """Extract all SAP A/P credit memos and their lines."""
        already_imported = ctx.env["account.move"].search(
            [
                ("sap_docnum", "!=", False),
                ("sap_table", "=", "orpc"),
            ]
        )

        where = ""
        args = []
        if already_imported:
            where = "WHERE docentry not in %s"
            args = [tuple(already_imported.mapped("sap_docentry"))]

        sql = f"SELECT * FROM orpc {where}" if where else "SELECT * FROM orpc"
        ctx.cr.execute(SQL(sql, *args) if args else sql)
        docs = ctx.cr.dictfetchall()

        if not docs:
            return {"headers": []}

        # Embed lines into each header so chunking keeps data together
        lines = self._get_lines_with_accounts(ctx.cr, "rpc1", docs)
        lines_by_doc = {}
        for line in lines:
            lines_by_doc.setdefault(line["docentry"], []).append(line)
        for doc in docs:
            doc["_lines"] = lines_by_doc.get(doc["docentry"], [])

        _logger.info(f"Extracted {len(docs)} A/P credit memos from SAP ORPC")
        return {"headers": docs}

    def _get_lines_with_accounts(self, cr, line_table, docs):
        """Get lines with account formatcode for vendor credit memos."""
        docentry_tuple = tuple(doc["docentry"] for doc in docs)
        sql = f"""
            SELECT l.*, a.formatcode as acct_formatcode
            FROM {line_table} l
            LEFT JOIN oact a ON l.acctcode = a.acctcode
            WHERE l.docentry IN %s
        """
        cr.execute(sql, (docentry_tuple,))
        return cr.dictfetchall()

    @ETL.extract("metadata")
    def extract_metadata(self, ctx: ETLContext):
        """Extract partners and lookups needed for transform."""
        partners = self.env["res.partner"].search_read(
            [("sap_card_code", "!=", False)],
            ["id", "sap_card_code"],
        )
        partners_dict = {p["sap_card_code"]: p["id"] for p in partners}

        order_lines_dict = self._get_order_line_links(ctx.cr)
        lookups = self._build_lookups()

        return {
            "partners": partners_dict,
            "order_lines": order_lines_dict,
            "lookups": lookups,
        }

    @ETL.transform()
    def transform_vendor_credit_memos(self, ctx: ETLContext, extracted):
        """Transform SAP vendor credit memo headers/lines into account.move create vals."""
        data = extracted["extract_vendor_credit_memos"]
        headers = data.get("headers", [])

        if not headers:
            return {"move_vals": [], "lookups": {}}

        # Rebuild lines dict from embedded _lines for _get_move_vals
        lines_dict = {doc["docentry"]: doc.pop("_lines", []) for doc in headers}

        metadata = extracted["extract_metadata"]
        partners_id_dict = metadata["partners"]
        order_lines_dict = metadata["order_lines"]
        lookups = metadata["lookups"]

        moves_vals = []
        for doc in headers:
            partner_id = partners_id_dict.get(doc["cardcode"])
            if not partner_id:
                _logger.warning(
                    "Could not find partner with cardcode %s for vendor credit memo",
                    doc["cardcode"],
                )
                continue

            vals = self._get_move_vals(
                doc, partner_id, lines_dict, "orpc", "rpc1", order_lines_dict, lookups
            )

            if not vals.get("line_ids"):
                _logger.warning(
                    f"Skipping vendor credit memo docentry={doc['docentry']}: no line_ids generated"
                )
                continue

            # For credit memos: positive total = refund, negative total = invoice
            # (opposite of invoice logic, so we swap the arguments)
            self._normalize_move_type(vals, "in_refund", "in_invoice")
            moves_vals.append(vals)

        _logger.info(f"Transformed {len(moves_vals)} A/P credit memos")
        return {"move_vals": moves_vals, "lookups": lookups}

    @ETL.load()
    def load_vendor_credit_memos(self, ctx: ETLContext, transformed):
        """Create and post A/P credit memos."""
        data = transformed.get("transform_vendor_credit_memos", {})
        move_vals = data.get("move_vals", [])
        lookups = data.get("lookups", {})

        if not move_vals:
            return

        self._create_pending_currency_rates(lookups)

        # Create moves one-at-a-time so a single bad record doesn't kill the chunk
        moves = ctx.env["account.move"]
        for vals in move_vals:
            ref = f"A/P credit memo SAP#{vals.get('sap_docnum', '?')}"
            with ctx.skippable(ref):
                moves |= ctx.env["account.move"].create(vals)

        if not moves:
            return

        # Post one-at-a-time grouped by journal under advisory lock
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post A/P credit memo SAP#{move.sap_docnum or '?'}"):
                        move.action_post()
