"""Sage 50 trial balance -> the opening `account.move`.

There are two shapes of Sage take-on, and this pipeline serves both. Which
one runs is decided by `history_start_date` on the `sage.database` record.

**Balances only** (`history_start_date` empty). The classic take-on: Odoo
opens at `cutover_date` with a balance carried for every account, and no
history behind it. The balance of every postable account is the current
fiscal year's opening balance (`taccount.dYts`) plus that year's ledger
movements up to the cutover, which gives the balance at any date inside the
year. Summing the ledger alone would be wrong, and quietly so: Sage's oldest
generation in a file typically starts a year or two back and everything
before it exists only as opening balances, so a pure ledger sum reports every
balance-sheet account short by its whole earlier history — and still nets to
zero, which reads like success. The receivable and payable control accounts
are included at their full Sage balance with no partner, mirrored by
`sage.counter.entry.importer`, which leaves a partner-less balance of exactly
zero on each control account — the check that actually catches a document
missed at import.

**With history** (`history_start_date` set to a fiscal year start). Odoo
opens at the start of the oldest year being replayed and
`sage.journal.entry.importer` posts every entry from there forward, so the
year can be *closed* in Odoo rather than merely opened there. The opening
entry then carries balance-sheet accounts only: profit and loss accounts open
at zero every year by definition, and their movement arrives with the replay.

Reconstructing that opening means working backwards from `taccount.dYts` —
the opening of the *current* year, the only one Sage stores — subtracting
each intervening generation's movement. That is exact for every account but
one.

**Sage rolls net income into retained earnings with no journal entry.** At a
year end it does not sweep the profit and loss into equity the way a closing
entry would; it rolls the generation and adjusts the retained-earnings
balance silently. Working backwards through the ledger therefore leaves
retained earnings short by every year of profit the file still remembers, and
the trial balance does not tie. Each roll has to be undone by hand, and Odoo
then re-derives the same result itself from the replayed profit and loss.
Which account takes the roll is not guessed: Sage names it in `tlinkact`.

Either way the entry balances against the transition account, which must then
read the recorded imbalance and nothing else. Whatever is left over is a real
difference.
"""

import logging

from odoo import _, models
from odoo.exceptions import UserError
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="sage.opening.balance.importer",
    sap_source="taccount",
    depends_on=["sage.account.importer"],
    allow_multiprocessing=False,
)
class SageOpeningBalanceImporter(models.AbstractModel):
    _name = "sage.opening.balance.importer"
    _description = "Sage 50 Opening Trial Balance"

    # ------------------------------------------------------------------
    # Which shape of take-on is this
    # ------------------------------------------------------------------
    def history_start(self, ctx: ETLContext) -> str:
        """The date the imported history begins, or "" for balances only.

        Validated against the generations actually present in the file. A
        date that is not a fiscal year start would leave retained earnings
        carrying a part-year roll that Sage never performed, so it is
        refused rather than approximated.
        """
        start = ctx.get_config("history_start_date")
        if not start:
            return ""
        spans = tools.generation_spans(ctx.cr)
        starts = {span["start"] for span in spans}
        if start not in starts:
            raise UserError(_(
                "History must start at the beginning of a fiscal year Sage "
                "still holds. This file offers %(offered)s, not %(start)s.",
                offered=", ".join(sorted(starts)), start=start,
            ))
        return start

    def _postable(self, ctx: ETLContext) -> dict:
        return {
            row["lId"]: row
            for row in tools.query(
                ctx.cr,
                """select lId, sName, sNameAlt, cFunc, dYts, dYtc
                     from taccount where cFunc in %s""",
                (tools.POSTABLE_FUNCS,),
            )
        }

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    @ETL.extract("taccount")
    def extract_balances(self, ctx: ETLContext) -> dict:
        accounts = self._postable(ctx)
        start = self.history_start(ctx)
        if start:
            as_of, balances = self._history_opening(ctx, accounts, start)
        else:
            as_of, balances = self._cutover_opening(ctx, accounts)

        source = ctx.env[ctx.get_config("source_model")].browse(
            ctx.get_config("source_id")
        )
        return {
            "as_of": as_of,
            "rows": [
                {
                    "sage_account": account_id,
                    "name": source.sage_name(
                        accounts[account_id], "sName", "sNameAlt"
                    ),
                    # Debit-positive, which is what Odoo wants on a journal
                    # item.
                    "balance": tools.signed_amount(
                        account_id, round(natural, 2)
                    ),
                }
                for account_id, natural in sorted(balances.items())
                if round(natural, 2) and account_id in accounts
            ],
            "unknown_accounts": [
                account_id for account_id, natural in balances.items()
                if round(natural, 2) and account_id not in accounts
            ],
        }

    def _history_opening(self, ctx, accounts, start) -> tuple:
        """Balance-sheet balances at the start of the oldest replayed year."""
        retained = tools.linked_accounts(ctx.cr).get("lAcNretErn")
        if not retained or retained not in accounts:
            raise UserError(_(
                "Sage does not name a retained-earnings account in "
                "`tlinkact`, so the year-end rolls cannot be undone and an "
                "opening balance cannot be reconstructed."
            ))

        postable = set(accounts)
        balance_sheet = {
            account_id for account_id in postable
            if account_id // 10_000_000 < 4
        }
        # `dYts` is the current fiscal year's opening balance — zero for
        # profit and loss accounts, which is exactly right, since they
        # restart each year.
        balances = {
            account_id: round(accounts[account_id]["dYts"], 2)
            for account_id in balance_sheet
        }
        rolled = 0.0
        for span in tools.generation_spans(ctx.cr):
            if span["start"] < start:
                # Older than the history being imported: whatever it holds is
                # already inside the balances we are working back to.
                continue
            if span["index"] == 0:
                # The current year opens at `dYts` by definition; there is
                # nothing to unwind.
                continue
            movement = tools.generation_movement(ctx.cr, span)
            for account_id in balance_sheet:
                balances[account_id] -= movement.get(account_id, 0.0)
            income = tools.net_income(movement, postable)
            balances[retained] -= income
            rolled += income
            _logger.info(
                "Unwound %s: net income %.2f rolled out of retained "
                "earnings %s.", span["header"], income, retained,
            )
        _logger.info(
            "Opening reconstructed at %s: %.2f of net income unwound in "
            "total.", start, rolled,
        )
        return start, balances

    def _cutover_opening(self, ctx, accounts) -> tuple:
        """Every account's balance at a cutover date inside the open year."""
        as_of = ctx.get_config("cutover_date")
        if not as_of:
            raise UserError(_("Set the cutover date before importing the "
                              "opening balance."))

        fiscal = tools.query(
            ctx.cr, "select dtSDate, dtFDate from tcompany"
        )[0]
        first = fiscal["dtSDate"].strftime("%Y-%m-%d")
        last = fiscal["dtFDate"].strftime("%Y-%m-%d")
        if not first <= as_of <= last:
            raise UserError(_(
                "The cutover date %(as_of)s is outside the fiscal year this "
                "company file is open on (%(start)s to %(end)s). Take a fresh "
                "Sage backup: this one cannot describe that date.",
                as_of=as_of, start=first, end=last,
            ))

        balances = {
            account_id: round(row["dYts"], 2)
            for account_id, row in accounts.items()
            if round(row["dYts"], 2)
        }
        header, lines = tools.GENERATIONS[0]
        for row in tools.query(
            ctx.cr,
            f"""select l.lAcctId, round(sum(l.dAmount), 2) as amount
                  from {header} j
                  join {lines} l on l.lJEntId = j.lId
                 where j.dtJourDate <= %s
                 group by l.lAcctId""",
            (as_of,),
        ):
            balances[row["lAcctId"]] = (
                balances.get(row["lAcctId"], 0.0) + row["amount"]
            )

        # Cross-check against Sage's own summary field. It describes the
        # file's "as at" moment, so this only means anything when the cutover
        # is on or after the last entry in the file; the count is logged
        # either way because a large gap is itself informative.
        drift = [
            account_id for account_id in accounts
            if abs(round(balances.get(account_id, 0.0), 2)
                   - round(accounts[account_id]["dYtc"], 2)) > 0.005
        ]
        _logger.info(
            "%s accounts differ from taccount.dYtc (expected unless the "
            "cutover is at or past the file's last entry).", len(drift),
        )
        return as_of, balances

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------
    @ETL.transform()
    def transform_balances(self, ctx: ETLContext, extracted: dict) -> dict:
        payload = extracted["extract_balances"]
        if payload["unknown_accounts"]:
            # A presentation row never carries postings. A balance on one
            # means the sign convention is wrong somewhere, which would
            # corrupt every figure in the entry.
            raise UserError(_(
                "Balances found on Sage accounts that are not postable: %s",
                payload["unknown_accounts"],
            ))
        total = round(sum(row["balance"] for row in payload["rows"]), 2)
        expected = ctx.get_config("known_imbalance") or 0.0
        if abs(-total - expected) > 0.01:
            _logger.warning(
                "The Sage trial balance nets to %.2f, so the transition "
                "account will take %.2f — not the recorded imbalance of "
                "%.2f. Investigate before treating the take-on as tied.",
                total, -total, expected,
            )
        return {
            "as_of": payload["as_of"],
            "rows": payload["rows"],
            # The transition account takes the opposite side so the move
            # balances.
            "transition_amount": round(-total, 2),
        }

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    def _reference(self, as_of: str) -> str:
        return f"Sage 50 take-on — opening balance at {as_of}"

    @ETL.load()
    def load_opening_balance(self, ctx: ETLContext, transformed: dict) -> None:
        payload = transformed["transform_balances"]
        journal_id = ctx.get_config("journal_id")
        transition_id = ctx.get_config("transition_account_id")
        if not journal_id or not transition_id:
            raise UserError(_(
                "The opening entry needs both a journal and a transition "
                "account."
            ))

        Move = ctx.env["account.move"]
        reference = self._reference(payload["as_of"])
        existing = Move.search([
            ("journal_id", "=", journal_id), ("ref", "=", reference),
        ], limit=1)
        if existing:
            _logger.info(
                "Opening entry already posted as %s; nothing to do.",
                existing.name,
            )
            return

        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        lines = []
        for row in payload["rows"]:
            account_id = accounts.get(row["sage_account"])
            if not account_id:
                raise UserError(_(
                    "No Odoo account for Sage %(sage_id)s, which carries a "
                    "balance of %(balance)s. Import the chart of accounts "
                    "first.",
                    sage_id=row["sage_account"], balance=row["balance"],
                ))
            balance = row["balance"]
            lines.append((0, 0, {
                "name": row["name"],
                "account_id": account_id,
                "debit": balance if balance > 0 else 0.0,
                "credit": -balance if balance < 0 else 0.0,
            }))
        amount = payload["transition_amount"]
        lines.append((0, 0, {
            "name": _("Sage 50 take-on — balancing entry"),
            "account_id": transition_id,
            "debit": amount if amount > 0 else 0.0,
            "credit": -amount if amount < 0 else 0.0,
        }))

        move = Move.create({
            "move_type": "entry",
            "journal_id": journal_id,
            "date": payload["as_of"],
            "ref": reference,
            "company_id": ctx.get_config("company_id"),
            "line_ids": lines,
        })
        move.action_post()
        ctx.report.success()
        _logger.info(
            "Opening entry %s posted at %s: %s accounts, transition account "
            "takes %.2f.",
            move.name, payload["as_of"], len(payload["rows"]), amount,
        )
