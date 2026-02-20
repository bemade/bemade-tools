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

        # Archive standard Odoo accounts that don't have QBO IDs
        # (fresh database - no transactions to worry about)
        odoo_default_accounts = ctx.env["account.account"].search(
            [("company_ids", "in", [company.id]), ("qbo_id", "=", False)]
        )

        if odoo_default_accounts:
            _logger.info(
                f"Found {len(odoo_default_accounts)} Odoo default accounts to archive "
                f"(fresh database - no transaction check needed)"
            )
            odoo_default_accounts.write({"active": False})
            _logger.info(
                f"Archived {len(odoo_default_accounts)} Odoo default accounts: "
                f"{', '.join(odoo_default_accounts.mapped('code'))}"
            )

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
        """Load accounts into Odoo and set proper defaults."""
        account_vals = transformed.get("transform_accounts", [])

        if not account_vals:
            _logger.info("No new accounts to create")
        else:
            # Batch create accounts
            accounts = ctx.env["account.account"].create(account_vals)
            _logger.info(f"Created {len(accounts)} accounts")

        # Always set account defaults (fixes company fields even on re-runs)
        self._set_account_defaults(ctx)

        # Update last sync timestamp
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection.last_account_sync = ctx.env.cr.now()

    def _set_account_defaults(self, ctx: ETLContext) -> None:
        """Set proper account defaults based on imported QBO accounts."""
        company = ctx.env.company
        IrDefault = ctx.env["ir.default"].sudo()

        # Find common account types from QBO accounts
        qbo_accounts = ctx.env["account.account"].search(
            [("company_ids", "in", [company.id]), ("qbo_id", "!=", False)]
        )

        if not qbo_accounts:
            _logger.warning("No QBO accounts found for setting defaults")
            return

        _logger.info(f"Setting account defaults from {len(qbo_accounts)} QBO accounts")

        # Default bank account (for bank journals)
        bank_accounts = qbo_accounts.filtered(
            lambda a: a.account_type == "asset_cash"
        ).sorted("code")
        if bank_accounts:
            bank_account = bank_accounts[0]
            # Set as default for new bank journals
            IrDefault.set("account.journal", "default_account_id", bank_account.id)
            _logger.info(
                f"Set default bank account: {bank_account.code} - {bank_account.name}"
            )

        # Default income account (for sale journals)
        income_accounts = qbo_accounts.filtered(
            lambda a: a.account_type == "income"
        ).sorted("code")
        if income_accounts:
            income_account = income_accounts[0]
            # Set as default for product categories
            IrDefault.set(
                "product.category",
                "property_account_income_categ_id",
                income_account.id,
            )
            # Update existing sale journals
            sale_journals = ctx.env["account.journal"].search(
                [("type", "=", "sale"), ("company_id", "=", company.id)]
            )
            if sale_journals:
                sale_journals.write({"default_account_id": income_account.id})
                _logger.info(
                    f"Updated {len(sale_journals)} sale journals with default income account: {income_account.code}"
                )
            _logger.info(
                f"Set default income account: {income_account.code} - {income_account.name}"
            )

        # Default expense account (for purchase journals)
        expense_accounts = qbo_accounts.filtered(
            lambda a: a.account_type in ["expense", "expense_direct_cost"]
        ).sorted("code")
        if expense_accounts:
            expense_account = expense_accounts[0]
            # Set as default for product categories
            IrDefault.set(
                "product.category",
                "property_account_expense_categ_id",
                expense_account.id,
            )
            # Update existing purchase journals
            purchase_journals = ctx.env["account.journal"].search(
                [("type", "=", "purchase"), ("company_id", "=", company.id)]
            )
            if purchase_journals:
                purchase_journals.write({"default_account_id": expense_account.id})
                _logger.info(
                    f"Updated {len(purchase_journals)} purchase journals with default expense account: {expense_account.code}"
                )
            _logger.info(
                f"Set default expense account: {expense_account.code} - {expense_account.name}"
            )

        # Default stock account (if exists)
        stock_accounts = qbo_accounts.filtered(
            lambda a: a.account_type in ["asset_current", "asset_non_current"]
        ).sorted("code")
        if stock_accounts:
            stock_account = stock_accounts[0]
            # Set as default for product categories
            IrDefault.set(
                "product.category",
                "property_stock_valuation_account_id",
                stock_account.id,
            )
            _logger.info(
                f"Set default stock account: {stock_account.code} - {stock_account.name}"
            )

        # Trigger auto-detection of AR/AP accounts (this already exists in qbo_connection)
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection._auto_detect_default_accounts()

        # Set early payment discount accounts to avoid payment processing errors
        company = ctx.env.company

        # Debug: Log current company settings
        _logger.info(
            f"Current early payment discount loss account: {company.account_journal_early_pay_discount_loss_account_id.code if company.account_journal_early_pay_discount_loss_account_id else 'None'}"
        )
        _logger.info(
            f"Current early payment discount gain account: {company.account_journal_early_pay_discount_gain_account_id.code if company.account_journal_early_pay_discount_gain_account_id else 'None'}"
        )

        # Debug: Log available QBO accounts
        expense_qbo_accounts = qbo_accounts.filtered(
            lambda a: a.account_type in ["expense", "expense_direct_cost"]
        )
        income_qbo_accounts = qbo_accounts.filtered(
            lambda a: a.account_type == "income"
        )
        _logger.info(
            f"Available expense accounts: {[(a.code, a.name) for a in expense_qbo_accounts[:5]]}"
        )
        _logger.info(
            f"Available income accounts: {[(a.code, a.name) for a in income_qbo_accounts[:5]]}"
        )

        # Enhanced inference for discount accounts
        # Look for accounts with discount-related names first, then fall back to reasonable defaults

        # Loss account: Look for expense accounts with discount-related names
        discount_loss_accounts = qbo_accounts.filtered(
            lambda a: a.account_type in ["expense", "expense_direct_cost"]
            and any(
                keyword in a.name.lower()
                for keyword in ["discount", "fee", "loss", "charge"]
            )
        ).sorted("code")

        _logger.info(
            f"Found discount loss accounts: {[(a.code, a.name) for a in discount_loss_accounts]}"
        )

        # Fallback to general expense if no specific discount account found
        expense_accounts = qbo_accounts.filtered(
            lambda a: a.account_type in ["expense", "expense_direct_cost"]
        ).sorted("code")

        # Gain account: Look for income accounts with discount-related names (but not "refunds")
        discount_gain_accounts = qbo_accounts.filtered(
            lambda a: a.account_type == "income"
            and any(
                keyword in a.name.lower()
                for keyword in ["early payment", "payment discount", "gain"]
            )
            and "refunds" not in a.name.lower()
        ).sorted("code")

        _logger.info(
            f"Found discount gain accounts: {[(a.code, a.name) for a in discount_gain_accounts]}"
        )

        # Fallback to general income, avoiding accounts with "discounts" and "refunds" in the name
        income_accounts = qbo_accounts.filtered(
            lambda a: a.account_type == "income"
            and "discounts" not in a.name.lower()
            and "refunds" not in a.name.lower()
        ).sorted("code")

        # Set loss account with preference for discount-specific accounts
        if discount_loss_accounts:
            company.account_journal_early_pay_discount_loss_account_id = (
                discount_loss_accounts[0].id
            )
            _logger.info(
                f"Set early payment discount loss account to: {discount_loss_accounts[0].code} - {discount_loss_accounts[0].name}"
            )
        elif expense_accounts:
            company.account_journal_early_pay_discount_loss_account_id = (
                expense_accounts[0].id
            )
            _logger.info(
                f"Set early payment discount loss account to general expense: {expense_accounts[0].code} - {expense_accounts[0].name}"
            )

        # Set gain account with preference for discount-specific accounts
        if discount_gain_accounts:
            company.account_journal_early_pay_discount_gain_account_id = (
                discount_gain_accounts[0].id
            )
            _logger.info(
                f"Set early payment discount gain account to: {discount_gain_accounts[0].code} - {discount_gain_accounts[0].name}"
            )
        elif income_accounts:
            company.account_journal_early_pay_discount_gain_account_id = (
                income_accounts[0].id
            )
            _logger.info(
                f"Set early payment discount gain account to general income: {income_accounts[0].code} - {income_accounts[0].name}"
            )

        # Final verification
        _logger.info(
            f"FINAL - Early payment discount loss account: {company.account_journal_early_pay_discount_loss_account_id.code if company.account_journal_early_pay_discount_loss_account_id else 'None'}"
        )
        _logger.info(
            f"FINAL - Early payment discount gain account: {company.account_journal_early_pay_discount_gain_account_id.code if company.account_journal_early_pay_discount_gain_account_id else 'None'}"
        )
