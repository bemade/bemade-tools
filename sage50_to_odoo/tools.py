"""Reading a Sage 50 Canadian Edition company file.

The `<company>.SAJ` directory *is* a MySQL 8.0 data directory, so a company
file can be served read-only from a userland `mysqld` and queried like any
other relational source. `scripts/setup_sage_db.sh` does that; everything
here assumes it has been done.

What lives in this file is the handful of facts about Sage's schema that a
query cannot discover for itself: how ledger amounts are signed, and how the
general ledger is split across fiscal generations.

**Read only, always.** Sage uses MySQL as a dumb store — no referential
integrity, record ids handed out from internal counters, status flags in
place of constraints. A write corrupts the file in ways Sage will not report
until much later.
"""

from __future__ import annotations

from typing import Any

#: Header/line table pairs, one per fiscal-year "generation", newest first.
#: Sage rolls the general ledger into a new pair at each year end and keeps
#: the previous ones under fixed names, so an entry older than the current
#: year is not in `tjourent` at all.
GENERATIONS = (
    ("tjourent", "tjentact"),
    ("tjently", "tjentlya"),
    ("tjeh01", "tjeah01"),
    ("tjeh02", "tjeah02"),
)

#: `tjourent.nModule`. For 1 and 2 the header's `lRecId` is the vendor or
#: customer id and `sSource` is the document number, which is how a bill or
#: an invoice is joined to the GL entry behind it.
MODULE_GENERAL, MODULE_PAYABLE, MODULE_RECEIVABLE = 0, 1, 2

#: Account-number sections whose natural side is debit: assets, cost of
#: sales and expenses. Sage's chart is strictly sectioned by leading digit —
#: 1 assets, 2 liabilities, 3 equity, 4 revenue, 5 cost of sales, 6-9
#: expenses — and the sign convention below depends on it.
DEBIT_SECTIONS = (1, 5, 6, 7, 9)

#: `taccount.cFunc` values that are real, postable accounts. `H`, `S` and `T`
#: are headings, subtotals and totals that exist only to draw the report;
#: `X` is the single "Net income" pseudo-account, which Odoo computes.
POSTABLE_FUNCS = ("L", "R")
NON_POSTABLE_FUNCS = ("H", "S", "T", "X")


def query(cr: Any, sql: str, args=None) -> list[dict]:
    """Run a query against the Sage cursor and return dict rows."""
    cr.execute(sql, args)
    return cr.dictfetchall()


def is_debit_natural(account_id: int) -> bool:
    """True when a positive `dAmount` on this account means a debit."""
    return account_id // 10_000_000 in DEBIT_SECTIONS


def signed_amount(account_id: int, amount: float) -> float:
    """Convert Sage's natural-side amount to a debit-positive amount.

    Sage stores ledger amounts signed in each account's *natural* side rather
    than debit-positive, so this is the only correct way to add lines from
    different sections of the chart together. Verified rather than assumed,
    by two independent checks that both pass exactly on a real file: for
    every used account `sum(lines) == dYtc - dYts`, and every journal entry
    in every generation nets to zero under this convention.
    """
    return amount if is_debit_natural(account_id) else -amount


#: The same conversion as a SQL fragment, for aggregates. Expects the account
#: table aliased as `a`.
SQL_DEBIT_SIGN = (
    "case when floor(a.lId/10000000) in (1,5,6,7,9) then 1 else -1 end"
)


def journal_entry(cr: Any, source: str, module: int, rec_id: int,
                  control_account: int | None = None,
                  expected_control: float | None = None) -> list[dict]:
    """The lines of the GL entry behind one AR/AP document.

    Searched across every fiscal generation, because an open item can predate
    the current year and some do — a credit note left open across a year end
    has its entry in `tjeh01`, not `tjourent`.

    A document number is not unique. An invoice that was posted, corrected
    and reposted leaves two entries on the same `(sSource, nModule, lRecId)`,
    only the second of which is live, and the amounts differ. Pass
    `control_account` and `expected_control` — the document's original amount
    — to pick the entry that actually matches. Without them the newest entry
    wins, which is the right guess but only a guess.

    Returns [] when no entry is found in any generation.
    """
    for header, lines in GENERATIONS:
        rows = query(
            cr,
            f"""select j.lId, j.dtJourDate, j.sComment, l.nLineNum,
                       l.lAcctId, l.dAmount, l.szComment
                  from {header} j
                  join {lines} l on l.lJEntId = j.lId
                 where j.sSource = %s and j.nModule = %s and j.lRecId = %s
                 order by j.lId, l.nLineNum""",
            (source, module, rec_id),
        )
        if not rows:
            continue
        by_entry: dict[int, list[dict]] = {}
        for row in rows:
            by_entry.setdefault(row["lId"], []).append(row)
        if len(by_entry) == 1:
            return next(iter(by_entry.values()))
        if control_account is not None and expected_control is not None:
            for entry_lines in by_entry.values():
                total = sum(
                    line["dAmount"] for line in entry_lines
                    if line["lAcctId"] == control_account
                )
                if abs(total - expected_control) < 0.005:
                    return entry_lines
        # Fall back to the last entry posted, which is the correction.
        return by_entry[max(by_entry)]
    return []


def linked_accounts(cr: Any) -> dict:
    """Sage's `tlinkact` row: the accounts Sage itself nominates for a role.

    Worth reading rather than hardcoding or guessing from account numbers.
    The two that matter to a take-on are `lAcNretErn` (retained earnings)
    and `lAcNcurErn` (the current-earnings pseudo-account, which is `cFunc`
    `X` and which Odoo computes rather than stores).
    """
    rows = query(cr, "select * from tlinkact limit 1")
    return rows[0] if rows else {}


def generation_spans(cr: Any) -> list[dict]:
    """Every populated fiscal generation, newest first, with its date span.

    Sage names the generations by position rather than by year, so the only
    way to learn which year a table holds is to look. Empty generations are
    dropped: a file that has not yet rolled twice still has the archive
    tables, just with nothing in them.
    """
    spans = []
    for index, (header, lines) in enumerate(GENERATIONS):
        row = query(
            cr,
            f"""select count(*) as entries,
                       min(dtJourDate) as first_date,
                       max(dtJourDate) as last_date
                  from {header}""",
        )[0]
        if not row["entries"]:
            continue
        spans.append({
            "index": index,
            "header": header,
            "lines": lines,
            "entries": row["entries"],
            "start": row["first_date"].strftime("%Y-%m-%d"),
            "end": row["last_date"].strftime("%Y-%m-%d"),
        })
    return spans


def generation_movement(cr: Any, span: dict) -> dict:
    """Sage account number -> that generation's net movement, natural side.

    Natural side rather than debit-positive because the caller combines it
    with `taccount.dYts`, which is stored the same way. Convert with
    `signed_amount` only when adding accounts from different sections
    together.
    """
    return {
        row["lAcctId"]: round(row["amount"], 2)
        for row in query(
            cr,
            f"""select l.lAcctId, sum(l.dAmount) as amount
                  from {span['header']} j
                  join {span['lines']} l on l.lJEntId = j.lId
                 group by l.lAcctId""",
        )
    }


def net_income(movement: dict, postable: set) -> float:
    """A generation's net income, debit-positive (so a profit is positive).

    Sage does NOT post a closing entry that sweeps the profit and loss into
    equity. It rolls the generation and moves the result into retained
    earnings as a silent balance adjustment, with no journal entry anywhere.
    So there is nothing to exclude from a replay — but an opening balance
    reconstructed by working backwards through the generations has to undo
    each roll by hand, or retained earnings comes out short by every year of
    profit the file still remembers.

    Verified exactly on a real file: this figure equals the jump in the
    `lAcNretErn` account between `taccount.dYtcLY` and `taccount.dYts`.
    """
    return -round(sum(
        signed_amount(account_id, amount)
        for account_id, amount in movement.items()
        if account_id in postable and account_id // 10_000_000 >= 4
    ), 2)
