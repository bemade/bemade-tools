"""QuickBooks Online Bank Journal Post-Processing Pipeline

This module creates bank journals for QBO-imported bank accounts
that don't already have a journal.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.journal",
    importer_name="qbo.bank.journal.processor",
    sap_source="Account",
    depends_on=["qbo.account.importer"],
)
class QboBankJournalProcessor(models.AbstractModel):
    """Post-processing pipeline to create bank journals for QBO bank accounts."""

    _name = "qbo.bank.journal.processor"
    _description = "QBO Bank Journal Processor"

    @ETL.extract("Account")
    def extract_bank_accounts(self, ctx: ETLContext) -> List[Dict]:
        """Find QBO-imported bank accounts without journals."""
        company = ctx.env.company

        # Find all QBO-imported bank accounts (asset_cash type)
        bank_accounts = ctx.env["account.account"].search(
            [
                ("account_type", "=", "asset_cash"),
                ("qbo_id", "!=", False),
                ("company_ids", "in", [company.id]),
            ]
        )

        # Find which ones don't have a bank journal
        accounts_needing_journals = []
        for account in bank_accounts:
            existing_journal = ctx.env["account.journal"].search(
                [
                    ("type", "=", "bank"),
                    ("default_account_id", "=", account.id),
                    ("company_id", "=", company.id),
                ],
                limit=1,
            )

            if not existing_journal:
                accounts_needing_journals.append(
                    {
                        "id": account.id,
                        "name": account.name,
                        "code": account.code,
                    }
                )

        # Collect existing journal codes to avoid duplicates in transform
        existing_codes = set(
            ctx.env["account.journal"]
            .search([("company_id", "=", company.id)])
            .mapped("code")
        )

        _logger.info(
            f"Found {len(bank_accounts)} QBO bank accounts, "
            f"{len(accounts_needing_journals)} need journals"
        )
        return {
            "accounts": accounts_needing_journals,
            "existing_codes": list(existing_codes),
            "company_id": company.id,
        }

    @ETL.transform()
    def transform_journals(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform bank accounts into journal values."""
        data = extracted.get("extract_bank_accounts", {})
        accounts = data.get("accounts", [])
        existing_codes = set(data.get("existing_codes", []))
        company_id = data.get("company_id")

        journal_vals = []
        for acc in accounts:
            code = acc["code"][:5]
            if code in existing_codes:
                # Try shorter truncations, then add numeric suffix
                for length in (4, 3):
                    candidate = acc["code"][:length]
                    if candidate not in existing_codes:
                        code = candidate
                        break
                else:
                    suffix = 1
                    base = acc["code"][:4]
                    while f"{base}{suffix}" in existing_codes:
                        suffix += 1
                    code = f"{base}{suffix}"
            existing_codes.add(code)
            journal_vals.append(
                {
                    "name": acc["name"],
                    "type": "bank",
                    "code": code,
                    "default_account_id": acc["id"],
                    "company_id": company_id,
                }
            )

        _logger.info(f"Prepared {len(journal_vals)} bank journals to create")
        return journal_vals

    @ETL.load()
    def load_journals(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create bank journals, then archive QBO 'deleted' journals.

        Journal creation is followed by an instance-wide cleanup step that
        archives any ``account.journal`` whose name contains "deleted"
        (case-insensitive). This pipeline is the journal-owning pipeline and
        runs as part of the orchestrated import, so this is the correct
        end-of-import point to clean up QBO's carried-over deleted journals.
        """
        journal_vals = transformed.get("transform_journals", [])

        if not journal_vals:
            _logger.info("No new bank journals to create")
        else:
            created = 0
            for vals in journal_vals:
                ctx.env["account.journal"].create(vals)
                created += 1
                _logger.info(f"Created bank journal '{vals['name']}'")

            _logger.info(f"Created {created} bank journals")

        self._archive_deleted_journals(ctx)

        # Point the company default, the active bank journals, and their
        # in/out payment method lines at the Clearing (1005) suspense account.
        # Runs unconditionally (even when 0 journals were created this pass) so
        # an orchestrated import always converges on the correct suspense config.
        self._configure_suspense_accounts(ctx)

    def _ensure_clearing_suspense_account(self, ctx: ETLContext):
        """Resolve (or create) the Clearing ``account.account`` used as suspense.

        Resolution order, scoped to the current company (account.account is
        multi-company via the ``company_ids`` M2M in 19.0):

        1. code == "1005";
        2. else name ilike "Clearing" AND account_type == "asset_current";
        3. else create a new {code 1005, name "Clearing", asset_current,
           reconcile} account.

        We deliberately NEVER select or create the "Bank Suspense" account
        (account 343 / "111312 Bank Suspense Account"): the directive is to use
        Clearing (1005) for suspense everywhere, and no id is hardcoded.
        """
        company = ctx.env.company
        Account = ctx.env["account.account"].with_context(active_test=False)

        account = Account.search(
            [("code", "=", "1005"), ("company_ids", "in", [company.id])],
            limit=1,
        )
        if not account:
            account = Account.search(
                [
                    ("name", "ilike", "Clearing"),
                    ("account_type", "=", "asset_current"),
                    ("company_ids", "in", [company.id]),
                ],
                limit=1,
            )
        if not account:
            account = Account.create(
                {
                    "name": "Clearing",
                    "code": "1005",
                    "account_type": "asset_current",
                    "reconcile": True,
                    "company_ids": [(6, 0, [company.id])],
                }
            )
            _logger.info(
                "Created Clearing suspense account %s for company %s",
                account.code,
                company.name,
            )
        return account

    def _configure_suspense_accounts(self, ctx: ETLContext) -> None:
        """Wire the Clearing (1005) account in as the suspense account.

        Sets it as (a) the company default so future journals auto-inherit it
        (``account.journal.suspense_account_id`` is a stored computed that falls
        back to ``company.account_journal_suspense_account_id``), (b) the
        suspense account on every active, non-"(deleted)" bank journal, and
        (c) the ``payment_account_id`` on those journals' inbound/outbound
        payment method lines.

        The method line write is a blanket OVERWRITE (not a fill-if-empty):
        QBO migrations leave some lines pointing at the wrong account
        ("111312 Bank Suspense Account" / acct 343); the directive is "1005
        everywhere", so every in/out line is repointed regardless of its
        current value. Idempotent: every step searches and sets to a fixed
        target, so re-runs are no-ops.
        """
        clearing = self._ensure_clearing_suspense_account(ctx)
        if not clearing:
            _logger.warning(
                "Could not resolve a Clearing suspense account; "
                "skipping suspense configuration."
            )
            return

        company = ctx.env.company

        # Company-level default: set only if it differs, so future bank
        # journals inherit Clearing without an unnecessary write.
        if company.account_journal_suspense_account_id != clearing:
            company.account_journal_suspense_account_id = clearing
            _logger.info(
                "Set company %s default suspense account to %s",
                company.name,
                clearing.code,
            )

        # Active bank journals, excluding QBO "(deleted)"-named carryovers.
        journals = ctx.env["account.journal"].search(
            [
                ("company_id", "=", company.id),
                ("type", "=", "bank"),
                ("name", "not ilike", "(deleted)"),
            ]
        )
        if journals:
            journals.write({"suspense_account_id": clearing.id})
            _logger.info(
                "Set suspense_account_id to %s on %d bank journal(s)",
                clearing.code,
                len(journals),
            )

        # Their inbound/outbound payment method lines: overwrite all
        # (including any bad 343 references) so payments route through Clearing.
        lines = ctx.env["account.payment.method.line"].search(
            [
                ("journal_id", "in", journals.ids),
                ("payment_type", "in", ("inbound", "outbound")),
            ]
        )
        if lines:
            lines.write({"payment_account_id": clearing.id})
            _logger.info(
                "Set payment_account_id to %s on %d in/out payment method "
                "line(s) across %d journal(s)",
                clearing.code,
                len(lines),
                len(lines.mapped("journal_id")),
            )

    @staticmethod
    def _archive_deleted_journals(ctx: ETLContext) -> None:
        """Archive every active ``account.journal`` whose name contains "deleted".

        QBO carries over journals for deactivated-in-QBO accounts; their names
        contain "deleted" (e.g. "Old Bank (deleted)"). These should not remain
        in the active journal set after a migration import.

        Idempotent: the search domain filters on ``active = True``, so journals
        already archived on a previous import are not re-touched and the write
        is a no-op when nothing matches. Matching is case-insensitive
        (``ilike``) because QBO's "(deleted)" suffix casing is not guaranteed.
        """
        to_archive = ctx.env["account.journal"].search(
            [("name", "ilike", "deleted"), ("active", "=", True)]
        )
        if not to_archive:
            _logger.info("No 'deleted' journals to archive")
            return

        to_archive.write({"active": False})
        _logger.info(
            "Archived %d 'deleted' journal(s): %s",
            len(to_archive),
            ", ".join(to_archive.mapped("name")),
        )
