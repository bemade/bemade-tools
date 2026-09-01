"""Sweep each closed year's result into retained earnings.

Sage does not post a closing entry. It rolls the generation at a year end and
adjusts the retained-earnings balance silently, so a faithful replay of its
ledger leaves every closed year's profit sitting in Odoo's computed
"Undistributed Profits/Losses" line instead of in the retained-earnings
account where Sage put it. The books are right either way — the equity total
is the same — but the balance sheet does not look like the one the client has
been reading, and retained earnings disagrees with Sage by the whole of the
history.

So each closed year gets the closing entry Sage never wrote: its net result
moved from the unaffected-earnings account Odoo computes into the account
Sage names in `tlinkact.lAcNretErn`. The open year is deliberately left
alone — that is the year the accountant is about to close, and closing it
here would take the decision away from them.

The same figures the opening balance unwound are the ones swept back, so the
two operations are exact inverses: reconstruct the opening by taking each
year's result out of retained earnings, replay the ledger, then put each
closed year's result back where Sage had it.
"""

import logging

from odoo import _, models
from odoo.exceptions import UserError
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)

REFERENCE = "Sage 50 take-on — year-end close"


@ETL.pipeline(
    target_model="account.move",
    importer_name="sage.year.end.close.importer",
    sap_source="taccount",
    depends_on=["sage.journal.entry.importer"],
    allow_multiprocessing=False,
)
class SageYearEndClose(models.AbstractModel):
    _name = "sage.year.end.close.importer"
    _description = "Sage 50 Year-End Close"

    def _reference(self, year_end: str) -> str:
        return f"{REFERENCE} {year_end}"

    @ETL.extract("taccount")
    def extract_closes(self, ctx: ETLContext) -> list:
        start = ctx.env["sage.opening.balance.importer"].history_start(ctx)
        if not start:
            return []
        postable = {
            row["lId"] for row in tools.query(
                ctx.cr,
                "select lId from taccount where cFunc in %s",
                (tools.POSTABLE_FUNCS,),
            )
        }
        retained = tools.linked_accounts(ctx.cr).get("lAcNretErn")
        if not retained:
            raise UserError(_(
                "Sage names no retained-earnings account in `tlinkact`, so a "
                "year cannot be closed against it."
            ))
        closes = []
        for span in tools.generation_spans(ctx.cr):
            if span["start"] < start or span["index"] == 0:
                # Older than the replay, or the year still open.
                continue
            movement = tools.generation_movement(ctx.cr, span)
            closes.append({
                "year_end": span["end"],
                "sage_retained": retained,
                "net_income": tools.net_income(movement, postable),
            })
        return closes

    @ETL.transform()
    def transform_closes(self, ctx: ETLContext, extracted: dict) -> list:
        return [row for row in extracted["extract_closes"] if row["net_income"]]

    @ETL.load()
    def load_closes(self, ctx: ETLContext, transformed: dict) -> None:
        closes = transformed["transform_closes"]
        if not closes:
            return
        company_id = ctx.get_config("company_id")
        journal_id = ctx.get_config("journal_id")
        Move = ctx.env["account.move"]
        Account = ctx.env["account.account"]

        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        unaffected = Account.search([
            ("account_type", "=", "equity_unaffected"),
            ("company_ids", "in", company_id),
        ], limit=1)
        if not unaffected:
            raise UserError(_(
                "No unaffected-earnings account on this company, so there is "
                "nothing to sweep a closed year out of."
            ))

        for close in closes:
            retained_id = accounts.get(close["sage_retained"])
            if not retained_id:
                raise UserError(_(
                    "No Odoo account for Sage %s, which Sage names as "
                    "retained earnings.", close["sage_retained"],
                ))
            reference = self._reference(close["year_end"])
            if Move.search_count([
                ("journal_id", "=", journal_id), ("ref", "=", reference),
            ]):
                _logger.info("%s already posted; skipping.", reference)
                continue

            # Debit-positive: a profit leaves the unaffected-earnings account
            # as a debit and lands in retained earnings as a credit.
            amount = close["net_income"]
            move = Move.create({
                "move_type": "entry",
                "journal_id": journal_id,
                "date": close["year_end"],
                "ref": reference,
                "company_id": company_id,
                "line_ids": [
                    (0, 0, {
                        "name": _("Result for the year"),
                        "account_id": unaffected.id,
                        "debit": amount if amount > 0 else 0.0,
                        "credit": -amount if amount < 0 else 0.0,
                    }),
                    (0, 0, {
                        "name": _("Result for the year"),
                        "account_id": retained_id,
                        "debit": -amount if amount < 0 else 0.0,
                        "credit": amount if amount > 0 else 0.0,
                    }),
                ],
            })
            move.action_post()
            ctx.report.success()
            _logger.info(
                "Year-end close %s posted at %s: %.2f swept to retained "
                "earnings.", move.name, close["year_end"], amount,
            )
