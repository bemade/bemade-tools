# -*- coding: utf-8 -*-
import logging
from odoo import api, models
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

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
        ar_account = accounts.filtered(lambda a: a.account_type == "asset_receivable")[
            :1
        ]
        ap_account = accounts.filtered(lambda a: a.account_type == "liability_payable")[
            :1
        ]

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
        }

    @ETL.transform()
    def transform_journals(self, ctx: ETLContext, extracted):
        """Create journal configuration using SAP accounts and archive default journals."""
        data = extracted.get("extract_journal_accounts") or {}
        cash_accounts = data.get("cash_accounts")
        ar_account = data.get("ar_account")
        ap_account = data.get("ap_account")
        income_account = data.get("income_account")
        expense_account = data.get("expense_account")

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

        journal_vals = []

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

        # 4. Bank Journals (one per cash account)
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
        return journal_vals

    @ETL.load()
    def load_journals(self, ctx: ETLContext, transformed):
        """Create account.journal records and mark chart template as installed."""
        journal_vals = transformed.get("transform_journals", [])

        if not journal_vals:
            _logger.info(
                "No new journals to create (all required journals already exist)"
            )
        else:
            journals = ctx.env["account.journal"].create(journal_vals)
            _logger.info(
                f"Created {len(journals)} account.journal records: {', '.join(journals.mapped('code'))}"
            )

        # Mark chart template as installed to prevent Odoo from auto-installing
        # its default chart template after module loading completes
        company = ctx.env.company
        if not company.chart_template:
            company.chart_template = "sap_imported"
            _logger.info(
                "Marked chart template as 'sap_imported' to prevent auto-installation"
            )
