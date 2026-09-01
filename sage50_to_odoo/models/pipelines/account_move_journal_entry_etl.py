"""Sage 50 general ledger -> Odoo `account.move`.

Replays every journal entry Sage still holds, one Odoo move per Sage entry,
at Sage's own date. This is what lets a fiscal year be *closed* in Odoo
rather than merely opened there: the accountant needs a real profit and
loss, not a balance sheet with a year of history missing behind it.

Sage keeps one header/line table pair per fiscal year and rolls them at each
year end, so "the general ledger" is several tables — see
`tools.generation_spans`. Every generation from the current one back to
`history_start_date` is imported whole.

**Every entry is replayed, including the ones behind the open documents.**
The general ledger is the truth here, and nothing is left out of it. The
open receivables and payables are also re-created as real invoices and bills
by `sage.open.item.importer`, because an ageing report and a reconciliation
need documents rather than journal lines — but those documents are mirrored
away in the general ledger by `sage.counter.entry.importer`, so they add
subledger detail and no accounting.

Substituting the document for the entry behind it was tried and is wrong.
Odoo builds an invoice from item lines onto revenue accounts, while Sage may
have booked the same document somewhere else entirely: on the file this was
built against, twenty open receivables totalling $133,894.80 sit against the
unbilled-goods-received account rather than against revenue. Swap the entry
for the invoice and that account loses its balance while revenue gains one,
and no exclusion rule can repair it. Replay everything; neutralise the
documents.

**No taxes.** Replayed lines post as plain journal items with no tax grids.
Those GST/QST periods were filed out of Sage years ago; stamping grids on
them would put filed periods back onto Odoo's tax returns. Documents entered
natively in Odoo from the cutover forward carry their taxes normally.

**No closing entries to skip.** Sage does not sweep the profit and loss into
equity with a journal entry at year end — it rolls the generation and adjusts
the retained-earnings balance silently. There is consequently nothing here to
filter, and the whole of the correction lives in
`sage.opening.balance.importer`, which has to undo those rolls to reconstruct
an opening balance. Odoo then re-derives each year's result itself.
"""

import logging
from collections import defaultdict

from odoo import _, models
from odoo.exceptions import UserError
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)

#: How many moves to create before flushing. Sage files run to tens of
#: thousands of entries and Odoo's in-memory cache grows with every posted
#: move, so the load is committed in batches rather than as one transaction.
BATCH_SIZE = 500


@ETL.pipeline(
    target_model="account.move",
    importer_name="sage.journal.entry.importer",
    sap_source="tjourent",
    # The opening balance sets the ground the replay stands on, and the
    # documents, payments and counter-entry must all be in before the
    # exclusion set can be read off them.
    depends_on=[
        "sage.opening.balance.importer",
        "sage.counter.entry.importer",
    ],
    allow_multiprocessing=False,
)
class SageJournalEntryImporter(models.AbstractModel):
    _name = "sage.journal.entry.importer"
    _description = "Sage 50 General Ledger Replay"

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    @ETL.extract("tjourent")
    def extract_entries(self, ctx: ETLContext) -> dict:
        start = ctx.env["sage.opening.balance.importer"].history_start(ctx)
        spans = [
            span for span in tools.generation_spans(ctx.cr)
            if span["start"] >= start
        ]
        if not spans:
            raise UserError(_(
                "No fiscal generation starts on or after %(start)s, so there "
                "is no general-ledger history to replay.", start=start,
            ))

        entries = []
        for span in spans:
            headers = {
                row["lId"]: row
                for row in tools.query(
                    ctx.cr,
                    f"""select lId, dtJourDate, nModule, sSource, sComment,
                               lRecId
                          from {span['header']}
                         where dtJourDate >= %s""",
                    (start,),
                )
            }
            if not headers:
                continue
            lines = defaultdict(list)
            for row in tools.query(
                ctx.cr,
                f"""select lJEntId, nLineNum, lAcctId, dAmount, szComment
                      from {span['lines']}
                     order by lJEntId, nLineNum""",
            ):
                if row["lJEntId"] in headers:
                    lines[row["lJEntId"]].append(row)

            for entry_id, header in headers.items():
                if not lines.get(entry_id):
                    # A header with no lines is not a posting. Sage leaves
                    # them behind; they carry no ledger effect either way.
                    continue
                entries.append({
                    "sage_gl_entry_ref": tools.entry_ref(
                        span["header"], entry_id
                    ),
                    "generation": span["header"],
                    "date": header["dtJourDate"].strftime("%Y-%m-%d"),
                    "module": header["nModule"],
                    "source": (header["sSource"] or "").strip(),
                    "comment": (header["sComment"] or "").strip(),
                    "rec_id": header["lRecId"] or 0,
                    "lines": [
                        {
                            "account": row["lAcctId"],
                            # Debit-positive, which is what a journal item
                            # wants. Sage stores the amount signed in the
                            # account's own natural side.
                            "balance": round(tools.signed_amount(
                                row["lAcctId"], row["dAmount"]
                            ), 2),
                            "label": (row["szComment"] or "").strip(),
                        }
                        for row in lines[entry_id]
                    ],
                })
        entries.sort(
            key=lambda entry: (entry["date"], entry["sage_gl_entry_ref"])
        )
        _logger.info(
            "Sage general ledger: %s entries from %s generation(s) on or "
            "after %s.", len(entries), len(spans), start,
        )
        return {"start": start, "entries": entries}

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------
    @ETL.transform()
    def transform_entries(self, ctx: ETLContext, extracted: dict) -> dict:
        payload = extracted["extract_entries"]
        unbalanced = [
            entry["sage_gl_entry_ref"] for entry in payload["entries"]
            if abs(round(sum(
                line["balance"] for line in entry["lines"]
            ), 2)) > 0.005
        ]
        if unbalanced:
            # Every entry in every generation nets to zero under this sign
            # convention on a healthy file. One that does not means the
            # convention is wrong for some account section, which would
            # corrupt every figure in the replay rather than just this entry.
            raise UserError(_(
                "%(count)s Sage journal entries do not net to zero, starting "
                "with %(ids)s. The sign convention is wrong somewhere — stop "
                "and check before importing anything.",
                count=len(unbalanced), ids=unbalanced[:10],
            ))
        return payload

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    def _partner_map(self, ctx: ETLContext) -> dict:
        """(module, Sage tiers id) -> Odoo partner id.

        A vendor and a customer can share an id in Sage — they are numbered
        from separate counters — so the module has to be part of the key.
        """
        mapping = {}
        Partner = ctx.env["res.partner"]
        for partner in Partner.search([("sage_customer_id", "!=", 0)]):
            mapping[(tools.MODULE_RECEIVABLE, partner.sage_customer_id)] = \
                partner.id
        for partner in Partner.search([("sage_vendor_id", "!=", 0)]):
            mapping[(tools.MODULE_PAYABLE, partner.sage_vendor_id)] = partner.id
        return mapping

    @ETL.load()
    def load_entries(self, ctx: ETLContext, transformed: dict) -> None:
        payload = transformed["transform_entries"]
        company_id = ctx.get_config("company_id")
        journal_id = ctx.get_config("journal_id")
        if not journal_id:
            raise UserError(_("The general-ledger replay needs a journal."))

        Move = ctx.env["account.move"]
        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        partners = self._partner_map(ctx)
        # Resolved once. Browsing the account per line costs a query for
        # every one of the tens of thousands of lines a full replay posts.
        control_accounts = set(ctx.env["account.account"].search([
            ("account_type", "in", ("asset_receivable", "liability_payable")),
            ("company_ids", "in", company_id),
        ]).ids)
        already = set(Move.search([
            ("sage_gl_entry_ref", "!=", False),
            ("company_id", "=", company_id),
            ("journal_id", "=", journal_id),
        ]).mapped("sage_gl_entry_ref"))

        posted = skipped = 0
        batch = ctx.env["account.move"]
        for entry in payload["entries"]:
            entry_ref = entry["sage_gl_entry_ref"]
            if entry_ref in already:
                skipped += 1
                continue

            partner_id = partners.get((entry["module"], entry["rec_id"]))
            lines = []
            for line in entry["lines"]:
                account_id = accounts.get(line["account"])
                if not account_id:
                    raise UserError(_(
                        "No Odoo account for Sage %(sage_id)s, used by "
                        "journal entry %(entry)s. Import the chart of "
                        "accounts first.",
                        sage_id=line["account"], entry=entry_ref,
                    ))
                balance = line["balance"]
                lines.append((0, 0, {
                    "name": line["label"] or entry["comment"] or "/",
                    "account_id": account_id,
                    # Only the receivable and payable lines carry the
                    # tiers. Sage's header tiers is the *document's*
                    # counterparty and a general-journal entry has none at
                    # all, so the control lines are the only ones where it
                    # is unambiguously right — and the only ones where it
                    # matters, since a control account with partner-less
                    # lines cannot be aged or reconciled.
                    "partner_id": (
                        partner_id
                        if partner_id and account_id in control_accounts
                        else False
                    ),
                    "debit": balance if balance > 0 else 0.0,
                    "credit": -balance if balance < 0 else 0.0,
                }))

            with ctx.skippable(source_ref=entry["source"] or entry_ref):
                move = Move.create({
                    "move_type": "entry",
                    "journal_id": journal_id,
                    "date": entry["date"],
                    "ref": self._reference(entry),
                    "narration": entry["comment"] or False,
                    "sage_gl_entry_ref": entry_ref,
                    "company_id": company_id,
                    "line_ids": lines,
                })
                batch |= move
                posted += 1
                ctx.report.success()

            if len(batch) >= BATCH_SIZE:
                batch.action_post()
                ctx.env.cr.commit()
                ctx.env.invalidate_all()
                batch = ctx.env["account.move"]
                _logger.info("Sage general ledger: %s entries posted.", posted)

        if batch:
            batch.action_post()
            ctx.env.cr.commit()

        _logger.info(
            "Sage general ledger: %s entries posted, %s already present.",
            posted, skipped,
        )

    def _reference(self, entry: dict) -> str:
        source = entry["source"]
        return f"Sage {source}" if source else _("Sage journal entry")
