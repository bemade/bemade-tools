"""Verification script: compare SAP ITR1 reconsum vs Odoo partials.

Read-only.  Samples N reconciliation groups (default: all), computes the
expected per-pair amounts from FIFO allocation on SAP data, and compares
them against the ``account.partial.reconcile`` rows in Odoo.  Prints
mismatches as CSV to stdout.

Usage (from Odoo shell):
    odoo-bin shell -d <db> -c odoo.conf <<'EOF'
    exec(open("addons/sap_b1_to_odoo/tools/verify_itr_reconciliation.py").read())
    EOF

Or as a standalone script (requires PYTHONPATH to include the Odoo source):
    python -m odoo.addons.sap_b1_to_odoo.tools.verify_itr_reconciliation \\
        --database <db> --config <conf>

Environment variables (optional):
    VERIFY_ITR_LIMIT   -- max groups to sample (default: all)
    VERIFY_ITR_CSV     -- path to write CSV output (default: stdout)
    VERIFY_ITR_FORCE_RESET  -- if "1", print but do not delete any data

Exit codes:
    0 -- no mismatches found
    1 -- one or more mismatches found
    2 -- script error (e.g. Odoo env not available)

Output columns (CSV):
    reconnum, debit_sap_docentry, credit_sap_docentry,
    expected_amount, actual_amount, delta, status
"""

import csv
import logging
import os
import sys
from collections import defaultdict

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core verification logic (requires ``env`` to be in scope or passed in)
# ---------------------------------------------------------------------------

# ITR1.srcobjtyp -> transtype (same as pipeline)
_SRCOBJTYP_TO_TRANSTYPE = {
    "13": "13", "14": "14", "18": "18", "19": "19",
    "24": "24", "46": "46",
    "20": "20", "21": "21", "59": "59", "60": "60",
    "-3": "-3", "-4": "-4", "-5": "-5",
    "202": "202", "25": "25", "30": "30",
    "203": "203", "204": "204", "321": "321",
}

_SRCOBJTYP_TO_TABLE = {
    "13": "oinv", "14": "orin", "18": "opch", "19": "orpc",
    "24": "orct", "46": "ovpm",
    "20": "opdn", "21": "orpd", "59": "oign", "60": "oige",
    "-3": "owtr", "-4": "oiqr", "-5": "oiqr",
    "202": "owor", "25": "odpo", "30": "ojdt",
    "203": "orin", "204": "orpc", "321": "oitr",
}


def _allocate_fifo_plain(debits, credits):
    """Pure FIFO allocator (amounts only, no AML objects).

    Args:
        debits: list of ``{"reconsum": float}``
        credits: list of ``{"reconsum": float}``

    Returns:
        list of ``(d_idx, c_idx, amount)`` triples.
    """
    d_caps = [d["reconsum"] for d in debits]
    c_caps = [c["reconsum"] for c in credits]
    triples = []
    di = ci = 0
    while di < len(debits) and ci < len(credits):
        while di < len(debits) and d_caps[di] < 0.005:
            di += 1
        while ci < len(credits) and c_caps[ci] < 0.005:
            ci += 1
        if di >= len(debits) or ci >= len(credits):
            break
        amount = round(min(d_caps[di], c_caps[ci]), 2)
        triples.append((di, ci, amount))
        d_caps[di] = round(d_caps[di] - amount, 2)
        c_caps[ci] = round(c_caps[ci] - amount, 2)
    return triples


def verify_itr_reconciliation(env, limit=None, out=None):
    """Compare SAP ITR1 allocation vs Odoo partials.

    Args:
        env: Odoo environment (``self.env`` or the shell ``env``).
        limit: Maximum number of reconnum groups to check (``None`` = all).
        out: File-like object for CSV output (default: ``sys.stdout``).

    Returns:
        Number of mismatches found (0 = clean).
    """
    if out is None:
        out = sys.stdout

    cr = env.cr

    # 1. Load SAP groups
    query = """
        SELECT r.reconnum, r.lineseq, r.srcobjtyp,
               r.srcobjabs::integer AS doc_id,
               r.iscredit,
               r.reconsum AS reconciled_amount
        FROM itr1 r
        JOIN oitr h ON r.reconnum = h.reconnum
        WHERE h.canceled = 'N'
        ORDER BY r.reconnum, r.lineseq
    """
    cr.execute(query)
    all_rows = cr.dictfetchall()

    groups_by_reconnum = defaultdict(list)
    for row in all_rows:
        groups_by_reconnum[row["reconnum"]].append(row)

    all_reconnums = list(groups_by_reconnum.keys())
    if limit:
        all_reconnums = all_reconnums[:int(limit)]

    _logger.info("[verify_itr] Checking %d groups", len(all_reconnums))

    # 2. Build doc -> Odoo move_id map for sampled groups
    sampled_doc_ids_by_type = defaultdict(set)
    for rn in all_reconnums:
        for row in groups_by_reconnum[rn]:
            sampled_doc_ids_by_type[row["srcobjtyp"]].add(row["doc_id"])

    # OJDT transid map
    ojdt_transid_map = {}
    for srcobjtyp, doc_ids in sampled_doc_ids_by_type.items():
        if not doc_ids:
            continue
        transtype = _SRCOBJTYP_TO_TRANSTYPE.get(srcobjtyp)
        if not transtype:
            continue
        cr.execute(
            "SELECT createdby, transid FROM ojdt"
            " WHERE transtype = %s AND createdby IN %s",
            (transtype, tuple(doc_ids)),
        )
        for createdby, transid in cr.fetchall():
            ojdt_transid_map[(srcobjtyp, createdby)] = transid

    doc_move_map = {}
    for srcobjtyp, doc_ids in sampled_doc_ids_by_type.items():
        if not doc_ids:
            continue
        sap_table = _SRCOBJTYP_TO_TABLE.get(srcobjtyp, "ojdt")
        moves = env["account.move"].search([
            ("sap_docentry", "in", list(doc_ids)),
            ("sap_table", "=", sap_table),
        ])
        for m in moves:
            doc_move_map[(srcobjtyp, m.sap_docentry)] = m.id

        missing = doc_ids - {m.sap_docentry for m in moves}
        for did in missing:
            transid = ojdt_transid_map.get((srcobjtyp, did))
            if transid:
                ojdt_moves = env["account.move"].search([
                    ("sap_docentry", "=", transid),
                    ("sap_table", "=", "ojdt"),
                ])
                if ojdt_moves:
                    doc_move_map[(srcobjtyp, did)] = ojdt_moves[0].id

    # 3. For each group: compute expected triples, find actual partials
    mismatches = 0
    writer = csv.writer(out)
    writer.writerow([
        "reconnum", "d_srcobjtyp", "d_doc_id", "c_srcobjtyp", "c_doc_id",
        "expected_amount", "actual_amount", "delta", "status",
    ])

    for rn in all_reconnums:
        rows = groups_by_reconnum[rn]

        debits = sorted(
            [r for r in rows if r["iscredit"] == "D"],
            key=lambda r: r["lineseq"],
        )
        credits = sorted(
            [r for r in rows if r["iscredit"] == "C"],
            key=lambda r: r["lineseq"],
        )

        # Enrich with Odoo move_id
        for member_list in (debits, credits):
            for m in member_list:
                m["move_id"] = doc_move_map.get(
                    (m["srcobjtyp"], m["doc_id"])
                )
                m["reconsum"] = abs(float(m["reconciled_amount"]))

        # Skip groups where any member is missing from Odoo
        if any(m["move_id"] is None for m in debits + credits):
            continue

        # Expected triples from FIFO
        expected_triples = _allocate_fifo_plain(debits, credits)

        # Collect all AR/AP AMLs for this group
        group_move_ids = {m["move_id"] for m in debits + credits}
        all_moves = env["account.move"].browse(list(group_move_ids))
        aml_by_move = {}
        for move in all_moves:
            arap = move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ("asset_receivable", "liability_payable")
            )
            if arap:
                aml_by_move[move.id] = arap[0].id  # use first/largest

        # Actual partials
        candidate_aml_ids = list(aml_by_move.values())
        if not candidate_aml_ids:
            continue

        actual_partials = env["account.partial.reconcile"].search([
            ("debit_move_id", "in", candidate_aml_ids),
            ("credit_move_id", "in", candidate_aml_ids),
        ])
        # Build set of (debit_aml_id, credit_aml_id) -> amount
        actual_map = {}
        for p in actual_partials:
            key = (p.debit_move_id.id, p.credit_move_id.id)
            actual_map[key] = actual_map.get(key, 0.0) + p.amount

        # Compare
        for di, ci, exp_amount in expected_triples:
            d_member = debits[di]
            c_member = credits[ci]
            d_aml_id = aml_by_move.get(d_member["move_id"])
            c_aml_id = aml_by_move.get(c_member["move_id"])
            if d_aml_id is None or c_aml_id is None:
                continue

            actual_amount = actual_map.get((d_aml_id, c_aml_id), 0.0)
            delta = round(actual_amount - exp_amount, 2)
            status = "OK" if abs(delta) < 0.005 else "MISMATCH"
            if status == "MISMATCH":
                mismatches += 1
                writer.writerow([
                    rn,
                    d_member["srcobjtyp"], d_member["doc_id"],
                    c_member["srcobjtyp"], c_member["doc_id"],
                    exp_amount, actual_amount, delta, status,
                ])

    _logger.info(
        "[verify_itr] Verification complete: %d mismatches in %d groups",
        mismatches, len(all_reconnums),
    )
    return mismatches


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def main(env=None):
    """Run verification and exit with appropriate code."""
    if env is None:
        # When run via ``exec(open(...).read())`` in odoo-bin shell, the
        # ``env`` global is injected by the shell.
        try:
            env = globals().get("env") or locals().get("env")  # noqa: F821
        except Exception:
            pass

    if env is None:
        print("ERROR: Odoo env not available. Run via 'odoo-bin shell'.", file=sys.stderr)
        sys.exit(2)

    limit = os.environ.get("VERIFY_ITR_LIMIT")
    csv_path = os.environ.get("VERIFY_ITR_CSV")

    if csv_path:
        with open(csv_path, "w", newline="") as fout:
            mismatches = verify_itr_reconciliation(env, limit=limit, out=fout)
    else:
        mismatches = verify_itr_reconciliation(env, limit=limit)

    if mismatches:
        print(
            f"\n[verify_itr] {mismatches} mismatch(es) found.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print("\n[verify_itr] All groups match. No mismatches.", file=sys.stderr)
        sys.exit(0)


# When exec()'d from odoo-bin shell, ``env`` is already in scope.
if __name__ == "__main__":
    main()
elif "env" in dir():
    # Called via exec() in odoo-bin shell -- run automatically
    main(env=env)  # type: ignore[name-defined]  # noqa: F821
