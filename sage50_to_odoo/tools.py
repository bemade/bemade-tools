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

#: Generation header table -> its line table, for a lookup by name.
LINES_FOR = {header: lines for header, lines in GENERATIONS}

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
                  expected_control: float | None = None,
                  day: Any = None, strict: bool = False,
                  exact_amount: bool = False) -> list[dict]:
    """The lines of the GL entry behind one AR/AP document.

    Searched across every fiscal generation, because an open item can predate
    the current year and some do — a credit note left open across a year end
    has its entry in `tjeh01`, not `tjourent`.

    A document number is not unique. An invoice that was posted, corrected
    and reposted leaves two entries on the same `(sSource, nModule, lRecId)`,
    only the second of which is live, and the amounts differ. Pass
    `control_account` and `expected_control` — the document's original amount
    — to pick the entry that actually matches. And an invoice and a later
    receipt can share a number for the same customer in different
    generations: pass the document's date as `day` so the entry dated that day
    wins over whichever generation happens to be searched first. See
    `choose_entry`.

    Returns [] when no entry is found in any generation.
    """
    entries, reversals = [], []
    reversal_source = f"CORR {source}"
    for header, lines in GENERATIONS:
        rows = query(
            cr,
            f"""select j.lId, j.sSource, j.dtJourDate, j.sComment, l.nLineNum,
                       l.lAcctId, l.dAmount, l.szComment
                  from {header} j
                  join {lines} l on l.lJEntId = j.lId
                 where j.sSource in (%s, %s) and j.nModule = %s
                   and j.lRecId = %s
                 order by j.lId, l.nLineNum""",
            (source, reversal_source, module, rec_id),
        )
        by_entry: dict[int, list[dict]] = {}
        for row in rows:
            # `lId` restarts in every generation, so it identifies an entry
            # only alongside the table it came from. Callers that remember an
            # entry MUST remember both -- see `entry_ref`.
            row["generation"] = header
            by_entry.setdefault(row["lId"], []).append(row)
        for entry in by_entry.values():
            if entry[0]["sSource"] == reversal_source:
                reversals.append(entry)
            else:
                entries.append(entry)
    entries = drop_reversed(entries, reversals)
    return choose_entry(entries, day, control_account, expected_control,
                        strict=strict, exact_amount=exact_amount)


def drop_reversed(entries: list[list[dict]],
                  reversals: list[list[dict]]) -> list[list[dict]]:
    """Remove the entries that a `CORR` entry cancels.

    Sage corrects a posted document by reversing it — a `CORR <number>` entry
    that mirrors it line for line — and posting the correction, sometimes
    under another number (a sale on account re-entered as paid on the spot
    becomes "Comptant"). The reversed original is not the document's entry
    however well its number and date match. Matched on the mirrored lines
    rather than on the reversal's comment, which is in the file's language.
    Each reversal cancels one entry.
    """
    def signature(entry, sign=1):
        return sorted(
            (line["lAcctId"], round(sign * line["dAmount"], 2))
            for line in entry
        )

    remaining = list(entries)
    for reversal in reversals:
        mirrored = signature(reversal, -1)
        for entry in remaining:
            if signature(entry) == mirrored:
                remaining.remove(entry)
                break
    return remaining


def choose_entry(entries: list[list[dict]], day: Any = None,
                 control_account: int | None = None,
                 expected_control: float | None = None,
                 strict: bool = False,
                 exact_amount: bool = False) -> list[dict]:
    """Pick one entry out of several sharing a document number.

    `entries` is a list of entries (each a list of its lines), newest
    generation first. With `day`, only the entries dated that day are
    considered, if there are any — and with `strict`, if there are none the
    answer is none, rather than an entry from another day. Then an entry
    whose control amount equals `expected_control`, in any generation (the
    newest such, the latest posted within it) — and with `exact_amount`,
    nothing else will do. Failing that, within the newest generation left:
    a single entry is the answer; otherwise the one whose control-account
    amount equals `expected_control`; otherwise the last one posted, which is
    the correction.
    """
    if not entries:
        return []
    if day is not None:
        if hasattr(day, "date"):
            day = day.date()
        dated = [
            entry for entry in entries
            if entry[0]["dtJourDate"] and entry[0]["dtJourDate"].date() == day
        ]
        if dated:
            entries = dated
        elif strict:
            return []
    # An entry whose control amount IS the document's, in any generation,
    # beats the newest generation: a receipt sharing the number sits on the
    # opposite side of the control account and never matches.
    if control_account is not None and expected_control is not None:
        matching = [
            entry for entry in entries
            if abs(sum(
                line["dAmount"] for line in entry
                if line["lAcctId"] == control_account
            ) - expected_control) < 0.005
        ]
        if matching:
            first = matching[0][0]["generation"]
            return max(
                (entry for entry in matching
                 if entry[0]["generation"] == first),
                key=lambda entry: entry[0]["lId"],
            )
        if exact_amount:
            return []
    newest = entries[0][0]["generation"]
    candidates = [entry for entry in entries if entry[0]["generation"] == newest]
    if len(candidates) == 1:
        return candidates[0]
    if control_account is not None and expected_control is not None:
        for entry in candidates:
            total = sum(
                line["dAmount"] for line in entry
                if line["lAcctId"] == control_account
            )
            if abs(total - expected_control) < 0.005:
                return entry
    # Fall back to the last entry posted, which is the correction.
    return max(candidates, key=lambda entry: entry[0]["lId"])


def pick_cash_entry(headers: list[dict], number: str) -> dict | None:
    """The live entry behind a document paid on the spot, or None.

    Sage posts a bill or a sale paid by cash or card as ONE entry — the
    document and its payment together, with no receivable or payable line —
    numbered with the payment method ("Comptant") rather than the document.
    What ties it to the document is the comment, which starts with
    "<document number>, ".

    A corrected document leaves the original, a `CORR` reversal of it and the
    correction, all with the same comment prefix. The reversal is never the
    document's entry; of the rest the latest is the live one, the same rule
    `journal_entry` falls back on.
    """
    prefix = f"{number},"
    live = [
        header for header in headers
        if (header.get("sComment") or "").startswith(prefix)
        and not (header.get("sSource") or "").startswith("CORR")
    ]
    if not live:
        return None
    return max(live, key=lambda header: header["lId"])


def infer_payment_accounts(lines: list[dict], tax_accounts,
                           control_amount: float) -> set:
    """The account(s) a cash document was paid from, read off its entry.

    For when the application row and its cheque header name no bank account.
    In a paid-on-the-spot entry the payment sits exactly where the control
    line would have been: on the control's side, and worth the document's
    control amount. So: the non-tax lines on that side, if together they
    make exactly that amount; else the single line that does; else nothing —
    an ambiguous entry is not guessed at.

    `lines` are debit-positive (`{"account", "balance"}`), `control_amount`
    is the document's control amount, debit-positive.
    """
    same_side = [
        line for line in lines
        if line["account"] not in tax_accounts
        and line["balance"] * control_amount > 0
    ]
    if same_side and abs(
        sum(line["balance"] for line in same_side) - control_amount
    ) < 0.01:
        return {line["account"] for line in same_side}
    exact = [
        line for line in same_side
        if abs(line["balance"] - control_amount) < 0.01
    ]
    if len(exact) == 1:
        return {exact[0]["account"]}
    return set()


def cash_entry(cr: Any, number: str, module: int, rec_id: int,
               date: Any) -> list[dict]:
    """The lines of the single entry behind a document paid on the spot.

    Same shape as `journal_entry`. Searched by tiers, module and date, then
    picked by comment prefix — see `pick_cash_entry`. Returns [] when there
    is none, which is the ordinary case: most documents are not cash.
    """
    for header, lines in GENERATIONS:
        headers = query(
            cr,
            f"""select lId, sSource, sComment from {header}
                 where nModule = %s and lRecId = %s and dtJourDate = %s
                   and sComment like %s""",
            (module, rec_id, date, f"{number},%"),
        )
        chosen = pick_cash_entry(headers, number)
        if not chosen:
            continue
        rows = query(
            cr,
            f"""select j.lId, j.dtJourDate, j.sComment, l.nLineNum,
                       l.lAcctId, l.dAmount, l.szComment
                  from {header} j
                  join {lines} l on l.lJEntId = j.lId
                 where j.lId = %s
                 order by l.nLineNum""",
            (chosen["lId"],),
        )
        for row in rows:
            row["generation"] = header
        return rows
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


def entry_ref(generation: str, entry_id: int) -> str:
    """A GL entry's identity, unique across the whole file.

    `tjourent.lId`, `tjently.lId` and `tjeh01.lId` are separate id spaces:
    the same integer names a different entry in each generation. Anything
    that records "which entry did this come from" and is later compared
    against another generation must use this, not the bare id, or entries
    are matched to entries they have nothing to do with.
    """
    return f"{generation}:{entry_id}"
