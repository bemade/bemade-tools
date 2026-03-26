"""QuickBooks Online Journal Entry ETL Pipeline

This module handles the migration of Journal Entries from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData, post_lock

from .extractor import QBOExtractor
from .move_builder import QBOMoveBuilder
from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.journal.entry.importer",
    sap_source="JournalEntry",
    depends_on=["qbo.account.importer", "qbo.tax.importer"],
)
class QboJournalEntryImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Journal Entries."""

    _name = "qbo.journal.entry.importer"
    _description = "QBO Journal Entry Importer"

    @ETL.extract("JournalEntry")
    def extract_journal_entries(self, ctx: ETLContext) -> ChunkableData:
        """Extract journal entries from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO journal entry IDs
        existing_ids = extractor.existing_qbo_ids(
            "account_move", "qbo_journal_entry_id"
        )
        _logger.info(f"Found {len(existing_ids)} existing journal entries in Odoo")

        # Fetch all journal entries from QBO
        entries = api_client.query_all(entity="JournalEntry", order_by="Id")

        # Filter out already imported
        new_entries = [
            entry for entry in entries if str(entry.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(entries)} journal entries from QBO, "
            f"{len(new_entries)} are new"
        )

        # Preload maps for transform
        extractor.preload("account", "account_currency", "currency")
        extractor.preload_journals("general")

        # Tax rate ref → tax account ID for JEs with TxnTaxDetail
        extractor.preload_tax_rate_account_map(use_suspense=True)

        return ChunkableData(
            records=new_entries,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_journal_entries(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO journal entries into Odoo account.move values."""
        data = extracted.get("extract_journal_entries")
        if not data:
            return []
        entries = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])

        move_vals = []
        skipped = 0

        for entry in entries:
            vals = builder.build_entry_move_vals(
                entry,
                journal_type="general",
                qbo_id_field="qbo_journal_entry_id",
                line_builder_fn=lambda e, cur, rate, foreign: (
                    self._build_je_lines(builder, e, cur, rate, foreign)
                ),
                extra_vals={"narration": entry.get("PrivateNote", "")},
            )
            if vals:
                # Set currency_id from CurrencyRef if present
                currency_code = entry.get("CurrencyRef", {}).get("value")
                if currency_code:
                    currency_id = builder.currency_map.get(currency_code)
                    if currency_id:
                        vals["currency_id"] = currency_id
                move_vals.append(vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals)} journal entries, skipped {skipped}")
        return move_vals

    @staticmethod
    def _build_je_lines(
        builder: QBOMoveBuilder,
        entry: Dict,
        currency_id: int,
        exchange_rate: float,
        is_foreign: bool,
    ) -> Optional[List[tuple]]:
        """Build journal entry lines with secondary currency support."""
        lines = entry.get("Line", [])
        line_vals = []

        for line in lines:
            detail = line.get("JournalEntryLineDetail", {})
            if not detail:
                continue

            amount_foreign = float(line.get("Amount", 0) or 0)
            if amount_foreign == 0:
                continue

            account_id, account_currency_id = builder.resolve_account_with_currency(
                detail
            )
            if not account_id:
                _logger.warning(
                    f"Account not found for QBO ID "
                    f"{detail.get('AccountRef', {}).get('value')} "
                    f"in journal entry {entry.get('Id')}"
                )
                return None  # Abort entire entry

            posting_type = detail.get("PostingType", "")
            amount_company = builder.convert_to_company_currency(
                amount_foreign, exchange_rate, is_foreign
            )

            if posting_type == "Debit":
                debit = round(amount_company, 2)
                credit = 0.0
                amount_currency = amount_foreign if is_foreign else 0
            elif posting_type == "Credit":
                debit = 0.0
                credit = round(amount_company, 2)
                amount_currency = -amount_foreign if is_foreign else 0
            else:
                continue

            line_data = {
                "account_id": account_id,
                "name": line.get("Description", "")
                or entry.get("PrivateNote", "")
                or "/",
                "debit": debit,
                "credit": credit,
            }

            if is_foreign:
                line_data["currency_id"] = currency_id
                line_data["amount_currency"] = amount_currency
            elif account_currency_id:
                line_data["currency_id"] = account_currency_id
                if posting_type == "Debit":
                    line_data["amount_currency"] = debit
                else:
                    line_data["amount_currency"] = -credit

            line_vals.append((0, 0, line_data))

        # Add tax lines from TxnTaxDetail (not included in Line entries)
        tax_line_tuples, _total_tax = builder.build_tax_lines_from_detail(
            entry, currency_id, exchange_rate, is_foreign,
        )
        line_vals.extend(tax_line_tuples)

        return line_vals or None

    @ETL.load()
    def load_journal_entries(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load journal entries into Odoo."""
        move_vals = transformed.get("transform_journal_entries", [])

        if not move_vals:
            _logger.info("No new journal entries to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals:
            with ctx.skippable(
                f"create journal entry QBO#{vals.get('qbo_journal_entry_id', '?')}"
            ):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} journal entries")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(
                        f"post journal entry QBO#{move.qbo_journal_entry_id or '?'}"
                    ):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} journal entries")
