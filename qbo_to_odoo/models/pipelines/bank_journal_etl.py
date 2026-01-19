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

        _logger.info(
            f"Found {len(bank_accounts)} QBO bank accounts, "
            f"{len(accounts_needing_journals)} need journals"
        )
        return accounts_needing_journals

    @ETL.transform()
    def transform_journals(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform bank accounts into journal values."""
        accounts = extracted.get("extract_bank_accounts", [])
        company = ctx.env.company

        journal_vals = []
        for acc in accounts:
            journal_vals.append(
                {
                    "name": acc["name"],
                    "type": "bank",
                    "code": acc["code"][:5],  # Journal code max 5 chars
                    "default_account_id": acc["id"],
                    "company_id": company.id,
                }
            )

        _logger.info(f"Prepared {len(journal_vals)} bank journals to create")
        return journal_vals

    @ETL.load()
    def load_journals(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create bank journals."""
        journal_vals = transformed.get("transform_journals", [])

        if not journal_vals:
            _logger.info("No new bank journals to create")
            return

        created = 0
        for vals in journal_vals:
            ctx.env["account.journal"].create(vals)
            created += 1
            _logger.info(f"Created bank journal '{vals['name']}'")

        _logger.info(f"Created {created} bank journals")
