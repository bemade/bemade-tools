"""Sage 50 trial balance -> the opening `account.move`.

The balance of every postable account is the current fiscal year's opening
balance (`taccount.dYts`) plus that year's ledger movements up to the cutover
date. That gives the balance at any date inside the year, which is what a
cutover on a date the client chooses actually needs, and it is cross-checked
against Sage's own summary field, which only ever describes the file's "as at"
moment.

**Summing the ledger alone would be wrong, and quietly so.** Sage's oldest
generation in a file typically starts a year or two back, and everything
before it exists only as opening balances. A pure ledger sum reports every
balance-sheet account short by its whole earlier history — and still nets to
zero, which reads like success.

The receivable and payable control accounts ARE included, carrying their full
Sage balance and no partner. That looks like double-counting against the open
documents, and would be, except that the counter-entry mirrors those
documents with control lines that also carry no partner. The three entries
together leave the general ledger with one AR balance and one AP balance and
a partner-less balance of exactly zero on each — which is the check that
actually catches a document missed at import.

The entry balances against the transition account, which must then read the
recorded imbalance and nothing else. Whatever is left over is a real
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
    depends_on=["sage.counter.entry.importer"],
    allow_multiprocessing=False,
)
class SageOpeningBalanceImporter(models.AbstractModel):
    _name = "sage.opening.balance.importer"
    _description = "Sage 50 Opening Trial Balance"

    def _reference(self, as_of: str) -> str:
        return f"Sage 50 take-on — opening balance at {as_of}"

    @ETL.extract("taccount")
    def extract_balances(self, ctx: ETLContext):
        as_of = ctx.get_config("cutover_date")
        if not as_of:
            raise UserError(_("Set the cutover date before importing the "
                              "opening balance."))

        fiscal = tools.query(
            ctx.cr, "select dtSDate, dtFDate from tcompany"
        )[0]
        start = fiscal["dtSDate"].strftime("%Y-%m-%d")
        end = fiscal["dtFDate"].strftime("%Y-%m-%d")
        if not start <= as_of <= end:
            raise UserError(_(
                "The cutover date %(as_of)s is outside the fiscal year this "
                "company file is open on (%(start)s to %(end)s). Take a fresh "
                "Sage backup: this one cannot describe that date.",
                as_of=as_of, start=start, end=end,
            ))

        accounts = {
            row["lId"]: row
            for row in tools.query(
                ctx.cr,
                """select lId, sName, sNameAlt, cFunc, dYts, dYtc
                     from taccount where cFunc in %s""",
                (tools.POSTABLE_FUNCS,),
            )
        }

        # `dYts` is the current fiscal year's opening balance — zero for P&L
        # accounts, which is exactly right, since they restart each year. The
        # identity `sum(current-year lines) == dYtc - dYts` holds for every
        # used account in a healthy file, which is what makes this safe.
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
