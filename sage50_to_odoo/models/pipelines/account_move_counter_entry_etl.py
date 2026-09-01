"""Neutralise the profit and loss the imported open documents created.

The open documents have to be re-entered as real invoices — real revenue and
expense accounts, real taxes, real dates — or they cannot be reconciled
against payments and the ageing is wrong. But they are historical: their
revenue was earned and reported in Sage, and posting them into Odoo reports
it a second time. This entry mirrors every line of them, so the general
ledger keeps the receivable and the payable and nothing else.

The receivable and payable lines here carry **no partner**. Together with the
opening entry, which carries Sage's own control balances (also with no
partner), that leaves a partner-less balance of exactly zero on each control
account. That, and not the trial balance, is the check that catches a
document which failed to import: a missing document is mirrored away here and
never appears in the trial balance at all.

> This is a deliberate departure from the usual "exclude the control accounts
> from the opening entry" recipe. That recipe is right for a take-on with no
> counter-entry. Alongside one it double-counts nothing but loses the
> partner-less check, which is the stronger control.
"""

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

CONTROL_TYPES = ("asset_receivable", "liability_payable")

#: Liquidity accounts are mirrored like everything else. An imported payment
#: debits the bank, and the opening entry carries the bank balance Sage
#: reported — which already includes that receipt. Mirroring the payment is
#: what stops the two counting twice.

REFERENCE = "Sage 50 take-on — reversal of the imported documents"


@ETL.pipeline(
    target_model="account.move",
    importer_name="sage.counter.entry.importer",
    depends_on=["sage.open.item.importer", "sage.payment.importer"],
    allow_multiprocessing=False,
)
class SageCounterEntryImporter(models.AbstractModel):
    _name = "sage.counter.entry.importer"
    _description = "Sage 50 Take-on Counter-entry"

    def _reference(self, year_end=None) -> str:
        return f"{REFERENCE} ({year_end})" if year_end else REFERENCE

    @ETL.extract("account_move")
    def extract_documents(self, ctx: ETLContext) -> list:
        """Reads Odoo, not Sage: the documents to mirror are the ones the
        open-item pipeline just posted, and only Odoo knows how they came
        out once its own tax engine had run.

        Runs in both shapes of take-on, and for the same reason in each:
        a document re-created as an invoice must not also report its own
        revenue. With history imported that reason is stronger, not weaker.
        The general-ledger replay is a faithful copy of Sage, so the
        document's accounting is ALREADY in Odoo, posted to the accounts
        Sage actually used. A re-derived invoice cannot be substituted for
        the entry behind it: Odoo builds it from item lines onto revenue
        accounts, while Sage may have booked it somewhere else entirely --
        on this client, twenty open receivables sit against the
        unbilled-goods-received account rather than against revenue.
        Mirroring the documents away and letting the replay carry the
        ledger is what makes every account tie by construction.
        """
        return ctx.env["account.move"].search([
            "|",
            ("sage_doc_id", "!=", 0),
            ("sage_application_id", "!=", 0),
            ("company_id", "=", ctx.get_config("company_id")),
            ("state", "=", "posted"),
        ]).ids

    @ETL.transform()
    def transform_lines(self, ctx: ETLContext, extracted: dict) -> list:
        documents = ctx.env["account.move"].browse(
            extracted["extract_documents"]
        )
        lines = []
        for line in documents.line_ids:
            if line.display_type in ("line_section", "line_note"):
                continue
            is_control = line.account_id.account_type in CONTROL_TYPES
            lines.append({
                "date": line.date,
                "name": _("Reversal of %s", line.move_id.ref or ""),
                "account_id": line.account_id.id,
                # No partner on the control lines: that is what makes the
                # partner-less balance a usable check.
                "partner_id": False if is_control else line.partner_id.id,
                "debit": line.credit,
                "credit": line.debit,
                "tax_tag_ids": self._mirrored_tax_tags(line).ids,
            })
        return lines

    def _mirrored_tax_tags(self, line):
        """The same tax grids as a line, but on the opposite side.

        A tax's invoice and refund repartitions carry mirrored grid tags —
        that is what a credit note uses — so the counter-entry takes the
        refund side of whatever repartition line the original was stamped
        from. The two repartitions are built in the same order, so matching by
        position within the same repartition type is exact.

        Tax grids are read by the tax report and take no part in the move
        balance, so stamping them cannot disturb a balanced ledger. Lines
        built by hand get no propagation, which is why this is done explicitly
        rather than left to the tax engine.
        """
        repartition = line.tax_repartition_line_id
        if not repartition:
            return line.tax_tag_ids
        tax = repartition.tax_id
        same_type = tax.invoice_repartition_line_ids.filtered(
            lambda r, t=repartition.repartition_type: r.repartition_type == t
        )
        mirrored = tax.refund_repartition_line_ids.filtered(
            lambda r, t=repartition.repartition_type: r.repartition_type == t
        )
        if repartition not in same_type:
            return line.tax_tag_ids
        index = list(same_type).index(repartition)
        if index >= len(mirrored):
            return line.tax_tag_ids
        return mirrored[index].tag_ids

    @ETL.load()
    def load_counter_entry(self, ctx: ETLContext, transformed: dict) -> None:
        journal_id = ctx.get_config("journal_id")
        if not journal_id:
            raise UserError(
                _("Pick a journal for the take-on entries. A dedicated one "
                  "keeps the whole take-on isolable afterwards.")
            )
        Move = ctx.env["account.move"]
        lines = transformed["transform_lines"]
        if not lines:
            raise UserError(
                _("No imported documents to mirror. Import the open items "
                  "first.")
            )

        # One entry per fiscal year, not one overall. A single mirror dated
        # at the latest document would neutralise a document raised in an
        # earlier year inside a later one, leaving both years wrong by the
        # same amount and the total right — which is exactly the error a
        # year-by-year comparison against Sage exists to catch. The
        # documents on this file already straddle a year end.
        company = ctx.env["res.company"].browse(ctx.get_config("company_id"))
        by_year = {}
        for line in lines:
            span = company.compute_fiscalyear_dates(
                fields.Date.from_string(line["date"])
            )
            by_year.setdefault(span["date_to"], []).append(line)

        for year_end, year_lines in sorted(by_year.items()):
            date = max(line["date"] for line in year_lines)
            reference = self._reference(year_end)
            existing = Move.search([
                ("journal_id", "=", journal_id), ("ref", "=", reference),
            ], limit=1)
            if existing:
                _logger.info(
                    "Counter-entry for %s already posted as %s; skipping.",
                    year_end, existing.name,
                )
                continue
            move = Move.create({
                "move_type": "entry",
                "journal_id": journal_id,
                "date": date,
                "ref": reference,
                "company_id": ctx.get_config("company_id"),
                "line_ids": [
                    (0, 0, {
                        key: value for key, value in line.items()
                        if key != "date"
                    } | {"tax_tag_ids": [(6, 0, line["tax_tag_ids"])]})
                    for line in year_lines
                ],
            })
            move.action_post()
            ctx.report.success()
            _logger.info(
                "Counter-entry %s posted at %s: %s lines.",
                move.name, date, len(year_lines),
            )
