"""QBO XLSX Fallback Pipeline.

Imports transaction types that have no QBO API endpoint:

- **Payroll Cheque** — payroll transactions (always CAD)
- **Inventory Starting Value** — opening inventory balances (always CAD)

These are imported as generic journal entries (``move_type='entry'``)
from the QBO Journal XLSX export file.  Since all fallback types are
CAD-only, no FX handling or tax corrections are needed — just a simple
create-then-post flow.

Runs after all entity pipelines so ``get_imported_qbo_ids()`` correctly
excludes transactions already imported via the API.
"""

import base64
import logging
from collections import defaultdict
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData, post_lock

from .extractor import QBOExtractor
from .gl_helpers import (
    build_code_maps,
    get_imported_qbo_ids,
    journal_entry_vals_from_export,
    parse_journal_export,
)

_logger = logging.getLogger(__name__)

# Only these QBO transaction types are imported from the XLSX.
# Everything else comes from the API entity pipelines.
#
# NOTE: "Payment" and "Bill Payment (Cheque)" are FX realization entries
# that QBO creates when payments settle at a different rate than the invoice.
# Odoo generates equivalent entries during reconciliation, so importing the
# QBO versions would double-count.  They appear as "unimported" in logs but
# the FX drift they represent is structural (QBO vs Odoo rate differences).
_ALLOWED_TYPES = frozenset({
    "Payroll Cheque",
    "Inventory Starting Value",
    "Tax Payment",
    "Sales Tax Payment",
    "Sales Tax Adjustment",
})


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.xlsx.fallback",
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
class QboXlsxFallbackImporter(models.AbstractModel):
    """Imports non-API QBO transaction types from the Journal XLSX export."""

    _name = "qbo.xlsx.fallback"
    _description = "QBO XLSX Fallback Importer"

    @ETL.extract("GLExport")
    def extract_fallback_transactions(self, ctx: ETLContext) -> ChunkableData:
        """Parse Journal XLSX, filter to allowed types, exclude imported."""
        extractor = QBOExtractor(ctx)

        connection = ctx.env["qbo.connection"].browse(
            ctx.get_config("source_id")
        )
        if not connection.gl_export_file:
            _logger.info(
                "No Journal export file uploaded — skipping XLSX fallback"
            )
            return ChunkableData(records=[], context={})

        file_content = base64.b64decode(connection.gl_export_file)
        transactions = parse_journal_export(file_content)
        _logger.info(
            "Parsed %d transactions from Journal export", len(transactions)
        )

        # Filter to allowed types only
        allowed = []
        for txn in transactions:
            if not txn.get("lines"):
                continue
            txn_type = txn["lines"][0].get("type", "")
            if txn_type in _ALLOWED_TYPES:
                allowed.append(txn)

        type_counts: Dict[str, int] = defaultdict(int)
        for txn in allowed:
            type_counts[txn["lines"][0]["type"]] += 1
        _logger.info(
            "Allowed fallback types: %d transactions (%s)",
            len(allowed),
            ", ".join(f"{t}: {c}" for t, c in sorted(type_counts.items())),
        )

        # Log transaction types NOT in our allowed list and NOT imported
        imported_ids = get_imported_qbo_ids(ctx)
        skipped_types: Dict[str, int] = defaultdict(int)
        skipped_details: List[str] = []
        for txn in transactions:
            if not txn.get("lines"):
                continue
            txn_type = txn["lines"][0].get("type", "")
            if txn_type not in _ALLOWED_TYPES and str(txn["id"]) not in imported_ids:
                skipped_types[txn_type] += 1
                # Collect account-level detail for drift investigation
                total_d = sum(l.get("debit", 0) for l in txn["lines"])
                total_c = sum(l.get("credit", 0) for l in txn["lines"])
                accts = sorted({
                    l.get("account_code", "?") for l in txn["lines"]
                })
                skipped_details.append(
                    f"  QBO#{txn['id']} {txn_type} "
                    f"D={total_d:,.2f} C={total_c:,.2f} "
                    f"accts=[{', '.join(accts)}]"
                )
        if skipped_types:
            _logger.warning(
                "Unimported XLSX types (not API, not fallback): %s",
                ", ".join(f"{t}: {c}" for t, c in sorted(skipped_types.items())),
            )
            _logger.warning(
                "Unimported XLSX transaction details:\n%s",
                "\n".join(skipped_details),
            )

        # Exclude transactions already imported by entity pipelines
        new_txns = [t for t in allowed if str(t["id"]) not in imported_ids]
        _logger.info(
            "After excluding %d imported IDs: %d new fallback transactions",
            len(imported_ids), len(new_txns),
        )

        # Build code maps for account resolution
        maps = build_code_maps(ctx)

        # Get the misc journal for generic entries
        extractor.preload_journals("general")

        return ChunkableData(
            records=new_txns,
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
        """Build journal entry vals from XLSX export lines."""
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
