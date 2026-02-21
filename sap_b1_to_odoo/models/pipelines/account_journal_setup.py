# -*- coding: utf-8 -*-
import logging
from odoo import api, models
from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.journal",
    importer_name="account.journal.setup",
    sap_source="oact",
    depends_on=[
        "res.company.importer",
        "account.account.importer",  # SAP CoA must be imported first
    ],
    allow_multiprocessing=False,
)
class AccountJournalSetup(models.AbstractModel):
    _name = "account.journal.setup"
    _description = "Create minimal journals using SAP chart of accounts"

    @ETL.extract("oact")
    def extract_journal_accounts(self, ctx: ETLContext):
        """Find suitable accounts from SAP CoA for journal default accounts.

        Looks for:
        - Cash accounts (for bank journals)
        - AR account (for sale journal)
        - AP account (for purchase journal)
        - Income account (for sale journal default)
        - Expense account (for purchase journal default)
        """
        # Get all imported accounts
        accounts = ctx.env["account.account"].search(
            [("company_ids", "in", [ctx.env.company.id])]
        )

        # Find specific account types
        cash_accounts = accounts.filtered(lambda a: a.account_type == "asset_cash")

        # Find the most-used AR and AP accounts from SAP business partners (ocrd.debpayacct).
        # SAP often has per-partner receivable/payable sub-accounts; we want the main ones.
        ar_account = self._find_most_used_account(ctx, accounts, "asset_receivable")
        ap_account = self._find_most_used_account(ctx, accounts, "liability_payable")

        # Find the most common inventory valuation account from SAP categories
        # Query SAP OITB for the most used balinvntac (inventory account)
        ctx.cr.execute(
            """
            SELECT a.formatcode, COUNT(*) as cnt
            FROM oitb o
            JOIN oact a ON o.balinvntac = a.acctcode
            WHERE o.balinvntac IS NOT NULL AND o.balinvntac != ''
            GROUP BY a.formatcode
            ORDER BY cnt DESC
            LIMIT 1
        """
        )
        row = ctx.cr.fetchone()
        stock_valuation_account = None
        if row:
            sap_acct_code = row[0]
            stock_valuation_account = ctx.env["account.account"].search(
                [
                    ("sap_acct_code", "=", sap_acct_code),
                    ("company_ids", "in", [ctx.env.company.id]),
                ],
                limit=1,
            )

        # Find default income/expense accounts - use the top-level (shortest code) in each category
        # Sort by: 1) code length (shortest first), 2) alphanumeric order
        # This gets the first root account (e.g., "400000" before "410000", "5" before "6")
        income_accounts = accounts.filtered(lambda a: a.account_type == "income")
        if income_accounts:
            # Sort by code length, then alphanumerically
            income_account = income_accounts.sorted(lambda a: (len(a.code), a.code))[:1]
        else:
            income_account = ctx.env["account.account"]

        # For expenses, we want the first one (likely COGS at "5" or operating at "6")
        # Sorting by length then code ensures we get "5" before "6" if both are root-level
        expense_accounts = accounts.filtered(lambda a: a.account_type == "expense")
        if expense_accounts:
            # Sort by code length, then alphanumerically - gets "5" before "6"
            expense_account = expense_accounts.sorted(lambda a: (len(a.code), a.code))[
                :1
            ]
        else:
            expense_account = ctx.env["account.account"]

        return {
            "cash_accounts": cash_accounts,
            "ar_account": ar_account,
            "ap_account": ap_account,
            "income_account": income_account,
            "expense_account": expense_account,
            "stock_valuation_account": stock_valuation_account,
        }

    @api.model
    def _find_most_used_account(self, ctx, accounts, account_type):
        """Find the most-used account of a given type from SAP business partners.

        Queries SAP ocrd.debpayacct to find which receivable/payable account
        is assigned to the most partners, then matches it to an imported Odoo account.
        Falls back to the first account of that type if no SAP match is found.
        """
        ctx.cr.execute(
            """
            SELECT a.formatcode, COUNT(*) as cnt
            FROM ocrd bp
            JOIN oact a ON bp.debpayacct = a.acctcode
            WHERE bp.debpayacct IS NOT NULL
            GROUP BY a.formatcode
            ORDER BY cnt DESC
            """
        )
        typed_accounts = accounts.filtered(lambda a: a.account_type == account_type)
        for row in ctx.cr.fetchall():
            sap_code = row[0]
            match = typed_accounts.filtered(lambda a, c=sap_code: a.sap_acct_code == c)
            if match:
                _logger.info(
                    f"Most-used {account_type} account from SAP: "
                    f"{match[0].display_name} ({row[1]} partners)"
                )
                return match[0]
        # Fallback: first account of this type
        if typed_accounts:
            _logger.warning(
                f"No SAP partner match for {account_type}, "
                f"falling back to {typed_accounts[0].display_name}"
            )
            return typed_accounts[0]
        return ctx.env["account.account"]

    @ETL.transform()
    def transform_journals(self, ctx: ETLContext, extracted):
        """Create journal configuration using SAP accounts and archive default journals."""
        data = extracted.get("extract_journal_accounts") or {}
        cash_accounts = data.get("cash_accounts")
        ar_account = data.get("ar_account")
        ap_account = data.get("ap_account")
        income_account = data.get("income_account")
        expense_account = data.get("expense_account")
        stock_valuation_account = data.get("stock_valuation_account")

        # Check existing journals (including archived ones to avoid unique constraint violations)
        existing_journals = (
            ctx.env["account.journal"]
            .with_context(active_test=False)
            .search([("company_id", "=", ctx.env.company.id)])
        )
        existing_codes = set(existing_journals.mapped("code"))

        # Get active journals for type checking
        active_journals = existing_journals.filtered(lambda j: j.active)
        existing_types = {j.type for j in active_journals}

        # Archive default Odoo journals (like BNK1) that have no transactions
        default_journals = active_journals.filtered(
            lambda j: j.code in ["BNK1", "CSH1", "STJ", "EXCH", "CABA"]
        )
        if default_journals:
            # Check each journal for moves
            unused_defaults = ctx.env["account.journal"]
            for journal in default_journals:
                has_moves = ctx.env["account.move"].search_count(
                    [("journal_id", "=", journal.id)], limit=1
                )
                if not has_moves:
                    unused_defaults |= journal

            if unused_defaults:
                unused_defaults.write({"active": False})
                _logger.info(
                    f"Archived {len(unused_defaults)} unused default journals: {', '.join(unused_defaults.mapped('code'))}"
                )

        # Fix up existing journals whose default_account_id points to an archived account.
        # This happens when Odoo auto-creates journals (e.g. INV, BILL) with default
        # accounts that the CoA pipeline later archives in favour of SAP accounts.
        journal_updates = []
        for journal in active_journals:
            if not journal.default_account_id or journal.default_account_id.active:
                continue
            replacement = None
            if journal.type == "sale" and income_account:
                replacement = income_account
            elif journal.type == "purchase" and expense_account:
                replacement = expense_account
            if replacement:
                journal_updates.append((journal, replacement))
                _logger.info(
                    f"Will re-point {journal.code} default account "
                    f"from archived {journal.default_account_id.display_name} "
                    f"to {replacement.display_name}"
                )

        journal_vals = []

        # Update existing journals if their default_account_id points to an archived account
        for journal in active_journals:
            if journal.type == "sale" and income_account:
                if (
                    not journal.default_account_id
                    or not journal.default_account_id.active
                ):
                    journal.default_account_id = income_account
                    _logger.info(
                        f"Updated {journal.code} journal default_account_id to {income_account.code}"
                    )
            elif journal.type == "purchase" and expense_account:
                if (
                    not journal.default_account_id
                    or not journal.default_account_id.active
                ):
                    journal.default_account_id = expense_account
                    _logger.info(
                        f"Updated {journal.code} journal default_account_id to {expense_account.code}"
                    )

        # 1. Sale Journal
        # Use INV code to match Odoo's default (accountant module creates INV for sales)
        if "sale" not in existing_types and "INV" not in existing_codes:
            vals = {
                "name": "Customer Invoices",
                "code": "INV",
                "type": "sale",
                "company_id": ctx.env.company.id,
            }
            # For sale journals, default_account_id is the default income account
            if income_account:
                vals["default_account_id"] = income_account.id
            journal_vals.append(vals)
            _logger.info("Will create Sale journal (INV)")

        # 2. Purchase Journal
        # Use BILL code to match Odoo's default (accountant module creates BILL for purchases)
        if "purchase" not in existing_types and "BILL" not in existing_codes:
            vals = {
                "name": "Vendor Bills",
                "code": "BILL",
                "type": "purchase",
                "company_id": ctx.env.company.id,
            }
            # For purchase journals, default_account_id is the default expense account
            if expense_account:
                vals["default_account_id"] = expense_account.id
            journal_vals.append(vals)
            _logger.info("Will create Purchase journal (BILL)")

        # 3. General Journal
        if "general" not in existing_types and "MISC" not in existing_codes:
            vals = {
                "name": "Miscellaneous Operations",
                "code": "MISC",
                "type": "general",
                "company_id": ctx.env.company.id,
            }
            journal_vals.append(vals)
            _logger.info("Will create General journal (MISC)")

        # 4. SAP Payment Reconciliation Journal (for payment import)
        if "SAPRC" not in existing_codes:
            vals = {
                "name": "SAP Payment Reconciliation",
                "code": "SAPRC",
                "type": "general",
                "company_id": ctx.env.company.id,
            }
            journal_vals.append(vals)
            _logger.info("Will create SAP Payment Reconciliation journal (SAPRC)")

        # 5. Stock Journal for inventory valuation
        if "STJ" not in existing_codes:
            vals = {
                "name": "Stock Journal",
                "code": "STJ",
                "type": "general",
                "company_id": ctx.env.company.id,
            }
            journal_vals.append(vals)
            _logger.info("Will create Stock journal (STJ)")

        # 6. Bank Journals (one per cash account)
        for idx, cash_account in enumerate(cash_accounts or [], start=1):
            journal_code = f"BNK{idx}"
            if journal_code in existing_codes:
                continue

            vals = {
                "name": cash_account.name,
                "code": journal_code,
                "type": "bank",
                "company_id": ctx.env.company.id,
                "default_account_id": cash_account.id,
            }
            journal_vals.append(vals)
            _logger.info(
                f"Will create Bank journal {journal_code} for {cash_account.name}"
            )

        _logger.info(f"Prepared {len(journal_vals)} journals to create")
        return {
            "journal_vals": journal_vals,
            "journal_updates": journal_updates,
            "ar_account": ar_account,
            "ap_account": ap_account,
            "stock_valuation_account": stock_valuation_account,
        }

    @ETL.load()
    def load_journals(self, ctx: ETLContext, transformed):
        """Create account.journal records and mark chart template as installed."""
        data = transformed.get("transform_journals") or {}
        journal_vals = data.get("journal_vals", [])
        journal_updates = data.get("journal_updates", [])
        stock_valuation_account = data.get("stock_valuation_account")

        ar_account = data.get("ar_account")
        ap_account = data.get("ap_account")

        # Set company-wide default receivable/payable accounts via ir.default.
        # The CoA pipeline archives Odoo's default accounts; without this,
        # partners inherit the archived defaults and invoice posting fails.
        IrDefault = ctx.env["ir.default"]
        if ar_account:
            IrDefault.set(
                "res.partner",
                "property_account_receivable_id",
                ar_account.id,
                company_id=ctx.env.company.id,
            )
            _logger.info(f"Set default receivable account to {ar_account.display_name}")
        if ap_account:
            IrDefault.set(
                "res.partner",
                "property_account_payable_id",
                ap_account.id,
                company_id=ctx.env.company.id,
            )
            _logger.info(f"Set default payable account to {ap_account.display_name}")

        # Re-point journals whose default account was archived by the CoA pipeline
        for journal, replacement in journal_updates:
            journal.default_account_id = replacement
            _logger.info(
                f"Re-pointed {journal.code} default account to {replacement.display_name}"
            )

        if not journal_vals:
            _logger.info(
                "No new journals to create (all required journals already exist)"
            )
        else:
            journals = ctx.env["account.journal"].create(journal_vals)
            _logger.info(
                f"Created {len(journals)} account.journal records: {', '.join(journals.mapped('code'))}"
            )

        # Set company stock valuation account and journal
        company = ctx.env.company
        company_vals = {}

        # Set stock valuation account from SAP
        if stock_valuation_account and not company.account_stock_valuation_id:
            company_vals["account_stock_valuation_id"] = stock_valuation_account.id
            _logger.info(
                f"Setting company stock valuation account to {stock_valuation_account.code} ({stock_valuation_account.name})"
            )

        # Set stock journal
        stock_journal = ctx.env["account.journal"].search(
            [("code", "=", "STJ"), ("company_id", "=", company.id)], limit=1
        )
        if stock_journal and not company.account_stock_journal_id:
            company_vals["account_stock_journal_id"] = stock_journal.id
            _logger.info(f"Setting company stock journal to {stock_journal.code}")

        if company_vals:
            company.write(company_vals)

        # Note: chart_template field is a dynamic selection - only valid template codes
        # from installed modules are allowed. We don't set it here since SAP import
        # provides its own CoA. Odoo will not auto-install a chart template if accounts
        # already exist for the company.
