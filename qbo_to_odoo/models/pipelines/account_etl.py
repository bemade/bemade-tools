"""QuickBooks Online Account ETL Pipeline

This module handles the migration of Chart of Accounts from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Any, Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .move_posting_helpers import reconcile_at_amount
from .utils import get_api_client

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
    "Credit Card": "asset_cash",  # treated as bank for reconciliation
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
        api_client = get_api_client(ctx)

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
            # Accounts referenced by company fields that will be reassigned
            # by _set_account_defaults — safe to clear and archive.
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

            # Exclude system accounts that are still needed by other
            # pipelines (e.g. the transfer pipeline uses the transit account
            # for cross-currency postings).
            keep_accounts = ctx.env["account.account"]
            if company.transfer_account_id and company.transfer_account_id in odoo_default_accounts:
                keep_accounts |= company.transfer_account_id
                _logger.info(
                    f"Keeping company.transfer_account_id "
                    f"({company.transfer_account_id.code}) active"
                )
            odoo_default_accounts -= keep_accounts

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

        _logger.info(
            f"Transformed {len(account_vals)} accounts, skipped {skipped}"
        )
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

    def _set_account_defaults(self, ctx: ETLContext) -> None:
        """Set proper account defaults based on imported QBO accounts.

        Uses QBO API AccountSubType queries to find the correct accounts for
        each company-level setting, then falls back to Odoo account type
        filtering for journal and product category defaults.
        """
        company = ctx.env.company
        IrDefault = ctx.env["ir.default"].sudo()
        api_client = get_api_client(ctx)

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
                ir_default_model="product.category",
                ir_default_field="property_account_income_categ_id",
                journal_type="sale",
            )
            self._set_account_from_qbo_subtype(
                ctx,
                api_client,
                company,
                qbo_accounts,
                fields=["expense_account_id"],
                label="product expense",
                account_type="Cost of Goods Sold",
                ir_default_model="product.category",
                ir_default_field="property_account_expense_categ_id",
                journal_type="purchase",
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

        # --- Default bank account for new journals ---
        # Pick the active QBO Bank account with the highest balance
        # (most likely the primary operating account).
        if api_client:
            bank_account = self._pick_primary_bank_account(
                api_client, qbo_accounts,
            )
            if bank_account:
                IrDefault.set(
                    "account.journal", "default_account_id", bank_account.id
                )
                _logger.info(
                    f"Set default bank account: "
                    f"{bank_account.code} - {bank_account.name}"
                )

        # Trigger auto-detection of AR/AP accounts (this already exists in qbo_connection)
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection._auto_detect_default_accounts()

    @staticmethod
    def _pick_primary_bank_account(api_client, qbo_accounts):
        """Return the Odoo account for the QBO Bank with the highest balance.

        Queries the QBO API for active Bank-type accounts (excludes Credit
        Card, Undeposited Funds, etc.) and picks the one with the largest
        absolute ``CurrentBalance``.  Returns ``None`` if no match.
        """
        qbo_banks = api_client.query_all(
            entity="Account",
            where="AccountType = 'Bank' AND Active = true",
        )
        if not qbo_banks:
            _logger.warning("No active QBO Bank accounts found for default")
            return None

        # Sort by absolute balance descending
        qbo_banks.sort(
            key=lambda a: abs(float(a.get("CurrentBalance", 0) or 0)),
            reverse=True,
        )

        # Find the first one that exists in Odoo
        for qbo_bank in qbo_banks:
            qbo_id = int(qbo_bank.get("Id", 0))
            match = qbo_accounts.filtered(lambda a, qid=qbo_id: a.qbo_id == qid)
            if match:
                _logger.info(
                    f"Primary bank account: QBO '{qbo_bank.get('Name')}' "
                    f"(balance {qbo_bank.get('CurrentBalance')})"
                )
                return match[0]

        _logger.warning("No QBO Bank accounts matched imported Odoo accounts")
        return None

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
        journal_type: Optional[str] = None,
    ) -> None:
        """Find a QBO account by AccountSubType/AccountType and set company fields.

        Queries the QBO API for accounts matching the given filters,
        finds the corresponding Odoo account (lowest code), and sets the
        specified company fields, ir.default, and/or journal defaults.

        Args:
            ctx: ETL context.
            api_client: QBO API client.
            company: res.company record.
            qbo_accounts: All QBO-imported Odoo accounts.
            fields: List of company field names to set.
            label: Human-readable label for logging.
            subtype: Optional QBO AccountSubType filter.
            account_type: Optional QBO AccountType filter.
            ir_default_model: If set, use ir.default for this model/field.
            ir_default_field: Field name for ir.default.
            journal_type: If set (e.g. "sale", "purchase"), update the
                default_account_id on matching journals.
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

        # Set company-level fields
        for field_name in fields:
            setattr(company, field_name, account.id)
        if fields:
            _logger.info(
                f"Set {label} account(s) to: {account.code} - {account.name}"
            )

        # Set ir.default for product categories (or other models)
        if ir_default_model and ir_default_field:
            ctx.env["ir.default"].sudo().set(
                ir_default_model, ir_default_field, account.id
            )
            _logger.info(
                f"Set {label} default ({ir_default_model}.{ir_default_field}): "
                f"{account.code} - {account.name}"
            )

        # Set default account on journals of the given type
        if journal_type:
            journals = ctx.env["account.journal"].search(
                [("type", "=", journal_type), ("company_id", "=", company.id)]
            )
            if journals:
                journals.write({"default_account_id": account.id})
                _logger.info(
                    f"Updated {len(journals)} {journal_type} journals with "
                    f"default account: {account.code} - {account.name}"
                )


@ETL.pipeline(
    target_model="account.account",
    importer_name="qbo.account.finalizer",
    sap_source="Account",
    depends_on=[
        "qbo.payment.importer",
        "qbo.journal.entry.importer",
        "qbo.transfer.importer",
        "qbo.deposit.importer",
        "qbo.expense.importer",
        "qbo.sales.receipt.importer",
        "qbo.refund.receipt.importer",
        "qbo.cc.payment.importer",
        "qbo.xlsx.fallback",
    ],
)
class QboAccountFinalizer(models.AbstractModel):
    """Archive inactive QBO accounts after all transactions are posted.

    This must run after every transaction pipeline so that moves referencing
    inactive accounts can be posted before the accounts are archived.
    """

    _name = "qbo.account.finalizer"
    _description = "QBO Account Finalizer"

    @ETL.extract("Account")
    def extract_inactive_accounts(self, ctx: ETLContext) -> List[Dict]:
        """Fetch inactive accounts from QBO."""
        api_client = get_api_client(ctx)
        accounts = api_client.query_all(
            entity="Account", where="Active = false", order_by="Id"
        )
        _logger.info(f"Found {len(accounts)} inactive accounts in QBO")
        return accounts

    @ETL.transform()
    def transform_inactive_accounts(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[int]:
        """Collect QBO IDs of inactive accounts to archive."""
        accounts = extracted.get("extract_inactive_accounts", [])
        return [int(a["Id"]) for a in accounts if a.get("Id")]

    @ETL.load()
    def load_archive_accounts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Retry failed credit applications, then archive inactive accounts."""
        # ── Reconciliation retry ──
        # Phase 4 credit applications run per-chunk during the payment
        # pipeline. CMs partially consumed at one FX rate may leave
        # residuals that prevent the next chunk from fully reconciling.
        # Now that all chunks are committed, retry unmatched pairs.
        self._retry_credit_reconciliation(ctx)

        inactive_qbo_ids = transformed.get("transform_inactive_accounts", [])
        if not inactive_qbo_ids:
            _logger.info("No inactive QBO accounts to archive")
            return

        accounts_to_archive = (
            ctx.env["account.account"]
            .with_context(active_test=False)
            .search([("qbo_id", "in", inactive_qbo_ids)])
        )

        if not accounts_to_archive:
            _logger.info(
                f"No Odoo accounts found matching {len(inactive_qbo_ids)} "
                f"inactive QBO IDs — nothing to archive"
            )
            return

        already_archived = accounts_to_archive.filtered(lambda a: not a.active)
        to_archive = accounts_to_archive - already_archived

        if already_archived:
            _logger.info(
                f"{len(already_archived)} QBO-imported accounts were already "
                f"archived: {', '.join(already_archived.mapped('code'))}"
            )

        if to_archive:
            to_archive.write({"active": False})
            _logger.info(
                f"Archived {len(to_archive)} QBO-imported accounts that are "
                f"inactive in QBO: {', '.join(to_archive.mapped('code'))}"
            )

    @staticmethod
    def _retry_credit_reconciliation(ctx: ETLContext):
        """Retry failed reconciliations after all payment chunks complete.

        Handles two cases that fail during multiprocessing:
        1. Credit notes/vendor credits partially consumed at different FX rates
        2. Payment entries that raced with invoice chunks on the same partner

        Runs single-threaded after all pipelines, so no chunk contention.
        """
        AML = ctx.env["account.move.line"]
        grand_total = 0

        # SQL to find unreconciled credit↔debit pairs by partner+account_type.
        # Covers CM/VC (refunds) and payment entries with unreconciled lines.
        _PAIRS_SQL = """
            WITH open_credits AS (
                SELECT aml.id AS line_id, aml.partner_id,
                       aa.account_type
                FROM account_move_line aml
                JOIN account_account aa ON aa.id = aml.account_id
                JOIN account_move am ON am.id = aml.move_id
                WHERE aml.reconciled = false
                  AND aml.amount_residual < 0
                  AND aa.account_type IN ('asset_receivable', 'liability_payable')
                  AND am.state = 'posted'
                  AND aml.partner_id IS NOT NULL
                  AND am.move_type IN ('out_refund', 'in_refund')
            ),
            open_debits AS (
                SELECT aml.id AS line_id, aml.partner_id,
                       aa.account_type
                FROM account_move_line aml
                JOIN account_account aa ON aa.id = aml.account_id
                JOIN account_move am ON am.id = aml.move_id
                WHERE aml.reconciled = false
                  AND aml.amount_residual > 0
                  AND aa.account_type IN ('asset_receivable', 'liability_payable')
                  AND am.state = 'posted'
                  AND am.move_type IN ('out_invoice', 'in_invoice')
                  AND aml.partner_id IS NOT NULL
            )
            SELECT DISTINCT ON (oc.line_id)
                   oc.line_id AS credit_line_id,
                   od.line_id AS debit_line_id
            FROM open_credits oc
            JOIN open_debits od
              ON od.account_type = oc.account_type
             AND od.partner_id = oc.partner_id
            ORDER BY oc.line_id, od.line_id
        """

        # Loop: each pass may free up new pairings as residuals change.
        iteration = 0
        while True:
            iteration += 1
            ctx.env.cr.execute(_PAIRS_SQL)
            pairs = ctx.env.cr.fetchall()
            if not pairs:
                break

            _logger.info(
                "Reconciliation retry pass %d: %d pairs to try",
                iteration, len(pairs),
            )
            reconciled = 0
            for credit_id, debit_id in pairs:
                with ctx.skippable(
                    f"retry reconcile credit={credit_id} debit={debit_id}"
                ):
                    credit_line = AML.browse(credit_id)
                    debit_line = AML.browse(debit_id)
                    if credit_line.reconciled or debit_line.reconciled:
                        continue
                    cap = min(
                        abs(credit_line.amount_residual_currency),
                        abs(debit_line.amount_residual_currency),
                    )
                    if cap < 0.01:
                        continue
                    reconcile_at_amount(credit_line, debit_line, cap)
                    reconciled += 1
            _logger.info(
                "Reconciliation retry pass %d: %d/%d resolved",
                iteration, reconciled, len(pairs),
            )
            grand_total += reconciled
            if reconciled == 0:
                break  # no progress, stop

        if grand_total:
            _logger.info(
                "Reconciliation retry: %d total reconciliations in %d passes",
                grand_total, iteration,
            )
        else:
            _logger.info("Reconciliation retry: no unmatched pairs found")
