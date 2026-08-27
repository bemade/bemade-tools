"""Bank journals for the Sage accounts payments were made through.

Sage records the bank account on the application itself (`lBnkAcctId`), so a
take-on that re-creates payments needs a journal per bank account actually
used. They are created here rather than by hand because the set is whatever
the data says it is.

**The journal's payment methods point at the bank account, not at an
outstanding account.** Odoo normally posts a payment to Outstanding
Receipts/Payments and only moves it to the bank when a statement is
reconciled — right for a payment being made today, wrong for one that cleared
the bank years ago in another system. Pointed at the bank account, the
imported payments post straight to it and count as matched, which is what the
client's own history says happened.
"""

import logging

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)

#: Sage account sections whose accounts can back a bank journal.
LIQUIDITY_TYPES = ("asset_cash", "liability_credit_card")


@ETL.pipeline(
    target_model="account.journal",
    importer_name="sage.bank.journal.importer",
    sap_source="tcustrdt",
    depends_on=["sage.account.importer"],
    allow_multiprocessing=False,
)
class SageBankJournalImporter(models.AbstractModel):
    _name = "sage.bank.journal.importer"
    _description = "Sage 50 Bank Journal Importer"

    @ETL.extract("tcustrdt")
    def extract_bank_accounts(self, ctx: ETLContext) -> list:
        """Every bank account an application was made through.

        Both sides, and every document rather than only the open ones: the
        journals are cheap, and a later phase importing payment history would
        otherwise have to create them halfway through.
        """
        seen = {}
        for table in ("tcustrdt", "tventrdt"):
            for row in tools.query(
                ctx.cr,
                f"""select lBnkAcctId, count(*) as uses
                      from {table}
                     where lBnkAcctId <> 0
                     group by lBnkAcctId""",
            ):
                seen[row["lBnkAcctId"]] = (
                    seen.get(row["lBnkAcctId"], 0) + row["uses"]
                )
        return [
            {"sage_account": account, "uses": uses}
            for account, uses in sorted(seen.items())
        ]

    @ETL.transform()
    def transform_journals(self, ctx: ETLContext, extracted: dict) -> list:
        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        Account = ctx.env["account.account"]
        values = []
        for row in extracted["extract_bank_accounts"]:
            account_id = accounts.get(row["sage_account"])
            if not account_id:
                ctx.report.warning(
                    f"No Odoo account for Sage bank account "
                    f"{row['sage_account']}; no journal created",
                    source_ref=str(row["sage_account"]),
                )
                continue
            account = Account.browse(account_id)
            if account.account_type not in LIQUIDITY_TYPES:
                # Sage will happily record an application against something
                # that is not a bank account. Making a bank journal out of it
                # would put a non-liquidity account behind a journal Odoo
                # treats as cash.
                ctx.report.warning(
                    f"Sage bank account {row['sage_account']} maps to "
                    f"{account.code} ({account.account_type}), which is not a "
                    f"liquidity account; no journal created",
                    source_ref=str(row["sage_account"]),
                )
                continue
            values.append({
                "sage_account": row["sage_account"],
                "account_id": account_id,
                "name": account.name,
                "code": self._journal_code(ctx, account),
            })
        return values

    def _journal_code(self, ctx: ETLContext, account) -> str:
        """A short, unique journal code derived from the account code."""
        base = "".join(c for c in (account.code or "") if c.isalnum())[:5]
        base = (base or "BNK").upper()
        code, suffix = base, 1
        Journal = ctx.env["account.journal"]
        while Journal.search_count([
            ("code", "=", code),
            ("company_id", "=", ctx.get_config("company_id")),
        ]):
            suffix += 1
            code = f"{base[:4]}{suffix}"
        return code

    @ETL.load()
    def load_journals(self, ctx: ETLContext, transformed: dict) -> None:
        Journal = ctx.env["account.journal"]
        company_id = ctx.get_config("company_id")
        created = matched = 0
        for values in transformed["transform_journals"]:
            journal = Journal.search([
                ("type", "=", "bank"),
                ("default_account_id", "=", values["account_id"]),
                ("company_id", "=", company_id),
            ], limit=1)
            if not journal:
                journal = Journal.create({
                    "name": values["name"],
                    "code": values["code"],
                    "type": "bank",
                    "company_id": company_id,
                    "default_account_id": values["account_id"],
                })
                created += 1
            else:
                matched += 1
            self._point_payment_methods_at_the_bank(journal)
            ctx.report.success()
        _logger.info(
            "Sage bank journals: %s created, %s already present.",
            created, matched,
        )

    def _point_payment_methods_at_the_bank(self, journal) -> None:
        """Make payments land in the bank account rather than in suspense.

        See the module docstring: an imported payment is history, and its
        money reached the bank long ago. Leaving it in Outstanding
        Receipts/Payments would show the client a pile of payments waiting for
        a bank statement that was reconciled in Sage years earlier.
        """
        methods = (
            journal.inbound_payment_method_line_ids
            | journal.outbound_payment_method_line_ids
        )
        methods.filtered(
            lambda line: not line.payment_account_id
        ).payment_account_id = journal.default_account_id
