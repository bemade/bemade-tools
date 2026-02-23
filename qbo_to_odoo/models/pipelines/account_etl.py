"""QuickBooks Online Account ETL Pipeline

This module handles the migration of Chart of Accounts from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Any, Dict, List, Optional

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
            # Clear company fields that reference default accounts before archiving,
            # otherwise Odoo will error when those accounts are used (e.g. during
            # payment posting which checks early payment discount accounts)
            account_fields = [
                "account_journal_early_pay_discount_loss_account_id",
                "account_journal_early_pay_discount_gain_account_id",
                "default_cash_difference_income_account_id",
                "default_cash_difference_expense_account_id",
                "account_journal_suspense_account_id",
                "income_currency_exchange_account_id",
                "expense_currency_exchange_account_id",
                "deferred_expense_account_id",
                "deferred_revenue_account_id",
                "income_account_id",
                "expense_account_id",
            ]
            for field_name in account_fields:
                account = getattr(company, field_name, None)
                if account and account in odoo_default_accounts:
                    setattr(company, field_name, False)
                    _logger.info(
                        f"Cleared company.{field_name} ({account.code}) "
                        f"before archiving"
                    )

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

            # Override: "Undeposited Funds" is classified as Other Current Asset
            # in QBO but functions as a bank/cash account for payment processing
            acct_name = account.get("Name", "")
            if "undeposited funds" in acct_name.lower():
                odoo_type = "asset_cash"

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

            vals = {
                "name": account.get("Name", ""),
                "code": code,
                "account_type": odoo_type,
                "reconcile": reconcile,
                "qbo_id": int(account.get("Id")),
                "company_ids": [(4, company.id)],
            }

            # Preserve currency from QBO (e.g. multi-currency AR/AP accounts)
            currency_ref = account.get("CurrencyRef", {}).get("value")
            if currency_ref:
                currency = ctx.env["res.currency"].search(
                    [("name", "=", currency_ref)], limit=1
                )
                if currency:
                    vals["currency_id"] = currency.id

            account_vals.append(vals)

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
        """Set proper account defaults based on imported QBO accounts.

        Uses QBO API AccountSubType queries to find the correct accounts for
        each company-level setting, then falls back to Odoo account type
        filtering for journal and product category defaults.
        """
        company = ctx.env.company
        IrDefault = ctx.env["ir.default"].sudo()
        api_client = ctx.get_config("api_client")

        # Find common account types from QBO accounts
        qbo_accounts = ctx.env["account.account"].search(
            [("company_ids", "in", [company.id]), ("qbo_id", "!=", False)]
        )

        if not qbo_accounts:
            _logger.warning("No QBO accounts found for setting defaults")
            return

        _logger.info(f"Setting account defaults from {len(qbo_accounts)} QBO accounts")

        # --- QBO AccountSubType-based company settings ---
        if api_client:
            self._set_account_from_qbo_subtype(
                ctx,
                api_client,
                company,
                qbo_accounts,
                subtype="ExchangeGainOrLoss",
                fields=[
                    "income_currency_exchange_account_id",
                    "expense_currency_exchange_account_id",
                ],
                label="exchange difference",
            )
            self._set_account_from_qbo_subtype(
                ctx,
                api_client,
                company,
                qbo_accounts,
                subtype="DiscountsRefundsGiven",
                fields=[
                    "account_journal_early_pay_discount_loss_account_id",
                    "account_journal_early_pay_discount_gain_account_id",
                ],
                label="early payment discount",
            )
            self._set_account_from_qbo_subtype(
                ctx,
                api_client,
                company,
                qbo_accounts,
                subtype="PrepaidExpensesPayable",
                fields=["deferred_expense_account_id"],
                label="deferred expense",
            )
            self._set_account_from_qbo_subtype(
                ctx,
                api_client,
                company,
                qbo_accounts,
                subtype="AccrualsAndDeferredIncome",
                fields=["deferred_revenue_account_id"],
                label="deferred revenue",
            )
            self._set_account_from_qbo_subtype(
                ctx,
                api_client,
                company,
                qbo_accounts,
                subtype="SalesOfProductIncome",
                fields=["income_account_id"],
                label="product income",
            )
            self._set_account_from_qbo_subtype(
                ctx,
                api_client,
                company,
                qbo_accounts,
                fields=["expense_account_id"],
                label="product expense",
                account_type="Cost of Goods Sold",
            )
            self._set_account_from_qbo_subtype(
                ctx,
                api_client,
                company,
                qbo_accounts,
                subtype="Inventory",
                fields=[],
                label="stock valuation",
                ir_default_model="product.category",
                ir_default_field="property_stock_valuation_account_id",
            )

        # --- Odoo account type-based defaults for journals ---

        # Default bank account (for bank journals)
        bank_accounts = qbo_accounts.filtered(
            lambda a: a.account_type == "asset_cash"
        ).sorted("code")
        if bank_accounts:
            bank_account = bank_accounts[0]
            IrDefault.set("account.journal", "default_account_id", bank_account.id)
            _logger.info(
                f"Set default bank account: {bank_account.code} - {bank_account.name}"
            )

        # Default income account (for sale journals + product categories)
        income_accounts = qbo_accounts.filtered(
            lambda a: a.account_type == "income"
        ).sorted("code")
        if income_accounts:
            income_account = income_accounts[0]
            IrDefault.set(
                "product.category",
                "property_account_income_categ_id",
                income_account.id,
            )
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

        # Default expense account (for purchase journals + product categories)
        expense_accounts = qbo_accounts.filtered(
            lambda a: a.account_type in ["expense", "expense_direct_cost"]
        ).sorted("code")
        if expense_accounts:
            expense_account = expense_accounts[0]
            IrDefault.set(
                "product.category",
                "property_account_expense_categ_id",
                expense_account.id,
            )
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

        # Trigger auto-detection of AR/AP accounts (this already exists in qbo_connection)
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection._auto_detect_default_accounts()

    def _set_account_from_qbo_subtype(
        self,
        ctx: ETLContext,
        api_client,
        company,
        qbo_accounts,
        fields: list,
        label: str,
        subtype: Optional[str] = None,
        account_type: Optional[str] = None,
        ir_default_model: Optional[str] = None,
        ir_default_field: Optional[str] = None,
    ) -> None:
        """Find a QBO account by AccountSubType/AccountType and set company fields.

        Queries the QBO API for accounts matching the given filters,
        finds the corresponding Odoo account (lowest code), and sets the
        specified company fields or ir.default.

        Args:
            ctx: ETL context.
            api_client: QBO API client.
            company: res.company record.
            qbo_accounts: All QBO-imported Odoo accounts.
            fields: List of company field names to set.
            label: Human-readable label for logging.
            subtype: Optional QBO AccountSubType filter.
            account_type: Optional QBO AccountType filter.
            ir_default_model: If set, use ir.default instead of company fields.
            ir_default_field: Field name for ir.default.
        """
        conditions = ["Active = true"]
        if subtype:
            conditions.append(f"AccountSubType = '{subtype}'")
        if account_type:
            conditions.append(f"AccountType = '{account_type}'")
        where = " AND ".join(conditions)

        qbo_accts = api_client.query_all(entity="Account", where=where)
        filter_desc = subtype or account_type
        if not qbo_accts:
            _logger.warning(
                f"No QBO account matching '{filter_desc}' found. "
                f"{label.capitalize()} accounts not set."
            )
            return

        # Find matching Odoo accounts sorted by code (lowest first)
        qbo_ids = [int(a.get("Id")) for a in qbo_accts]
        odoo_matches = qbo_accounts.filtered(lambda a: a.qbo_id in qbo_ids).sorted(
            "code"
        )

        if not odoo_matches:
            _logger.warning(
                f"QBO accounts for '{filter_desc}' found but no matching Odoo "
                f"accounts. {label.capitalize()} accounts not set."
            )
            return

        account = odoo_matches[0]

        if ir_default_model and ir_default_field:
            ctx.env["ir.default"].sudo().set(
                ir_default_model, ir_default_field, account.id
            )
            _logger.info(
                f"Set {label} account (ir.default): " f"{account.code} - {account.name}"
            )
        else:
            for field_name in fields:
                setattr(company, field_name, account.id)
            _logger.info(
                f"Set {label} account(s) to: " f"{account.code} - {account.name}"
            )
