"""Post-import GL correction: fix account assignments on enriched moves.

Compares each enriched move's posted lines against JDT1 (the GL truth)
and corrects any account mismatches. Runs after the JDT1 importer.

The enrichment gives us proper move_types and product lines, but Odoo
may route some lines to different accounts than SAP. This pipeline
restores GL accuracy by overwriting accounts to match JDT1.
"""

import logging
from collections import defaultdict

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData

_logger = logging.getLogger(__name__)

_ENRICHABLE_TRANSTYPES = {"13", "14", "18", "19"}


@ETL.pipeline(
    target_model="account.move.line",
    importer_name="account.move.gl.correction",
    sap_source="jdt1",
    depends_on=["account.move.jdt1.importer"],
    allow_multiprocessing=False,
)
class AccountMoveGLCorrection(models.AbstractModel):
    _name = "account.move.gl.correction"
    _description = "Fix enriched move accounts to match JDT1 GL truth"

    @ETL.extract("jdt1")
    def extract_gl_corrections(self, ctx: ETLContext):
        """Extract JDT1 lines for all enriched moves and compare with Odoo."""
        # Get all enriched moves (sap_table = oinv/opch/orin/orpc)
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
            _logger.info("[GL Fix] No enriched moves to correct.")
            return {"corrections": []}

        # Build reverse map: (sap_table, docentry) -> move_id
        move_id_map = {(row[2], row[1]): row[0] for row in enriched_moves}

        # Map sap_table -> transtype for OJDT lookup
        table_to_transtype = {
            "oinv": "13", "orin": "14", "opch": "18", "orpc": "19",
        }

        # Find OJDT transids for these moves
        transid_to_move_id = {}
        for sap_table, docentries in self._group_by_table(enriched_moves):
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

        if not transid_to_move_id:
            _logger.info("[GL Fix] No OJDT matches found.")
            return {"corrections": []}

        # Fetch JDT1 lines for these transactions
        ctx.cr.execute(
            """
            SELECT j.transid, j.debit, j.credit, a.formatcode
              FROM jdt1 j
              JOIN oact a ON j.account = a.acctcode
             WHERE j.transid IN %s
             ORDER BY j.transid, j.line_id
            """,
            (tuple(transid_to_move_id.keys()),),
        )
        jdt1_by_move = defaultdict(list)
        for row in ctx.cr.dictfetchall():
            debit = float(row["debit"] or 0)
            credit = float(row["credit"] or 0)
            if debit == 0 and credit == 0:
                continue
            move_id = transid_to_move_id[row["transid"]]
            jdt1_by_move[move_id].append({
                "formatcode": row["formatcode"].strip(),
                "debit": debit,
                "credit": credit,
            })

        # Build account lookup
        accounts = ctx.env["account.account"].search_read(
            [("sap_acct_code", "!=", False)],
            ["id", "sap_acct_code"],
        )
        account_by_code = {a["sap_acct_code"]: a["id"] for a in accounts}

        # Compare and build corrections
        corrections = []
        for move_id, jdt1_lines in jdt1_by_move.items():
            # Build expected account balances from JDT1
            # Key: account_id -> {debit, credit}
            expected = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
            for jl in jdt1_lines:
                acct_id = account_by_code.get(jl["formatcode"])
                if acct_id:
                    expected[acct_id]["debit"] += jl["debit"]
                    expected[acct_id]["credit"] += jl["credit"]

            corrections.append({
                "move_id": move_id,
                "expected": dict(expected),
            })

        _logger.info(
            "[GL Fix] Prepared corrections for %d enriched moves.",
            len(corrections),
        )
        return {"corrections": corrections}

    @ETL.transform()
    def transform_gl_corrections(self, ctx: ETLContext, extracted):
        """Pass through — corrections already built in extract."""
        return extracted.get("extract_gl_corrections", {})

    @ETL.load()
    def load_gl_corrections(self, ctx: ETLContext, transformed):
        """Fix account assignments on posted moves to match JDT1."""
        data = transformed.get("transform_gl_corrections", {})
        corrections = data.get("corrections", [])
        if not corrections:
            _logger.info("[GL Fix] No corrections to apply.")
            return

        fixed_moves = 0
        fixed_lines = 0

        for correction in corrections:
            move_id = correction["move_id"]
            expected = correction["expected"]

            # Get actual posted lines for this move
            ctx.env.cr.execute(
                """
                SELECT aml.id, aml.account_id, aml.debit, aml.credit
                  FROM account_move_line aml
                 WHERE aml.move_id = %s
                """,
                (move_id,),
            )
            actual_lines = ctx.env.cr.fetchall()

            # Build actual account balances
            actual = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
            for line_id, account_id, debit, credit in actual_lines:
                actual[account_id]["debit"] += float(debit)
                actual[account_id]["credit"] += float(credit)

            # Compare — if they match, skip
            if self._balances_match(expected, actual):
                continue

            # Fix: for each Odoo line, find the JDT1 line it should match
            # by amount, and update its account if different.
            #
            # Strategy: match lines by (debit, credit) amount. For each
            # Odoo line, find an unmatched JDT1 entry with the same amounts
            # and assign its account.
            jdt1_pool = []
            for acct_id, totals in expected.items():
                jdt1_pool.append({
                    "account_id": acct_id,
                    "debit": round(totals["debit"], 2),
                    "credit": round(totals["credit"], 2),
                    "matched": False,
                })

            move_fixed = False
            for line_id, account_id, debit, credit in actual_lines:
                debit = round(float(debit), 2)
                credit = round(float(credit), 2)

                # Find matching JDT1 entry by amount
                for jl in jdt1_pool:
                    if jl["matched"]:
                        continue
                    if jl["debit"] == debit and jl["credit"] == credit:
                        if jl["account_id"] != account_id:
                            ctx.env.cr.execute(
                                "UPDATE account_move_line"
                                " SET account_id = %s"
                                " WHERE id = %s",
                                (jl["account_id"], line_id),
                            )
                            fixed_lines += 1
                            move_fixed = True
                        jl["matched"] = True
                        break

            if move_fixed:
                fixed_moves += 1

        if fixed_lines:
            ctx.env.invalidate_all()

        _logger.info(
            "[GL Fix] Corrected %d lines across %d moves.",
            fixed_lines, fixed_moves,
        )

    @staticmethod
    def _group_by_table(move_rows):
        """Group (id, sap_docentry, sap_table) rows by sap_table."""
        by_table = defaultdict(list)
        for _id, docentry, table in move_rows:
            by_table[table].append(docentry)
        return by_table.items()

    @staticmethod
    def _balances_match(expected, actual):
        """Check if account-level balances match within tolerance."""
        all_accounts = set(expected) | set(actual)
        for acct_id in all_accounts:
            e = expected.get(acct_id, {"debit": 0, "credit": 0})
            a = actual.get(acct_id, {"debit": 0, "credit": 0})
            if (
                abs(e["debit"] - a["debit"]) > 0.01
                or abs(e["credit"] - a["credit"]) > 0.01
            ):
                return False
        return True
