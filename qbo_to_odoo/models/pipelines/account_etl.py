"""QuickBooks Online Account ETL Pipeline

This module handles the migration of Chart of Accounts from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Any, Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# QBO Account Type to Odoo account_type mapping
QBO_ACCOUNT_TYPE_MAP = {
    "Bank": "asset_cash",
    "Other Current Asset": "asset_current",
    "Fixed Asset": "asset_fixed",
    "Other Asset": "asset_non_current",
    "Accounts Receivable": "asset_receivable",
    "Equity": "equity",
    "Expense": "expense",
    "Other Expense": "expense",
    "Cost of Goods Sold": "expense_direct_cost",
    "Accounts Payable": "liability_payable",
    "Credit Card": "liability_credit_card",
    "Long Term Liability": "liability_non_current",
    "Other Current Liability": "liability_current",
    "Income": "income",
    "Other Income": "income_other",
}


@ETL.pipeline(
    target_model="account.account",
    importer_name="qbo.account.importer",
    sap_source="Account",  # QBO entity name
    depends_on=[],
)
class QboAccountImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Chart of Accounts."""

    _name = "qbo.account.importer"
    _description = "QBO Account Importer"

    @ETL.extract("Account")
    def extract_accounts(self, ctx: ETLContext) -> List[Dict]:
        """Extract accounts from QBO API.

        Uses the API client from source_config instead of a database cursor.
        """
        api_client = ctx.get_config("api_client")
        if not api_client:
            raise ValueError("API client not found in ETL context")

        # Get existing QBO IDs to avoid re-importing
        ctx.env.cr.execute(
            "SELECT qbo_id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        existing_qbo_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_qbo_ids)} existing accounts in Odoo")

        # Fetch all accounts from QBO (both active and inactive)
        accounts = api_client.query_all(
            entity="Account", where="Active IN (true, false)", order_by="Id"
        )

        # Filter out already imported accounts
        new_accounts = [
            acc for acc in accounts if str(acc.get("Id")) not in existing_qbo_ids
        ]

        _logger.info(
            f"Extracted {len(accounts)} accounts from QBO, "
            f"{len(new_accounts)} are new"
        )
        return new_accounts

    @ETL.transform()
    def transform_accounts(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO accounts into Odoo account values."""
        accounts = extracted.get("extract_accounts", [])

        company = ctx.env.company

        account_vals = []
        skipped = 0
        # Track codes used in this batch to avoid duplicates within the import
        used_codes = set()

        # Pre-load existing codes from database (including archived)
        existing_accounts = (
            ctx.env["account.account"]
            .with_context(active_test=False)
            .search_read([("company_ids", "in", [company.id])], ["code"])
        )
        for acc in existing_accounts:
            if acc.get("code"):
                used_codes.add(acc["code"])

        for account in accounts:
            # Skip accounts without account number
            acct_num = account.get("AcctNum")
            if not acct_num:
                _logger.warning(
                    f"Skipping account '{account.get('Name')}' "
                    f"(QBO ID: {account.get('Id')}) - no account number"
                )
                skipped += 1
                continue

            # Map QBO account type to Odoo
            qbo_type = account.get("AccountType", "")
            odoo_type = QBO_ACCOUNT_TYPE_MAP.get(qbo_type, "asset_current")

            # Check for duplicate code (both in DB and in current batch)
            code = str(acct_num)
            if code in used_codes:
                # Generate unique code with suffix
                suffix = 1
                new_code = f"{code}.{suffix}"
                while new_code in used_codes:
                    suffix += 1
                    new_code = f"{code}.{suffix}"
                _logger.warning(
                    f"Duplicate code '{code}' for account '{account.get('Name')}' "
                    f"(QBO ID: {account.get('Id')}). Using '{new_code}' instead."
                )
                code = new_code

            # Track this code as used
            used_codes.add(code)

            # Determine if reconcilable
            reconcile = odoo_type in ("asset_receivable", "liability_payable")

            account_vals.append(
                {
                    "name": account.get("Name", ""),
                    "code": code,
                    "account_type": odoo_type,
                    "reconcile": reconcile,
                    "qbo_id": int(account.get("Id")),
                    "company_ids": [(4, company.id)],
                }
            )

        _logger.info(f"Transformed {len(account_vals)} accounts, skipped {skipped}")
        return account_vals

    @ETL.load()
    def load_accounts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load accounts into Odoo."""
        account_vals = transformed.get("transform_accounts", [])

        if not account_vals:
            _logger.info("No new accounts to create")
            return

        # Create accounts one by one to handle potential errors
        created = 0
        errors = 0

        for vals in account_vals:
            try:
                ctx.env["account.account"].create(vals)
                created += 1
            except Exception as e:
                _logger.error(f"Failed to create account {vals.get('code')}: {e}")
                errors += 1

        _logger.info(f"Created {created} accounts, {errors} errors")

        # Update last sync timestamp
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection.last_account_sync = ctx.env.cr.now()
