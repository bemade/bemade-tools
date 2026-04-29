"""Post-import GL correction: fix AR/AP and tax accounts on enriched moves.

Enriched invoices/bills have correct revenue/expense accounts from SAP
but Odoo auto-generates AR/AP and tax lines using its own defaults.
This pipeline corrects those accounts to match JDT1.

Targets exactly:
  1. The payment_term line (AR/AP) — one per move
  2. Tax lines — one per tax group per move
"""

import logging
from collections import defaultdict

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move.line",
    importer_name="account.move.gl.correction",
    sap_source="jdt1",
    depends_on=["account.move.jdt1.importer"],
    allow_multiprocessing=False,
)
class AccountMoveGLCorrection(models.AbstractModel):
    _name = "account.move.gl.correction"
    _description = "Fix AR/AP and tax accounts on enriched moves to match JDT1"

    @ETL.extract("jdt1")
    def extract_gl_corrections(self, ctx: ETLContext):
        """Build correction map: for each enriched move, find the JDT1
        AR/AP and tax account assignments."""
        # Get enriched moves
        ctx.env.cr.execute(
            """
            SELECT id, sap_docentry, sap_table
              FROM account_move
             WHERE sap_table IN ('oinv', 'orin', 'opch', 'orpc')
               AND state = 'posted'
            """
        )
        enriched_moves = ctx.env.cr.fetchall()

        if not enriched_moves:
            _logger.info("[GL Fix] No enriched moves.")
            return {"corrections": []}

        move_id_map = {(row[2], row[1]): row[0] for row in enriched_moves}

        table_to_transtype = {
            "oinv": "13", "orin": "14", "opch": "18", "orpc": "19",
        }

        # Map enriched moves → OJDT transids, tracking sap_table per move
        transid_to_move_id = {}
        move_id_to_table = {}
        by_table = defaultdict(list)
        for _, docentry, table in enriched_moves:
            by_table[table].append(docentry)

        for sap_table, docentries in by_table.items():
            transtype = table_to_transtype[sap_table]
            ctx.cr.execute(
                "SELECT createdby, transid FROM ojdt"
                " WHERE transtype = %s AND createdby IN %s",
                (transtype, tuple(docentries)),
            )
            for createdby, transid in ctx.cr.fetchall():
                move_id = move_id_map.get((sap_table, createdby))
                if move_id:
                    transid_to_move_id[transid] = move_id
                    move_id_to_table[move_id] = sap_table

        if not transid_to_move_id:
            _logger.info("[GL Fix] No OJDT matches.")
            return {"corrections": []}

        # Account lookup: sap_acct_code → (account_id, account_type)
        accounts = ctx.env["account.account"].search_read(
            [("sap_acct_code", "!=", False)],
            ["id", "sap_acct_code", "account_type"],
        )
        acct_by_code = {
            a["sap_acct_code"]: (a["id"], a["account_type"])
            for a in accounts
        }

        # Get JDT1 lines grouped by move, classified by account type
        ctx.cr.execute(
            """
            SELECT j.transid, a.formatcode,
                   SUM(j.debit) AS debit, SUM(j.credit) AS credit
              FROM jdt1 j
              JOIN oact a ON j.account = a.acctcode
             WHERE j.transid IN %s
               AND (j.debit <> 0 OR j.credit <> 0)
             GROUP BY j.transid, a.formatcode
            """,
            (tuple(transid_to_move_id.keys()),),
        )

        # For each move, find the AR/AP account and tax accounts from JDT1
        # AR/AP: the receivable or payable account
        # Tax: accounts that are liability_current (typical for tax payable)
        corrections = []
        jdt1_by_move = defaultdict(list)
        for row in ctx.cr.dictfetchall():
            move_id = transid_to_move_id[row["transid"]]
            code = row["formatcode"].strip()
            info = acct_by_code.get(code)
            if info:
                jdt1_by_move[move_id].append({
                    "account_id": info[0],
                    "account_type": info[1],
                    "debit": float(row["debit"] or 0),
                    "credit": float(row["credit"] or 0),
                })

        for move_id, jdt1_lines in jdt1_by_move.items():
            ar_ap_account = None
            tax_accounts = []
            sap_table = move_id_to_table.get(move_id, "")

            # The payment_term line's expected sign on the AR/AP control:
            #   oinv (out_invoice): debit  (customer owes us)
            #   orin (out_refund/AR CM): credit (we owe customer back)
            #   opch (in_invoice): credit (we owe vendor)
            #   orpc (in_refund/AP CM): debit  (vendor owes us back)
            # Pick the JDT1 AR/AP line whose sign matches the expected
            # payment_term direction.  The previous logic used a single
            # is_purchase flag and was wrong for refunds, picking the
            # opposite-side offset (often a customer-AR account on bills
            # like spousal-support payouts).
            payment_term_is_debit = sap_table in ("oinv", "orpc")

            for jl in jdt1_lines:
                if jl["account_type"] in (
                    "asset_receivable", "liability_payable",
                ):
                    if payment_term_is_debit and jl["debit"] > 0:
                        ar_ap_account = jl["account_id"]
                    elif not payment_term_is_debit and jl["credit"] > 0:
                        ar_ap_account = jl["account_id"]
                elif jl["account_type"] == "liability_current":
                    # Tax accounts are typically liability_current
                    tax_accounts.append({
                        "account_id": jl["account_id"],
                        "debit": jl["debit"],
                        "credit": jl["credit"],
                    })

            if ar_ap_account or tax_accounts:
                corrections.append({
                    "move_id": move_id,
                    "ar_ap_account_id": ar_ap_account,
                    "tax_accounts": tax_accounts,
                })

        _logger.info(
            "[GL Fix] Prepared corrections for %d moves "
            "(%d with AR/AP, %d with tax).",
            len(corrections),
            sum(1 for c in corrections if c["ar_ap_account_id"]),
            sum(1 for c in corrections if c["tax_accounts"]),
        )
        return {"corrections": corrections}

    @ETL.transform()
    def transform_gl_corrections(self, ctx: ETLContext, extracted):
        return extracted.get("extract_gl_corrections", {})

    @ETL.load()
    def load_gl_corrections(self, ctx: ETLContext, transformed):
        """Fix AR/AP and tax line accounts on posted moves."""
        data = transformed.get("transform_gl_corrections", {})
        corrections = data.get("corrections", [])
        if not corrections:
            _logger.info("[GL Fix] No corrections to apply.")
            return

        fixed_ar_ap = 0
        fixed_tax = 0

        for correction in corrections:
            move_id = correction["move_id"]

            # Fix AR/AP: update the payment_term line's account
            if correction["ar_ap_account_id"]:
                ctx.env.cr.execute(
                    """
                    UPDATE account_move_line
                       SET account_id = %s
                     WHERE move_id = %s
                       AND display_type = 'payment_term'
                    """,
                    (correction["ar_ap_account_id"], move_id),
                )
                if ctx.env.cr.rowcount > 0:
                    fixed_ar_ap += 1

            # Fix tax: match tax lines by amount to JDT1 tax accounts
            for tax_acct in correction["tax_accounts"]:
                # Match by amount — Odoo has one tax line per tax
                if tax_acct["credit"] > 0:
                    ctx.env.cr.execute(
                        """
                        UPDATE account_move_line
                           SET account_id = %s
                         WHERE move_id = %s
                           AND display_type = 'tax'
                           AND ABS(credit - %s) < 0.02
                        """,
                        (tax_acct["account_id"], move_id,
                         tax_acct["credit"]),
                    )
                elif tax_acct["debit"] > 0:
                    ctx.env.cr.execute(
                        """
                        UPDATE account_move_line
                           SET account_id = %s
                         WHERE move_id = %s
                           AND display_type = 'tax'
                           AND ABS(debit - %s) < 0.02
                        """,
                        (tax_acct["account_id"], move_id,
                         tax_acct["debit"]),
                    )
                if ctx.env.cr.rowcount > 0:
                    fixed_tax += 1

        if fixed_ar_ap or fixed_tax:
            ctx.env.invalidate_all()

        _logger.info(
            "[GL Fix] Fixed %d AR/AP lines, %d tax lines.",
            fixed_ar_ap, fixed_tax,
        )
