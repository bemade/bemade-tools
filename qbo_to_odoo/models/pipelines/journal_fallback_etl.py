"""QBO Journal Fallback Pipeline.

Imports transactions the API entity pipelines cannot build:

- **Payroll Cheque** — payroll transactions (no API endpoint)
- **Inventory Starting Value** — opening inventory balances (no API endpoint)
- **Tax Payment / Sales Tax Payment / Sales Tax Adjustment** (no API endpoint)
- **Journal Entry** — only the home-currency-adjustment JEs the API pipeline
  drops (all foreign line amounts are zero)
- **Payment / Bill Payment** — only payments the API pipeline skipped
  (e.g. deleted partner); pure zero-total credit applications are excluded

These are imported as generic journal entries (``move_type='entry'``)
from the cached QBO JournalReport (``qbo.journal.cache``), booking the
report's home-currency amounts as-is — GL truth by construction, so no
FX or tax recomputation is involved (a line on a foreign-currency account
gets its foreign amount at par, which is cosmetic: the TB is home-currency).

Runs after all entity pipelines so ``get_imported_qbo_ids()`` correctly
excludes transactions already imported via the API.
"""

import logging
from collections import defaultdict
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData, post_lock

from .extractor import QBOExtractor
from .gl_helpers import (
    build_code_maps,
    build_cogs_completion_transactions,
    get_imported_qbo_ids,
    journal_entry_vals_from_export,
)

_logger = logging.getLogger(__name__)

# Only these QBO transaction types are imported from the JournalReport.
# Everything else comes from the API entity pipelines.
_ALLOWED_TYPES = frozenset({
    "Payroll Cheque",   # XLSX export name
    "Paycheque",        # API JournalReport name
    "Inventory Starting Value",
    "Tax Payment",
    "Sales Tax Payment",
    "Sales Tax Adjustment",
    # QBO home-currency-adjustment JEs (foreign Line.Amount == 0, so the
    # JournalEntry API pipeline can't build them and drops them). They exist
    # only in the JournalReport with real home-currency amounts (typically
    # DR exchange G/L / CR a foreign bank). get_imported_qbo_ids already
    # excludes the JEs the API pipeline DID import, so this only picks up
    # the dropped ones.
    "Journal Entry",
    # Payments the API pipeline skipped (e.g. deleted partner -> no
    # receivable/payable account resolves).  get_imported_qbo_ids excludes
    # every account.payment the API pipeline created, and pure zero-total
    # credit applications (AR/AP + exchange legs only, no cash) are filtered
    # out in extract — those are reconciliations, not missing moves, and
    # their FX is handled by the FX true-up finalizer.
    "Payment",
    "Bill Payment (Cheque)",
    "Bill Payment (Credit Card)",
})

_PAYMENT_TYPES = frozenset({
    "Payment",
    "Bill Payment (Cheque)",
    "Bill Payment (Credit Card)",
})


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.journal.fallback",
    sap_source="GLExport",
    depends_on=[
        "qbo.account.importer",
        "qbo.invoice.importer",
        "qbo.bill.importer",
        "qbo.credit.memo.importer",
        "qbo.vendor.credit.importer",
        "qbo.payment.importer",
        "qbo.journal.entry.importer",
        "qbo.transfer.importer",
        "qbo.deposit.importer",
        "qbo.expense.importer",
        "qbo.sales.receipt.importer",
        "qbo.refund.receipt.importer",
        "qbo.cc.payment.importer",
    ],
    chunk_size=200,
)
class QboJournalFallbackImporter(models.AbstractModel):
    """Imports non-API QBO transaction types from the JournalReport cache."""

    _name = "qbo.journal.fallback"
    _description = "QBO Journal Fallback Importer"

    @ETL.extract("GLExport")
    def extract_fallback_transactions(self, ctx: ETLContext) -> ChunkableData:
        """Read cached JournalReport, filter to allowed types, exclude imported."""
        extractor = QBOExtractor(ctx)

        connection = ctx.env["qbo.connection"].browse(
            ctx.get_config("source_id")
        )
        cache = connection._ensure_journal_cache()

        # Collect IDs already imported by entity pipelines
        imported_ids = get_imported_qbo_ids(ctx)

        # Query the cache for allowed types, excluding imported
        transactions = cache.get_transactions_for_import(
            _ALLOWED_TYPES, imported_ids
        )
        _logger.info(
            "Journal cache returned %d fallback transactions "
            "(after excluding %d imported IDs)",
            len(transactions),
            len(imported_ids),
        )

        # Build code maps for account resolution (also used by the
        # pure-application filter below).
        maps = build_code_maps(ctx)

        # Drop pure zero-total credit/debit-note applications: payment-type
        # txns whose every line sits on AR/AP or the exchange G/L accounts.
        # Those are reconciliations (handled in payment_etl + the FX true-up
        # finalizer), not missing moves — importing them would double-book
        # their FX and dump un-reconcilable noise on the control accounts.
        company = ctx.env.company
        exch_ids = (
            company.income_currency_exchange_account_id
            | company.expense_currency_exchange_account_id
        ).ids
        arap_types = ("asset_receivable", "liability_payable")

        def _is_pure_application(txn) -> bool:
            if not txn["lines"] or txn["lines"][0].get("type") not in _PAYMENT_TYPES:
                return False
            for line in txn["lines"]:
                acct_id = maps["code_map"].get(line.get("account_code"))
                if acct_id is None:
                    return False  # unmapped leg — not provably an application
                if (
                    maps["account_type_map"].get(acct_id) not in arap_types
                    and acct_id not in exch_ids
                ):
                    return False  # has a cash/other leg — a real payment
            return True

        pure_apps = [t for t in transactions if _is_pure_application(t)]
        if pure_apps:
            transactions = [t for t in transactions if not _is_pure_application(t)]
            _logger.info(
                "Fallback: excluded %d pure credit-application payment txns "
                "(reconciliations, not missing moves)", len(pure_apps),
            )

        # Log type breakdown
        type_counts: Dict[str, int] = defaultdict(int)
        for txn in transactions:
            if txn.get("lines"):
                type_counts[txn["lines"][0]["type"]] += 1
        if type_counts:
            _logger.info(
                "Fallback types: %s",
                ", ".join(f"{t}: {c}" for t, c in sorted(type_counts.items())),
            )

        # Log unimported types from the full cache for drift investigation
        all_cache_txns = cache.transaction_ids
        skipped_types: Dict[str, int] = defaultdict(int)
        skipped_details: List[str] = []
        for txn in all_cache_txns:
            if (
                txn.txn_type not in _ALLOWED_TYPES
                and (txn.qbo_txn_id or "") not in imported_ids
            ):
                skipped_types[txn.txn_type or "Unknown"] += 1
                total_d = sum(l.debit for l in txn.line_ids)
                total_c = sum(l.credit for l in txn.line_ids)
                accts = sorted({l.account_code or "?" for l in txn.line_ids})
                skipped_details.append(
                    f"  QBO#{txn.qbo_txn_id or '?'} {txn.txn_type} "
                    f"D={total_d:,.2f} C={total_c:,.2f} "
                    f"accts=[{', '.join(accts)}]"
                )
        if skipped_types:
            _logger.warning(
                "Unimported cache types (not API, not fallback): %s",
                ", ".join(f"{t}: {c}" for t, c in sorted(skipped_types.items())),
            )
            _logger.warning(
                "Unimported cache transaction details:\n%s",
                "\n".join(skipped_details),
            )

        # Complete entity-imported transactions (invoices, sales receipts)
        # with the perpetual-COGS lines QBO booked but no pipeline recovers:
        # DR Cost of Goods Sold / CR inventory valuation.  Appended to the
        # whole-transaction fallback list so the same transform/load builds
        # balancing JEs from them.
        cogs_txns = build_cogs_completion_transactions(
            ctx,
            cache.id,
            imported_ids,
            maps["code_map"],
            maps["account_type_map"],
        )
        if cogs_txns:
            _logger.info(
                "COGS completion: recovering dropped COGS/inventory lines "
                "for %d entity-imported transactions",
                len(cogs_txns),
            )
            transactions = transactions + cogs_txns

        # Get the misc journal for generic entries
        extractor.preload_journals("general")

        return ChunkableData(
            records=transactions,
            context={
                "code_map": maps["code_map"],
                "account_currency_map": maps["account_currency_map"],
                "company_currency_id": maps["company_currency_id"],
                "journal_id": extractor._journal_ids.get("general"),
                "company_id": extractor._company_id,
            },
        )

    @ETL.transform()
    def transform_fallback(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Build journal entry vals from cached JournalReport lines."""
        data = extracted.get("extract_fallback_transactions")
        if not data:
            return []
        transactions = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        code_map = context.get("code_map", {})
        account_currency_map = context.get("account_currency_map", {})
        company_currency_id = context.get("company_currency_id")
        journal_id = context.get("journal_id")
        company_id = context.get("company_id")

        if not journal_id:
            _logger.error("No general journal found — cannot create fallback JEs")
            return []

        move_vals_list = []
        skipped = 0

        for txn in transactions:
            txn_id = str(txn["id"])
            lines = txn["lines"]
            if not lines:
                continue

            txn_type = lines[0].get("type", "Unknown")
            txn_date = lines[0].get("date", "")
            txn_num = lines[0].get("num", "")
            txn_name = lines[0].get("name", "")

            vals = journal_entry_vals_from_export(
                txn_id=txn_id,
                txn_type=txn_type,
                txn_date=txn_date,
                txn_num=txn_num,
                txn_name=txn_name,
                lines_data=lines,
                code_map=code_map,
                account_currency_map=account_currency_map,
                company_currency_id=company_currency_id,
                txn_currency_id=None,  # all CAD, no FX
                txn_exchange_rate=1.0,
                journal_id=journal_id,
                company_id=company_id,
            )
            if vals:
                move_vals_list.append(vals)
            else:
                skipped += 1

        _logger.info(
            "Transformed %d fallback JEs, skipped %d",
            len(move_vals_list), skipped,
        )
        return move_vals_list

    @ETL.load()
    def load_fallback(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create and post fallback journal entries."""
        move_vals_list = transformed.get("transform_fallback", [])
        if not move_vals_list:
            _logger.info("No fallback transactions to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals_list:
            ref = vals.get("ref", "?")
            with ctx.skippable(f"create fallback JE {ref}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info("Created %d fallback journal entries", len(moves))

        # Post by journal
        posted = 0
        by_journal: Dict[int, list] = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, ctx.env["account.move"])
            by_journal[move.journal_id.id] |= move

        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post fallback JE {move.ref or '?'}"):
                        move.action_post()
                        posted += 1

        _logger.info("Posted %d fallback journal entries", posted)
