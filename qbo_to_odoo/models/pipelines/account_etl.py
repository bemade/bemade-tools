"""QuickBooks Online Account ETL Pipeline

This module handles the migration of Chart of Accounts from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Any, Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .reconcile_pass import run_reconciliation_pass
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
            odoo_default_accounts = QboAccountFinalizer._filter_tax_referenced(
                ctx, odoo_default_accounts
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

            # Preserve currency from QBO (e.g. multi-currency AR/AP accounts).
            # Search including inactive currencies and activate any the QBO
            # books use: an inactive currency is absent from the importer's
            # currency map, so its transactions fall back to the company
            # currency and post at par (rate 1.0) instead of QBO's exchange
            # rate — fabricating FX. Activating it here (accounts import first)
            # lets every downstream pipeline resolve and rate it correctly.
            currency_ref = account.get("CurrencyRef", {}).get("value")
            if currency_ref:
                currency = ctx.env["res.currency"].with_context(
                    active_test=False
                ).search([("name", "=", currency_ref)], limit=1)
                if currency:
                    if not currency.active:
                        currency.active = True
                        _logger.info(
                            "Activated currency %s used by QBO account %s",
                            currency.name, account.get("Name", currency_ref),
                        )
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
        "qbo.journal.fallback",
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
        """Replay QBO applications, true up FX, archive inactive accounts."""
        # ── One-pass reconciliation replay ──
        # Every QBO settlement lives as application lines on a Payment or
        # BillPayment. Now that all transaction pipelines have committed,
        # replay each one as a reconciliation group against the imported
        # moves (see reconcile_pass.py).
        run_reconciliation_pass(ctx)

        # ── Realized-FX true-up ──
        # QBO books 100% of a foreign payment's realized FX inside the
        # payment (clearing AR/AP at the booking rate); Odoo defers it to
        # reconciliation EXCH entries that under-book on open many-to-many
        # chains, stranding the remainder on the AR/AP control account.
        # Book the per-payment shortfall to the exchange account now that
        # reconciliation is final. Must run AFTER run_reconciliation_pass.
        self._book_payment_fx_trueup(ctx)

        # Cent-scale rounding parity — after all structural corrections,
        # book the per-transaction rounding remainders (per-line rounding
        # differs between the systems) so every account ties to the source
        # GL to the penny. Must run LAST.
        self._book_gl_cent_trueup(ctx)

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
            to_archive = self._filter_tax_referenced(ctx, to_archive)

        if to_archive:
            to_archive.write({"active": False})
            _logger.info(
                f"Archived {len(to_archive)} QBO-imported accounts that are "
                f"inactive in QBO: {', '.join(to_archive.mapped('code'))}"
            )

    @staticmethod
    def _filter_tax_referenced(ctx: ETLContext, accounts):
        """Return *accounts* minus any account referenced by a tax repartition line.

        Searches ``account.tax.repartition.line`` with ``active_test=False`` so
        that repartition lines belonging to an inactive ``account.tax`` are still
        counted (the tax carries the active flag; the repartition line has none).
        Logs the excluded account codes at INFO level.

        :param ctx: ETL context carrying the ORM environment.
        :param accounts: ``account.account`` recordset to filter.
        :return: The input recordset minus any tax-referenced accounts.
        """
        if not accounts:
            return accounts
        referenced_ids = set(
            ctx.env["account.tax.repartition.line"]
            .with_context(active_test=False)
            .search([("account_id", "in", accounts.ids)])
            .mapped("account_id")
            .ids
        )
        skipped = accounts.filtered(lambda a: a.id in referenced_ids)
        if skipped:
            _logger.info(
                "Not archiving %d tax-referenced account(s): %s",
                len(skipped),
                ", ".join(skipped.mapped("code")),
            )
        return accounts - skipped

    @staticmethod
    def _book_payment_fx_trueup(ctx: ETLContext):
        """Recover the realized FX QBO books on payments but Odoo under-books.

        QBO clears a foreign payment's AR/AP at the *invoice booking rate* and
        posts 100% of the realized FX to the exchange gain/loss account inside
        the payment transaction — including zero-total payments that merely
        apply credit/debit notes.  Odoo instead books FX as reconciliation
        exchange-difference entries at its own rates, which diverge on open
        many-to-many chains and on credit-note applications, stranding the
        remainder on the AR/AP control account.

        Every reconciliation goes through ``reconcile_at_amount``, which stamps
        the exchange moves it creates with the driving QBO transaction
        (``ref = QBO_EXCH:<Kind>:<qbo_txn_id>``).  This finalizer then:

        1. Reverses ``QBO_EXCH:RETRY`` exchange moves (heuristic partner-level
           pairings with no driving txn — their FX is re-booked per-txn below).
        2. For every payment-type transaction in the JournalReport cache books::

               gap = QBO_exch(txn) - stamped_exchange_FX(txn)

           as a foreign-amount-zero home-currency adjustment — the exchange
           account against the AR/AP control account QBO's own GL used, dated
           to the transaction so each fiscal year ties to QBO.  The AR/AP leg
           carries no partner, so customer/vendor aging is untouched.

        This covers regular payments, embedded credit applications (stamped
        with their payment's txn), and pure zero-total applications (which
        have no ``account.payment`` record at all).  Runs once, after all
        reconciliation is final.  Idempotent via the ``QBO_FX_TRUEUP`` ref.
        """
        env = ctx.env
        company = env.company
        gain_acct = company.income_currency_exchange_account_id
        loss_acct = company.expense_currency_exchange_account_id
        exch_journal = company.currency_exchange_journal_id
        if not (gain_acct and loss_acct and exch_journal):
            _logger.warning(
                "FX true-up skipped: company exchange journal/accounts not "
                "configured (gain=%s loss=%s journal=%s)",
                gain_acct.code if gain_acct else None,
                loss_acct.code if loss_acct else None,
                exch_journal.code if exch_journal else None,
            )
            return

        # Guard against a double-run (e.g. finalizer re-executed on a DB that
        # already has the true-up).
        if env["account.move"].search_count(
            [("ref", "=like", "QBO_FX_TRUEUP%")]
        ):
            _logger.info("FX true-up already booked — skipping")
            return

        # The QBO cache stores home-currency amounts under QBO's own account
        # codes; the exchange account carries that code across import, so derive
        # it rather than hard-coding a chart-specific value.
        exch_accts = gain_acct | loss_acct
        exch_codes = tuple({a.code for a in exch_accts if a.code})
        exch_ids = tuple(exch_accts.ids)
        if not exch_codes:
            _logger.warning("FX true-up skipped: exchange accounts have no code")
            return

        # AR/AP control accounts by code — used to place each true-up's
        # counterpart on the same control account QBO's own GL used.
        arap_accts = env["account.account"].search(
            [("account_type", "in", ("asset_receivable", "liability_payable"))]
        )
        arap_by_code = {a.code: a for a in arap_accts if a.code}
        if not arap_by_code:
            _logger.warning("FX true-up skipped: no coded AR/AP accounts")
            return

        # Step 1 — reverse heuristic-retry FX.  RETRY-stamped exchange moves
        # come from partner-level pairings with no driving QBO transaction, so
        # their FX cannot be attributed to a cache txn; QBO's version of that
        # FX is booked per-txn in step 2, so leaving the retry FX in place
        # would double-count it.
        retry_moves = env["account.move"].search(
            [("ref", "=", "QBO_EXCH:RETRY"), ("state", "=", "posted")]
        )
        if retry_moves:
            reversals = retry_moves._reverse_moves(
                default_values_list=[
                    {"ref": "QBO_FX_RETRY_REVERSAL", "date": m.date}
                    for m in retry_moves
                ],
            )
            reversals.action_post()
            retry_fx = sum(
                l.balance
                for l in reversals.line_ids
                if l.account_id in exch_accts
            )
            _logger.info(
                "FX true-up: reversed %d heuristic-retry exchange moves "
                "(%.2f FX returned to AR/AP; re-booked per-txn below)",
                len(retry_moves), -retry_fx,
            )

        # Step 2 — uniform per-QBO-txn gap.  For every payment-type txn in the
        # JournalReport cache (regular payments, zero-total credit/debit-note
        # applications, and embedded applications alike):
        #
        #     gap = QBO_exch(txn) - stamped_exchange_FX(txn)
        #
        # The Odoo side is the exchange moves reconcile_at_amount stamped with
        # this txn's ``QBO_EXCH:<Kind>:<id>`` ref, so attribution is exact and
        # application FX (which has no account.payment record) is covered.
        # The control account comes from the txn's own AR/AP line in the cache.
        env.cr.execute(
            """
            WITH qbo_txn AS (
                SELECT CASE WHEN t.txn_type = 'Payment' THEN 'Payment'
                            ELSE 'BillPayment' END AS kind,
                       t.qbo_txn_id AS qid,
                       min(t.txn_date) AS dt,
                       sum(CASE WHEN l.account_code IN %(exch_codes)s
                                THEN l.debit - l.credit ELSE 0 END) AS qbo_exch
                FROM qbo_journal_cache_transaction t
                JOIN qbo_journal_cache_line l ON l.transaction_id = t.id
                WHERE t.txn_type IN ('Payment', 'Bill Payment (Cheque)',
                                     'Bill Payment (Credit Card)')
                  -- Skipped payments the journal fallback imported GL-as-is
                  -- already carry QBO's FX inside the fallback move.
                  AND NOT EXISTS (
                      SELECT 1 FROM account_move fm
                      WHERE fm.state = 'posted'
                        AND fm.ref = 'JNL-' || t.txn_type || '-' || t.qbo_txn_id
                  )
                GROUP BY 1, 2
            ),
            ctrl AS (
                -- Dominant AR/AP account per txn, from QBO's own GL lines.
                SELECT DISTINCT ON (kind, qid) kind, qid, account_code
                FROM (
                    SELECT CASE WHEN t.txn_type = 'Payment' THEN 'Payment'
                                ELSE 'BillPayment' END AS kind,
                           t.qbo_txn_id AS qid, l.account_code,
                           sum(abs(l.debit - l.credit)) AS weight
                    FROM qbo_journal_cache_transaction t
                    JOIN qbo_journal_cache_line l ON l.transaction_id = t.id
                    WHERE t.txn_type IN ('Payment', 'Bill Payment (Cheque)',
                                         'Bill Payment (Credit Card)')
                      AND l.account_code IN %(arap_codes)s
                    GROUP BY 1, 2, 3
                ) w
                ORDER BY kind, qid, weight DESC
            ),
            odoo_fx AS (
                SELECT split_part(m.ref, ':', 2) AS kind,
                       split_part(m.ref, ':', 3) AS qid,
                       sum(aml.balance) AS fx
                FROM account_move m
                JOIN account_move_line aml ON aml.move_id = m.id
                WHERE m.ref LIKE 'QBO\\_EXCH:%%'
                  AND m.ref != 'QBO_EXCH:RETRY'
                  AND m.state = 'posted'
                  AND aml.account_id IN %(exch_ids)s
                GROUP BY 1, 2
            )
            SELECT q.kind, q.qid, q.dt, c.account_code,
                   round((q.qbo_exch - COALESCE(o.fx, 0))::numeric, 2) AS gap
            FROM qbo_txn q
            LEFT JOIN ctrl c ON c.kind = q.kind AND c.qid = q.qid
            LEFT JOIN odoo_fx o ON o.kind = q.kind AND o.qid = q.qid
            WHERE abs(q.qbo_exch - COALESCE(o.fx, 0)) >= 0.01
            ORDER BY q.dt, q.kind, q.qid
            """,
            {
                "exch_ids": exch_ids,
                "exch_codes": exch_codes,
                "arap_codes": tuple(arap_by_code),
            },
        )
        rows = env.cr.fetchall()

        # Stamped FX with no cache counterpart would mean a reconciliation
        # driven by a txn the JournalReport doesn't know — surface it.
        env.cr.execute(
            """
            SELECT m.ref, round(sum(aml.balance)::numeric, 2)
            FROM account_move m
            JOIN account_move_line aml ON aml.move_id = m.id
            WHERE m.ref LIKE 'QBO\\_EXCH:%%'
              AND m.ref != 'QBO_EXCH:RETRY'
              AND m.state = 'posted'
              AND aml.account_id IN %(exch_ids)s
              AND NOT EXISTS (
                  SELECT 1 FROM qbo_journal_cache_transaction t
                  WHERE t.qbo_txn_id = split_part(m.ref, ':', 3)
                    AND ((split_part(m.ref, ':', 2) = 'Payment'
                          AND t.txn_type = 'Payment')
                         OR (split_part(m.ref, ':', 2) = 'BillPayment'
                             AND t.txn_type IN ('Bill Payment (Cheque)',
                                                'Bill Payment (Credit Card)')))
              )
            GROUP BY m.ref
            """,
            {"exch_ids": exch_ids},
        )
        for ref, fx in env.cr.fetchall():
            _logger.warning(
                "FX true-up: stamped exchange FX %.2f on %s has no "
                "JournalReport txn — left un-trued", fx, ref,
            )

        if not rows:
            _logger.info("FX true-up: no per-txn FX gaps found")
            return

        move_vals = []
        net = 0.0
        skipped_no_ctrl = 0
        for kind, qid, dt, ctrl_code, gap in rows:
            gap = float(gap)
            ctrl = arap_by_code.get(ctrl_code)
            if not ctrl:
                # QBO's own GL for this txn has no AR/AP line (e.g. an FX
                # adjustment payment booked against undeposited funds), but
                # Odoo's exchange moves for it DID rebalance a control
                # account — that account is where the gap sits, so true up
                # against it.
                env.cr.execute(
                    """
                    SELECT aml.account_id
                    FROM account_move m
                    JOIN account_move_line aml ON aml.move_id = m.id
                    JOIN account_account aa ON aa.id = aml.account_id
                    WHERE m.ref = %s AND m.state = 'posted'
                      AND aa.account_type IN
                          ('asset_receivable', 'liability_payable')
                    GROUP BY aml.account_id
                    ORDER BY sum(abs(aml.balance)) DESC
                    LIMIT 1
                    """,
                    [f"QBO_EXCH:{kind}:{qid}"],
                )
                row = env.cr.fetchone()
                if row:
                    ctrl = env["account.account"].browse(row[0])
            if not ctrl:
                skipped_no_ctrl += 1
                _logger.warning(
                    "FX true-up: %s %s gap %.2f has no AR/AP control "
                    "account in the cache — left un-trued", kind, qid, gap,
                )
                continue
            net += gap
            # gap > 0: Odoo under-booked a loss -> Dr exchange-loss, Cr AR/AP.
            # gap < 0: Odoo under-booked a gain -> Dr AR/AP, Cr exchange-gain.
            if gap > 0:
                exch_line = {"account_id": loss_acct.id, "debit": gap, "credit": 0.0}
                ctrl_line = {"account_id": ctrl.id, "debit": 0.0, "credit": gap}
            else:
                exch_line = {"account_id": gain_acct.id, "debit": 0.0, "credit": -gap}
                ctrl_line = {"account_id": ctrl.id, "debit": -gap, "credit": 0.0}
            # AR/AP leg: pure home-currency FX adjustment, no partner so the
            # aged receivable/payable is untouched — only the GL balance moves.
            # On a foreign-currency control account the line must carry the
            # account currency with 0 foreign amount (realized-FX shape); on a
            # home-currency account amount_currency must equal the balance, so
            # leave the ORM to fill it.
            ctrl_line["name"] = f"QBO FX true-up {kind} {qid}"
            ctrl_cur = ctrl.currency_id
            if ctrl_cur and ctrl_cur != company.currency_id:
                ctrl_line.update({"currency_id": ctrl_cur.id, "amount_currency": 0.0})
            exch_line["name"] = f"QBO FX true-up {kind} {qid}"
            move_vals.append({
                "move_type": "entry",
                "date": dt,
                "journal_id": exch_journal.id,
                # Per-txn ref so downstream per-transaction attribution
                # (the cent-parity pass) can fold this back onto its txn.
                "ref": f"QBO_FX_TRUEUP:{kind}:{qid}",
                "line_ids": [(0, 0, exch_line), (0, 0, ctrl_line)],
            })

        moves = env["account.move"].create(move_vals)
        moves.action_post()
        _logger.info(
            "FX true-up: booked %d per-txn adjustments, net %.2f to exchange "
            "account (recovered from AR/AP control; %d skipped without "
            "control account)",
            len(moves), net, skipped_no_ctrl,
        )

    @staticmethod
    def _book_gl_cent_trueup(ctx: ETLContext):
        """Book per-transaction rounding-parity entries against the source GL.

        After every structural correction, what remains between Odoo and the
        JournalReport cache is per-document rounding: the two systems round
        line amounts differently (per-line vs per-total), leaving cent-scale
        gaps scattered across accounts.  Both ledgers are balanced, so each
        transaction's per-account gaps sum to zero — which makes the exact
        remainder bookable as ONE balanced parity entry per transaction,
        dated at the transaction, with one leg per account.

        Attribution folds every Odoo move onto its QBO transaction: entity
        qbo-id columns, account.payment ids, stamped exchange moves
        (``QBO_EXCH:<Kind>:<id>``), per-txn FX true-ups
        (``QBO_FX_TRUEUP:<Kind>:<id>``), and journal-fallback moves
        (``JNL-<type>-<id>``).  Heuristic-retry exchange moves and their
        reversals cancel pairwise and are excluded.  QBO transaction ids are
        globally unique across types, so keys never collide.

        Safety guards (each skip is logged):
        - any per-account gap over 1.00 → not rounding, skip the txn;
        - a txn whose gaps don't sum to zero → incomplete attribution, skip.

        Runs once, last.  Idempotent via the ``QBO_CENT_TRUEUP`` ref.
        """
        env = ctx.env
        company = env.company
        journal = company.currency_exchange_journal_id
        if not journal:
            _logger.warning(
                "Cent parity skipped: company.currency_exchange_journal_id "
                "not configured"
            )
            return
        if env["account.move"].search_count([("ref", "=", "QBO_CENT_TRUEUP")]):
            _logger.info("Cent parity already booked — skipping")
            return

        env.cr.execute(
            """
            WITH codes AS (
                SELECT id, code_store::jsonb ->> %(cid)s AS code
                FROM account_account
                WHERE code_store::jsonb ->> %(cid)s IS NOT NULL
            ),
            odoo_side AS (
                SELECT c.code,
                       CASE
                         WHEN m.ref IN ('QBO_EXCH:RETRY',
                                        'QBO_FX_RETRY_REVERSAL')
                           THEN NULL  -- cancels pairwise, no txn identity
                         WHEN m.ref LIKE 'QBO\\_EXCH:%%'
                           THEN split_part(m.ref, ':', 3)
                         WHEN m.ref LIKE 'QBO\\_FX\\_TRUEUP:%%'
                           THEN split_part(m.ref, ':', 3)
                         WHEN m.ref LIKE 'JNL-%%'
                           THEN regexp_replace(m.ref, '^JNL-.*-', '')
                         ELSE COALESCE(
                             m.qbo_invoice_id::text, m.qbo_bill_id::text,
                             m.qbo_credit_memo_id::text,
                             m.qbo_vendor_credit_id::text,
                             m.qbo_journal_entry_id::text,
                             m.qbo_deposit_id::text, m.qbo_expense_id::text,
                             m.qbo_transfer_id::text,
                             m.qbo_sales_receipt_id::text,
                             m.qbo_refund_receipt_id::text,
                             m.qbo_tax_payment_id::text,
                             m.qbo_cc_payment_id::text,
                             p.qbo_payment_id::text,
                             p.qbo_bill_payment_id::text)
                       END AS key,
                       min(m.date) AS dt,
                       sum(aml.balance) AS v
                FROM account_move m
                LEFT JOIN account_payment p ON p.move_id = m.id
                JOIN account_move_line aml ON aml.move_id = m.id
                JOIN codes c ON c.id = aml.account_id
                WHERE m.state = 'posted'
                GROUP BY 1, 2
            ),
            qbo_side AS (
                SELECT l.account_code AS code, t.qbo_txn_id AS key,
                       min(t.txn_date) AS dt,
                       sum(l.debit - l.credit) AS v
                FROM qbo_journal_cache_transaction t
                JOIN qbo_journal_cache_line l ON l.transaction_id = t.id
                WHERE l.account_code IS NOT NULL
                GROUP BY 1, 2
            )
            SELECT COALESCE(q.key, o.key) AS key,
                   COALESCE(q.code, o.code) AS code,
                   COALESCE(q.dt, o.dt) AS dt,
                   round((COALESCE(q.v, 0) - COALESCE(o.v, 0))::numeric, 2)
                       AS gap
            FROM qbo_side q
            FULL OUTER JOIN odoo_side o
              ON o.code = q.code AND o.key = q.key
            WHERE COALESCE(q.key, o.key) IS NOT NULL
              AND abs(COALESCE(q.v, 0) - COALESCE(o.v, 0)) >= 0.005
            ORDER BY 3, 1, 2
            """,
            {"cid": str(company.id)},
        )
        rows = env.cr.fetchall()
        if not rows:
            _logger.info("Cent parity: ledgers already tie to the penny")
            return

        acct_by_code = {
            a.code: a
            for a in env["account.account"].search([])
            if a.code
        }
        by_txn = {}
        for key, code, dt, gap in rows:
            by_txn.setdefault(key, {"dt": dt, "gaps": []})
            by_txn[key]["gaps"].append((code, float(gap)))
            if dt and (by_txn[key]["dt"] is None or dt < by_txn[key]["dt"]):
                by_txn[key]["dt"] = dt

        move_vals = []
        skipped = 0
        gross = 0.0
        for key, data in by_txn.items():
            gaps = data["gaps"]
            if any(abs(g) > 1.0 for _, g in gaps):
                skipped += 1
                _logger.warning(
                    "Cent parity: txn %s has a gap over 1.00 (%s) — not "
                    "rounding, left un-trued",
                    key, ", ".join(f"{c}:{g:+.2f}" for c, g in gaps),
                )
                continue
            if abs(round(sum(g for _, g in gaps), 2)) >= 0.005:
                skipped += 1
                _logger.warning(
                    "Cent parity: txn %s gaps don't balance (%s) — "
                    "incomplete attribution, left un-trued",
                    key, ", ".join(f"{c}:{g:+.2f}" for c, g in gaps),
                )
                continue
            lines = []
            for code, gap in gaps:
                acct = acct_by_code.get(code)
                if not acct:
                    lines = []
                    break
                lv = {
                    "account_id": acct.id,
                    "name": f"QBO rounding parity {key}",
                    "debit": gap if gap > 0 else 0.0,
                    "credit": -gap if gap < 0 else 0.0,
                }
                cur = acct.currency_id
                if cur and cur != company.currency_id:
                    lv.update({"currency_id": cur.id, "amount_currency": 0.0})
                lines.append((0, 0, lv))
            if not lines:
                skipped += 1
                _logger.warning(
                    "Cent parity: txn %s references an unmapped account "
                    "code — left un-trued", key,
                )
                continue
            gross += sum(abs(g) for _, g in gaps)
            move_vals.append({
                "move_type": "entry",
                "date": data["dt"],
                "journal_id": journal.id,
                "ref": "QBO_CENT_TRUEUP",
                "line_ids": lines,
            })

        if not move_vals:
            _logger.info(
                "Cent parity: nothing bookable (%d skipped)", skipped,
            )
            return
        moves = env["account.move"].create(move_vals)
        moves.action_post()
        _logger.info(
            "Cent parity: booked %d per-txn rounding entries (%.2f gross "
            "absolute; %d skipped)",
            len(moves), gross, skipped,
        )
