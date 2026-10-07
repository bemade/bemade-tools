# Sage 50 to Odoo

ETL pipelines that read a **Sage 50 Canadian Edition** company file offline
and load it into Odoo: chart of accounts, partners, product categories,
products, pricelists, the open receivables and payables, and the opening
trial balance.

Built on `etl_framework`, like the SAP Business One and QuickBooks Online
importers in this repository. The difference is the source driver: Sage 50 is
MySQL, so this module needs `PyMySQL` where the others need `psycopg2`.

> **Install this on the machine running the migration, and nowhere else.**
> The Sage identifiers it writes live in `sage50_mapping`, which is the module
> that stays in production. Uninstall this one — and any client layer on top
> of it — *before* promoting the migrated database. A module flagged installed
> whose code is missing from the addons path breaks registry loading, so
> "absent" is not the same as "uninstalled".
>
> Keep `etl_framework` installed: it has no external dependencies and it owns
> `etl.import.report`, the take-on's execution history.

---

## Getting the data out of Sage

Sage 50 CA is not a proprietary flat-file format. The `<company>.SAJ`
directory **is** a MySQL 8.0 InnoDB data directory, and a Sage backup is a
plain Microsoft Cabinet archive containing it plus the small `.SAI`
companion. A full relational view is therefore always obtainable offline: no
Sage licence, no sysadmin password, no Windows machine, and no risk to the
client's live file.

```bash
./scripts/setup_sage_db.sh /path/to/backup.cab /path/to/workdir
```

That extracts the cabinet, downloads a matching `mysqld` into the work
directory, stages a copy of the datadir and starts the server on a unix
socket. Afterwards `./sage-mysql.sh {start|stop|dump}` manages it. Point the
`sage.database` record at `<workdir>/sagedb/run/mysql.sock`.

Three things the script encodes that are easy to get wrong:

- **The server version must match.** Recent Sage releases write the datadir
  with MySQL `8.0.27`; pointing a newer 8.0.x at it silently upgrades the data
  dictionary in place. `errorlog.txt` inside the `.SAJ` names the exact build.
- **`lower_case_table_names=1`.** The file was written on Windows. MySQL 8
  records the setting in the data dictionary and refuses to start on a
  mismatch.
- **`skip-grant-tables`.** Sage's own MySQL passwords are not available to us.
  It also implies `--skip-networking`, so the socket is the only way in —
  which is the behaviour you want for client data anyway.

**Read only, always.** Sage uses MySQL as a dumb store: no referential
integrity, record ids handed out from internal `nxtpids` counters, status
flags in place of constraints. A write corrupts the file in ways Sage will
not report until much later.

### Why not Sage's own export

`File → Import/Export → Export Records` emits master records as CSV and
**drops the tax breakdown on vendor bills** — which is exactly the detail an
open-payables take-on needs. It also cannot reach the GL entry behind a
document, which in most files *is* the line detail (see below).

---

## What the schema looks like

### The general ledger is generational

Sage keeps one header/line table pair per fiscal year and rolls them at each
year end:

| Generation | Header | Lines |
|---|---|---|
| Current | `tjourent` | `tjentact` |
| Prior | `tjently` | `tjentlya` |
| Archive 1 | `tjeh01` | `tjeah01` |
| Archive 2 | `tjeh02` | `tjeah02` |

Header: `dtJourDate`, `sSource` (document number), `sComment`, `nModule`,
`lRecId`. Lines: `lJEntId` → header, `lAcctId`, `dAmount`, `szComment`.

`nModule`: **0 = general journal, 1 = payables, 2 = receivables.** For modules
1 and 2 the header's `lRecId` is the vendor or customer id and `sSource` is
the document number, which is how a bill or an invoice is joined to its GL
entry.

An open item can predate the current year, so **any lookup must span all four
generations**. `tools.journal_entry` does.

### Amounts are signed in the account's natural side

`dAmount` is positive for a debit on accounts in sections 1, 5, 6, 7 and 9
(assets, cost of sales, expenses) and positive for a *credit* on sections 2, 3
and 4 (liabilities, equity, revenue). `tools.signed_amount` converts to
debit-positive, and it is the only correct way to add lines from different
sections together.

Two independent checks confirm the convention on a real file, and both are
worth running on a new one: for every used account
`sum(lines) == dYtc - dYts`, and every journal entry in every generation nets
to zero.

### Chart of accounts — `taccount`

`lId` is the 8-digit account number; `sName` / `sNameAlt` are the two
languages. `cFunc` gives the row's role: `H` heading, `S` subtotal, `T` total
are presentation-only, **`L` and `R` are the postable accounts**, and `X` is
the single "net income" pseudo-account that Odoo computes rather than stores.
Balances: `dYts` = opening for the current fiscal year, `dYtc` = current.
`sGifiCode` carries GIFI where the bookkeeper set it.

### Open receivables and payables

The document lives in `tcustr` / `tventr` and every application against it in
`tcustrdt` / `tventrdt`; the residual is the sum of `dAmount` over a
document's detail rows. Reconstructed that way both totals tie exactly to
their control accounts, which is what makes this — not the ageing report — the
source of truth.

**`bHasDetail` is routinely 0 on every document, and it does not mean the
document has no lines.** It says the *receivable record* carries no detail.
The item lines — item, quantity, unit price, account — live in `titrec` /
`titrline`, keyed on `sSource1` + `lVenCusId` rather than on the document's
own id, which is why joining from the document finds nothing. Documents coded
straight to an account have `titrline` rows too, with `lInventId` 0. Where a
document has no `titrec` row at all, the GL entry is the line source.

Application rows (`nTranType`): 0 and 8 are the document's own row (invoice
and credit note), 1 a receipt or payment, 2 an early-payment discount taken
on a receipt. `bReversed` rows come in cancelling pairs.

Two joins need care, and both bite silently:

- A document number is not unique. An invoice posted, corrected and reposted
  leaves two entries on the same `(sSource, nModule, lRecId)`, only the second
  of which is live. Disambiguated by matching the control-account amount.
- On the payable side a bill that was reversed and reposted leaves an exact
  mirror pair. Negating the expected control amount for that side — which
  looks like the obvious thing to do — matches the *reversal* every time.

---

## Known Sage data problems

These are properties of Sage, not of any one file, and the pipelines handle
all of them:

| Problem | Where it is handled |
|---|---|
| Trial balance out by a fixed amount, predating the oldest generation | `known_imbalance` on `sage.database` |
| Phone numbers typed into the customer *name* field | `res_partner_etl._split_phone_from_name` |
| Provinces and countries written free-hand, postal codes in the country column | `res_partner_etl._clean_province` / `_clean_country` |
| Units of measure free text, conversion factor 1.0 even where the units differ | `product_template_etl`, kept verbatim in `sage_unit` |
| Item categories unused, the dimension encoded in the revenue account instead | `product_category_etl._category_path` |
| No standard cost anywhere (`dBldCost` = 0) | logged as a warning, not imported as zero |
| Credit notes recorded as invoices with negative amounts | classified by the sign of the residual, not `nTranType` |
| Bills part taxable and part not | the taxable base is divided out of the tax, then matched or split |

---

## Two shapes of take-on

Which one runs is decided by **`history_start_date`** on the `sage.database`
record.

| | `history_start_date` empty | set to a fiscal year start |
|---|---|---|
| Odoo opens at | `cutover_date` | that year's first day |
| Profit and loss history | none | replayed in full |
| Counter-entry | posted | silent |
| GL replay | skipped | every entry from the start date |
| Strong check | partner-less control balance = 0 | trial balance ties to Sage, account by account |

Take the first when the source system keeps the year and closes it. Take the
second when the year's remaining bills and adjustments will be booked in
Odoo — the year then exists whole in neither system, and it can be closed
from neither, unless the history comes across.

`history_start_date` must be the **first day of a fiscal year Sage still
holds**. Anything else is refused rather than approximated, for the reason in
the next section.

---

## Closed documents and settlements

With a history start, **`import_closed_documents`** imports every invoice,
bill and credit note of the replayed years, not only the open ones — the same
builder, with the same item lines, taxes and repost disambiguation. A settled
document dated before the history start stays out: its balance and its
settlement are both inside the opening entry.

The applications against those documents do **not** go through
`sage.payment.importer` (one `account.payment` per application row): at this
scale that turns one receipt into ten payments, puts early-payment discounts
into the bank, and cannot place an application whose row names no bank.
Instead `sage.settlement.importer` posts each Sage receipt or payment entry as
itself — every line Sage posted — with its control line split into one line
per application: the document's partner, exactly the amount applied, and
reconciled with that document. Money applied to something not imported stays
on a remainder line. The ledger is Sage's by construction and the
reconciliation is Sage's pair by pair.

Two shapes do not fit that mould and are handled on their own:

- **Paid on the spot.** A bill or sale paid by cash or card is ONE Sage
  entry — the document and its payment together, with no receivable or
  payable line — numbered with the payment method (`Comptant` on a French
  file) and tied to the document only by a comment starting with
  `"<document number>, "`. The document importer finds it by that prefix
  (never a `CORR` reversal; of an original and its correction, the latest),
  builds the document from its expense and tax lines, and leaves the line on
  the account the money came from to the settlement importer, which posts it
  against the document.
- **Credit note applied to an invoice.** Sage moves the amount from one
  document to the other with no GL entry at all: two applications, `+x` and
  `-x`, same number and date. The two documents are reconciled with each
  other directly.

An application is traced to its entry on (number, module, tiers id, **date**):
a receipt number is not unique across years. The day's applications are then
matched to the day's entries by amount — the whole group to one entry (one
receipt, several documents), else each cheque's applications to one entry
(several payments to one partner on one day, all numbered "Comptant", told
apart only by `lChqId`), else each application alone. Of entries posted,
reversed and re-posted the same day, the latest matching one is live.

Finding a **document's** entry has the same traps, and they bite harder,
because a document that claims the wrong entry makes the replay drop a real
one — revenue twice, a receipt missing from the bank:

- An invoice and a later receipt can share a number for the same customer in
  different generations. The lookup prefers the entry dated on the
  document's own date, then the cash entry (above), and only then the same
  number on another date.
- A `CORR <number>` entry that mirrors an entry line for line cancels it.
  The cancelled original is dropped before choosing, so a sale on account
  re-entered as paid on the spot finds its correction, not the dead original.

Two smaller shapes: freight sits on the item record's header
(`titrec.dFreight`), not on an item line, and is added as a line on Sage's
linked freight account (`tlinkact.lAcNFrRev` / `lAcNFrExp`); and a bill with
nothing but tax on it (customs, import duties) keeps its tax lines as its
lines, untaxed. Documents worth 0.00 with nothing on them are skipped.

Two guards come with it. The document import refuses to run while any
imported product is valued in real time, because Odoo would add stock
valuation lines to every document carrying it; import while valuation is
periodic, then switch. And `action_check` adds two checks: every document's
Odoo residual against Sage's, and no stock-valuation, exchange-difference or
cash-basis entry created along the way.

## Open sales orders

`sage.sale.order.importer` brings the orders Sage has not cleared into Odoo
as confirmed orders, for what is still to deliver. Historical orders are not
imported: Sage keeps no link from an invoice to the order it filled
(`tcustr.lOrdId` is 0), and the invoices already carry the product history.

`tsoline.dOrdered` is ordered, `dQuantity` delivered so far, `dRemaining`
Sage's difference — meaningless when the order was taken by the piece and
delivered by weight. `remaining_quantity` decides instead: untouched lines
keep their ordered quantity; a weight line with a delivery is done (estimated
weight ordered, actual delivered); a count line part delivered keeps the
rest, unless its delivered quantity is fractional (it was weighed). Where
Sage's free-text unit is not the product's Odoo unit (`same_unit`), the
quantity stays in Sage's unit, the line says so and the import reports it:
the conversion factor is not in the file.

## The year-end roll has no journal entry

Sage does not sweep the profit and loss into equity with a closing entry at
year end. It rolls the generation and adjusts the retained-earnings balance
*silently*, with no journal entry anywhere in any generation.

Two consequences, and both bite:

- **There is nothing to filter out of a replay.** Looking for closing entries
  to skip is looking for something that does not exist. Replay every entry;
  Odoo derives each year's result itself.
- **An opening balance reconstructed by working backwards is wrong on exactly
  one account.** Subtracting each generation's movement from `taccount.dYts`
  is exact for every account except retained earnings, which comes out short
  by every year of profit the file still remembers. Each roll has to be undone
  by hand.

Verified exactly on a real file: the roll equals the jump in the retained
earnings account between `taccount.dYtcLY` and `taccount.dYts`, and that
account is the one Sage names in `tlinkact.lAcNretErn` — not a guess from the
account number.

---

## What the replay does not carry

Replayed lines post as plain journal items with **no tax grids**. Those
GST/QST periods were filed out of Sage; stamping grids on them would put
filed periods back onto Odoo's tax returns. Documents entered natively in
Odoo from the cutover forward carry their taxes normally.

Only the **receivable and payable lines carry a partner**. Sage's header
tiers is the *document's* counterparty and a general-journal entry has none
at all, so the control lines are the only ones where it is unambiguously
right — and the only ones where it matters, since a control account with
partner-less lines cannot be aged or reconciled.

Entries that became real invoices, bills or payments are skipped by
**entry**, recorded on the move as `sage_gl_entry_ref` — never by document
number. A document posted, corrected and reposted leaves two entries and only
one of them became the invoice; the other is real ledger history and must
replay.

The reference is `<generation table>:<row id>`, e.g. `tjourent:4812`, and the
generation half is load-bearing. **Sage restarts row ids in every fiscal
generation**, so `tjourent.lId` 4812 and `tjeh01.lId` 4812 are unrelated
entries. Keying on the bare id excludes an arbitrary entry from every other
year — which silently drops real ledger history and shows up only as a
balance-sheet account that will not tie.

---

## The three-entry take-on

This is the balances-only shape. With history imported the counter-entry
falls silent, because the revenue it exists to keep out of Odoo is precisely
what the replay is there to bring in.

The open documents, a counter-entry and an opening entry, in that order.

```
                            AR control      of which no partner
1. imported documents        100,000.00                    0.00
2. counter-entry            -100,000.00             -100,000.00
3. opening entry             100,000.00              100,000.00
TOTAL                        100,000.00                    0.00
```

The documents are re-entered as real invoices — real accounts, real taxes,
real dates — because otherwise they cannot be reconciled against payments and
the ageing is wrong. The counter-entry mirrors every line of them, so their
revenue is not reported a second time. The opening entry then carries Sage's
own trial balance, control accounts included.

The control lines of entries 2 and 3 carry **no partner**, and that is the
point: it leaves a partner-less balance of exactly zero on each control
account, which is the check that catches a document that failed to import.
A trial balance that ties does not — a missing document is mirrored away by
the counter-entry and never appears in it at all.

The opening entry balances against a transition account, which must then read
`known_imbalance` and nothing else. `action_check` on `sage.database` runs all
three checks, plus one for Odoo's **Invoicing Switch Threshold**, which
cancels posted entries before its date with a raw SQL sweep and no chatter.

With history imported, the partner-less check stops meaning anything and is
reported as information rather than as a verdict: the opening entry carries
the control balances as they stood at the *start* of the replay, with no
partner, and they decay toward zero only as the replayed payments settle the
documents that were open back then. `action_check` swaps in the trial balance
against Sage instead, **reported per account** — a total that ties while two
accounts are wrong in opposite directions is exactly the failure worth
catching.

---

## Writing a client layer

Everything client-specific is a hook. A client layer is a small module
depending on `sage50_to_odoo` that `_inherit`s the pipelines it needs to
change and calls `super()`.

| Hook | On | What it decides |
|---|---|---|
| `_account_type_overrides` | `sage.account.importer` | Accounts whose Odoo type cannot be read off their number — **including the two control accounts**, which everything downstream keys off |
| `_range_rules` | `sage.account.importer` | Only for a chart that departs from Sage's own sectioning |
| `_tax_account_aliases` | `sage.account.importer` | Sage's tax accounts mapped onto the localisation's, so the tax report reads one set |
| `_category_path` | `sage.product.category.importer` | How to read a revenue account name as a category path |
| `_uom_xmlid`, `_sale_tax_xmlid_suffix` | `sage.product.importer` | Unit mapping; the product-level default sales tax |
| `_tax_account_map`, `_tax_rate_candidates`, `_tax_combinations` | `sage.open.item.importer` | Which GL account holds which tax, at what rate, and which Odoo tax that implies |

Example:

```python
class AcmeSageAccountImporter(models.AbstractModel):
    _inherit = "sage.account.importer"

    def _account_type_overrides(self):
        return super()._account_type_overrides() | {
            11000000: "asset_receivable",
            21000000: "liability_payable",
            10400000: "asset_cash",
        }
```

One trap worth naming, because it has bitten more than one migration: an
account holding profit Sage has already closed out is plain `equity`, **never**
`equity_unaffected`. Odoo keeps exactly one `equity_unaffected` account per
company, resolves it by type, and derives both the current-year and
previous-years unallocated-earnings lines of the balance sheet from it. A
second one makes that resolution ambiguous and double-counts the balance into
a line that already reports it.
