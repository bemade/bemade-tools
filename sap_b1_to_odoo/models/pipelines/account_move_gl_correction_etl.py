"""Post-import GL correction: fix account assignments on enriched moves.

Compares each enriched move's account-level totals against JDT1 and
corrects mismatches by reassigning lines to the correct accounts.
Runs after the JDT1 importer, before reconciliation.
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
    _description = "Fix enriched move accounts to match JDT1 GL truth"

    @ETL.extract("jdt1")
    def extract_gl_corrections(self, ctx: ETLContext):
        """Build per-move expected vs actual account balances."""
        # Get all enriched moves
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

        move_id_map = {(row[2], row[1]): row[0] for row in enriched_moves}

        table_to_transtype = {
            "oinv": "13", "orin": "14", "opch": "18", "orpc": "19",
        }

        # Map enriched moves to OJDT transids
        transid_to_move_id = {}
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

        if not transid_to_move_id:
            _logger.info("[GL Fix] No OJDT matches found.")
            return {"corrections": []}

        # Build expected account-level balances from JDT1
        ctx.cr.execute(
            """
            SELECT j.transid,
                   a.formatcode,
                   SUM(j.debit) AS debit,
                   SUM(j.credit) AS credit
              FROM jdt1 j
              JOIN oact a ON j.account = a.acctcode
             WHERE j.transid IN %s
             GROUP BY j.transid, a.formatcode
            """,
            (tuple(transid_to_move_id.keys()),),
        )

        # account code -> account_id
        accounts = ctx.env["account.account"].search_read(
            [("sap_acct_code", "!=", False)], ["id", "sap_acct_code"],
        )
        account_by_code = {a["sap_acct_code"]: a["id"] for a in accounts}

        # expected[move_id][account_id] = {"debit": x, "credit": y}
        expected_by_move = defaultdict(lambda: defaultdict(
            lambda: {"debit": 0.0, "credit": 0.0}
        ))
        for row in ctx.env.cr.dictfetchall():
            move_id = transid_to_move_id[row["transid"]]
            code = row["formatcode"].strip()
            acct_id = account_by_code.get(code)
            if not acct_id:
                continue
            debit = round(float(row["debit"] or 0), 2)
            credit = round(float(row["credit"] or 0), 2)
            if debit or credit:
                expected_by_move[move_id][acct_id]["debit"] += debit
                expected_by_move[move_id][acct_id]["credit"] += credit

        # Build actual account-level balances from Odoo
        move_ids = list(expected_by_move.keys())
        ctx.env.cr.execute(
            """
            SELECT move_id, account_id,
                   ROUND(SUM(debit)::numeric, 2) AS debit,
                   ROUND(SUM(credit)::numeric, 2) AS credit
              FROM account_move_line
             WHERE move_id IN %s
             GROUP BY move_id, account_id
            """,
            (tuple(move_ids),),
        )
        actual_by_move = defaultdict(lambda: defaultdict(
            lambda: {"debit": 0.0, "credit": 0.0}
        ))
        for row in ctx.env.cr.dictfetchall():
            actual_by_move[row["move_id"]][row["account_id"]]["debit"] = float(row["debit"])
            actual_by_move[row["move_id"]][row["account_id"]]["credit"] = float(row["credit"])

        # Find moves with mismatches
        corrections = []
        for move_id in move_ids:
            expected = expected_by_move[move_id]
            actual = actual_by_move[move_id]
            all_accounts = set(expected) | set(actual)

            has_diff = False
            for acct_id in all_accounts:
                e = expected.get(acct_id, {"debit": 0, "credit": 0})
                a = actual.get(acct_id, {"debit": 0, "credit": 0})
                if (
                    abs(e["debit"] - a["debit"]) > 0.01
                    or abs(e["credit"] - a["credit"]) > 0.01
                ):
                    has_diff = True
                    break

            if has_diff:
                corrections.append({
                    "move_id": move_id,
                    "expected": {k: dict(v) for k, v in expected.items()},
                    "actual": {k: dict(v) for k, v in actual.items()},
                })

        _logger.info(
            "[GL Fix] %d of %d enriched moves need account corrections.",
            len(corrections), len(move_ids),
        )
        return {"corrections": corrections}

    @ETL.transform()
    def transform_gl_corrections(self, ctx: ETLContext, extracted):
        return extracted.get("extract_gl_corrections", {})

    @ETL.load()
    def load_gl_corrections(self, ctx: ETLContext, transformed):
        """Fix accounts by reassigning lines from wrong accounts to right ones.

        For each mismatched move, finds accounts with excess debit/credit
        (in Odoo but not in JDT1) and accounts with deficit (in JDT1 but
        not in Odoo), then moves lines from excess to deficit accounts.
        """
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
            actual = correction["actual"]

            # Compute per-account diffs: positive = Odoo has too much
            all_accounts = set(expected) | set(actual)
            debit_excess = {}   # account_id -> excess debit amount
            credit_excess = {}  # account_id -> excess credit amount

            for acct_id in all_accounts:
                e = expected.get(acct_id, {"debit": 0, "credit": 0})
                a = actual.get(acct_id, {"debit": 0, "credit": 0})
                d_diff = round(a["debit"] - e["debit"], 2)
                c_diff = round(a["credit"] - e["credit"], 2)
                if abs(d_diff) > 0.01:
                    debit_excess[acct_id] = d_diff
                if abs(c_diff) > 0.01:
                    credit_excess[acct_id] = c_diff

            # Get individual lines for this move
            ctx.env.cr.execute(
                """
                SELECT id, account_id, debit, credit
                  FROM account_move_line
                 WHERE move_id = %s
                 ORDER BY id
                """,
                (move_id,),
            )
            lines = [
                {"id": r[0], "account_id": r[1],
                 "debit": float(r[2]), "credit": float(r[3])}
                for r in ctx.env.cr.fetchall()
            ]

            move_fixed = False

            # For each line on an account with excess, try to reassign it
            # to an account with deficit of the same type (debit or credit)
            for line in lines:
                acct = line["account_id"]

                # Check debit excess
                if line["debit"] > 0 and acct in debit_excess and debit_excess[acct] > 0:
                    # Find an account that needs more debit
                    for target_acct, deficit in debit_excess.items():
                        if deficit < 0 and abs(deficit) >= line["debit"] - 0.01:
                            # Move this line to target account
                            ctx.env.cr.execute(
                                "UPDATE account_move_line"
                                " SET account_id = %s WHERE id = %s",
                                (target_acct, line["id"]),
                            )
                            debit_excess[acct] = round(
                                debit_excess[acct] - line["debit"], 2,
                            )
                            debit_excess[target_acct] = round(
                                deficit + line["debit"], 2,
                            )
                            fixed_lines += 1
                            move_fixed = True
                            break

                # Check credit excess
                if line["credit"] > 0 and acct in credit_excess and credit_excess[acct] > 0:
                    for target_acct, deficit in credit_excess.items():
                        if deficit < 0 and abs(deficit) >= line["credit"] - 0.01:
                            ctx.env.cr.execute(
                                "UPDATE account_move_line"
                                " SET account_id = %s WHERE id = %s",
                                (target_acct, line["id"]),
                            )
                            credit_excess[acct] = round(
                                credit_excess[acct] - line["credit"], 2,
                            )
                            credit_excess[target_acct] = round(
                                deficit + line["credit"], 2,
                            )
                            fixed_lines += 1
                            move_fixed = True
                            break

            if move_fixed:
                fixed_moves += 1

        if fixed_lines:
            ctx.env.invalidate_all()

        _logger.info(
            "[GL Fix] Corrected %d lines across %d moves.",
            fixed_lines, fixed_moves,
        )
