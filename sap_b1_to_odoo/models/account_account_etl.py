# -*- coding: utf-8 -*-
import logging
from odoo import api, models, Command
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.account",
    importer_name="account.account.importer",
    sap_source="oact",
    depends_on=["res.company.importer"],
    allow_multiprocessing=False,
)
class AccountAccountImporter(models.AbstractModel):
    _name = "account.account.importer"
    _description = "SAP Chart of Accounts Importer (OACT)"

    @ETL.extract("oact")
    def extract_accounts(self, ctx: ETLContext):
        """Extract postable accounts from SAP OACT with their root account group.

        We only import postable accounts (postable='Y').
        Non-postable accounts are typically group/header accounts.

        Uses recursive CTE to trace each account to its root group (Assets, Liabilities, etc.)
        """
        ctx.cr.execute(
            """
            WITH RECURSIVE hierarchy AS (
                -- Start with root accounts (no parent)
                SELECT acctcode, acctname, fathernum, acctcode as root_code, acctname as root_name
                FROM oact
                WHERE fathernum IS NULL
                
                UNION ALL
                
                -- Recursively join children to parents
                SELECT o.acctcode, o.acctname, o.fathernum, h.root_code, h.root_name
                FROM oact o
                JOIN hierarchy h ON o.fathernum = h.acctcode
            )
            SELECT 
                o.acctcode,
                o.acctname,
                o.finanse,
                o.postable,
                o.acttype,
                o.formatcode,
                o.currtotal,
                o.fctotal,
                o.fathernum,
                h.root_code,
                h.root_name
            FROM oact o
            LEFT JOIN hierarchy h ON o.acctcode = h.acctcode
            WHERE o.postable = 'Y'
            ORDER BY o.acctcode
        """
        )
        accounts = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(accounts)} postable accounts from SAP OACT")
        return {"accounts": accounts}

    @ETL.transform()
    def transform_accounts(self, ctx: ETLContext, extracted):
        """Transform SAP accounts to Odoo account.account vals.

        Maps SAP account types to Odoo account_type using:
        - finanse: Y = balance sheet, N = P&L
        - acttype: N (asset), L (liability), E (equity/expense), I (income)
        - Account code ranges for finer classification
        """
        data = extracted.get("extract_accounts") or {}
        sap_accounts = data.get("accounts", [])

        if not sap_accounts:
            _logger.info("No SAP accounts to transform")
            return []

        # Archive Odoo's default accounts (those without sap_acct_code) before importing SAP CoA
        # This ensures a clean slate with only SAP accounts
        existing_accounts = ctx.env["account.account"].search(
            [("company_ids", "in", [ctx.env.company.id])]
        )

        # Only archive accounts that are NOT from SAP (no sap_acct_code)
        odoo_default_accounts = existing_accounts.filtered(
            lambda a: not a.sap_acct_code
        )

        if odoo_default_accounts:
            _logger.info(
                f"Found {len(odoo_default_accounts)} Odoo default accounts (non-SAP) to archive"
            )
            # Check if any are in use (have journal items)
            accounts_with_moves = odoo_default_accounts.filtered(lambda a: a.used)
            if accounts_with_moves:
                _logger.warning(
                    f"Cannot archive {len(accounts_with_moves)} accounts that have journal entries: "
                    f"{', '.join(accounts_with_moves.mapped('code'))}"
                )
                # Only archive unused accounts
                accounts_to_archive = odoo_default_accounts - accounts_with_moves
            else:
                accounts_to_archive = odoo_default_accounts

            if accounts_to_archive:
                accounts_to_archive.write({"active": False})
                _logger.info(
                    f"Archived {len(accounts_to_archive)} unused Odoo default accounts"
                )

        existing_codes = set(existing_accounts.mapped("code"))

        account_vals = []
        for sap_acct in sap_accounts:
            raw_code = (
                sap_acct.get("formatcode") or sap_acct.get("acctcode") or ""
            ).strip()
            if not raw_code:
                _logger.warning(f"Skipping account with no code: {sap_acct}")
                continue

            # Sanitize code: Odoo only allows alphanumeric and dots
            # Replace spaces with dots to preserve visual grouping (e.g., "10210 000" → "10210.000")
            code = raw_code.replace(" ", ".")

            # Skip if already exists
            if code in existing_codes:
                continue

            name = (sap_acct.get("acctname") or "").strip()
            if not name:
                name = code

            # Determine account type
            account_type = self._infer_account_type(sap_acct)

            vals = {
                "code": code,  # Sanitized code (spaces → dots, e.g., "10210.000")
                "name": name,
                "account_type": account_type,
                "company_ids": [
                    Command.set([ctx.env.company.id])
                ],  # Many2many in Odoo 19
                "sap_acct_code": raw_code,  # Original SAP code (with spaces)
            }
            account_vals.append(vals)

        _logger.info(
            f"Transformed {len(account_vals)} new accounts (skipped {len(sap_accounts) - len(account_vals)} existing)"
        )
        return account_vals

    def _infer_account_type(self, sap_acct):
        """Infer Odoo account_type from SAP account data.

        Uses SAP's account hierarchy (root_name) to determine type:
        - Assets → asset_* types
        - Liabilities → liability_* types
        - Equity → equity types
        - Revenues → income types
        - Cost of Sales → expense_direct_cost
        - Expenses → expense types
        - Other Revenues and Expenses → income_other or expense

        TODO: Make keyword matching configurable via a mapping table or configuration file
        to support different naming conventions across SAP B1 implementations.

        Odoo account_type options:
        - Assets: asset_receivable, asset_cash, asset_current, asset_non_current,
                  asset_prepayments, asset_fixed
        - Liabilities: liability_payable, liability_credit_card, liability_current,
                       liability_non_current
        - Equity: equity, equity_unaffected
        - Income: income, income_other
        - Expense: expense, expense_depreciation, expense_direct_cost
        """
        root_name = (sap_acct.get("root_name") or "").strip().lower()
        finanse = (sap_acct.get("finanse") or "").strip().upper()
        raw_code = (
            sap_acct.get("formatcode") or sap_acct.get("acctcode") or ""
        ).strip()
        code = raw_code.replace(" ", ".")  # Sanitize: replace spaces with dots
        name = (sap_acct.get("acctname") or "").strip().lower()

        # Classify by SAP root account group (most reliable)
        if "asset" in root_name:
            # Assets - classify by name and finanse flag
            if finanse == "Y" or "cash" in name or "bank" in name or "checking" in name:
                return "asset_cash"
            elif "receivable" in name or "a/r" in name:
                return "asset_receivable"
            elif "prepaid" in name:
                return "asset_prepayments"
            elif (
                "fixed" in name
                or "furniture" in name
                or "equipment" in name
                or "machinery" in name
                or "vehicle" in name
                or "building" in name
                or "leasehold" in name
            ):
                return "asset_fixed"
            elif "inventory" in name:
                return "asset_current"
            else:
                return "asset_current"  # Default for assets

        elif "liabilit" in root_name:
            # Liabilities - classify by name
            if "payable" in name or "a/p" in name:
                return "liability_payable"
            elif "credit card" in name:
                return "liability_credit_card"
            elif "long" in name or "long-term" in name or "note" in name:
                return "liability_non_current"
            else:
                return "liability_current"  # Default for liabilities

        elif "equity" in root_name:
            # Equity
            # Note: Odoo only allows ONE account with equity_unaffected type
            # For safety, we default all to 'equity' and let user manually set
            # the retained earnings account to equity_unaffected after import
            # TODO: Add logic to find and set the main retained earnings account
            return "equity"

        elif "revenue" in root_name:
            # Revenues
            return "income"

        elif "cost of sales" in root_name or "cost of" in root_name:
            # Cost of Sales (COGS)
            return "expense_direct_cost"

        elif "expense" in root_name:
            # Operating Expenses
            if "depreciation" in name or "depre" in name:
                return "expense_depreciation"
            else:
                return "expense"

        elif "other revenue" in root_name:
            # Other Revenues and Expenses - check account name
            if "revenue" in name or "income" in name or "gain" in name:
                return "income_other"
            else:
                return "expense"

        # Fallback if no root_name match
        else:
            _logger.warning(
                f"Could not classify account {code} ({name}) with root_name='{root_name}'. Defaulting to expense."
            )
            return "expense"

    @ETL.load()
    def load_accounts(self, ctx: ETLContext, transformed):
        """Create account.account records from transformed data."""
        account_vals = transformed.get("transform_accounts", [])

        if not account_vals:
            _logger.info("No new accounts to create")
            return

        accounts = ctx.env["account.account"].create(account_vals)
        _logger.info(f"Created {len(accounts)} account.account records from SAP OACT")
