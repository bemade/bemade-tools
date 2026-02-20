"""QuickBooks Online Journal Entry ETL Pipeline

This module handles the migration of Journal Entries from QBO to Odoo
using the ETL framework.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, RETRYABLE_ERRORS

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.journal.entry.importer",
    sap_source="JournalEntry",
    depends_on=["qbo.account.importer"],
)
class QboJournalEntryImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Journal Entries."""

    _name = "qbo.journal.entry.importer"
    _description = "QBO Journal Entry Importer"

    @ETL.extract("JournalEntry")
    def extract_journal_entries(self, ctx: ETLContext) -> List[Dict]:
        """Extract journal entries from QBO API."""
        api_client = ctx.get_config("api_client")
        if not api_client:
            raise ValueError("API client not found in ETL context")

        # Get existing QBO journal entry IDs
        ctx.env.cr.execute(
            "SELECT qbo_journal_entry_id FROM account_move "
            "WHERE qbo_journal_entry_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
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
        return new_entries

    @ETL.transform()
    def transform_journal_entries(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO journal entries into Odoo account.move values."""
        entries = extracted.get("extract_journal_entries", [])

        # Build account lookup
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company
        journal = ctx.env["account.journal"].search(
            [
                ("type", "=", "general"),
                ("company_id", "=", company.id),
            ],
            limit=1,
        )

        if not journal:
            # Create a general journal if none exists
            journal = ctx.env["account.journal"].create(
                {
                    "name": "General Journal",
                    "code": "GEN",
                    "type": "general",
                    "company_id": company.id,
                }
            )
            _logger.info(
                f"Created general journal {journal.name} for QBO journal entries"
            )

        move_vals = []
        skipped = 0

        # Exchange rates are now synced by qbo.exchange.rate.importer pipeline
        for entry in entries:
            # Parse date
            txn_date = entry.get("TxnDate")
            if txn_date:
                try:
                    date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                except ValueError:
                    date = datetime.now().date()
            else:
                date = datetime.now().date()

            # Get currency info for the move
            currency_ref = entry.get("CurrencyRef", {})
            currency_code = currency_ref.get("value") if currency_ref else None
            exchange_rate = float(entry.get("ExchangeRate", 1.0) or 1.0)

            # Determine if this is a foreign currency entry
            is_foreign_currency = False
            currency = None
            if currency_code and currency_code != company.currency_id.name:
                currency = ctx.env["res.currency"].search(
                    [("name", "=", currency_code)], limit=1
                )
                if currency:
                    is_foreign_currency = True

            # Build line items
            lines = entry.get("Line", [])
            line_vals = []
            has_error = False

            for line in lines:
                detail = line.get("JournalEntryLineDetail", {})
                if not detail:
                    continue

                account_ref = detail.get("AccountRef", {})
                if not account_ref:
                    continue

                # Skip zero-amount lines
                amount_foreign = float(line.get("Amount", 0) or 0)
                if amount_foreign == 0:
                    continue

                qbo_account_id = int(account_ref.get("value", 0))
                account_id = account_map.get(qbo_account_id)

                if not account_id:
                    _logger.warning(
                        f"Account not found for QBO ID {qbo_account_id} "
                        f"in journal entry {entry.get('Id')}"
                    )
                    has_error = True
                    break

                posting_type = detail.get("PostingType", "")

                # Convert to company currency if foreign currency
                # QBO ExchangeRate = home currency per 1 foreign unit
                if is_foreign_currency and exchange_rate:
                    amount_company = amount_foreign * exchange_rate
                else:
                    amount_company = amount_foreign

                if posting_type == "Debit":
                    debit = amount_company
                    credit = 0.0
                    amount_currency = amount_foreign if is_foreign_currency else 0
                elif posting_type == "Credit":
                    debit = 0.0
                    credit = amount_company
                    amount_currency = -amount_foreign if is_foreign_currency else 0
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

                # Add currency fields for foreign currency entries
                if is_foreign_currency and currency:
                    line_data["currency_id"] = currency.id
                    line_data["amount_currency"] = amount_currency

                line_vals.append((0, 0, line_data))

            if has_error or not line_vals:
                skipped += 1
                continue

            # Fix rounding differences to ensure entry balances
            total_debit = sum(l[2]["debit"] for l in line_vals)
            total_credit = sum(l[2]["credit"] for l in line_vals)
            diff = round(total_debit - total_credit, 2)

            if diff != 0:
                adjusted = False
                # Try to adjust a credit line first (if debit > credit)
                if diff > 0:
                    for i in range(len(line_vals) - 1, -1, -1):
                        if line_vals[i][2]["credit"] > 0:
                            line_vals[i][2]["credit"] = round(
                                line_vals[i][2]["credit"] + diff, 2
                            )
                            adjusted = True
                            break
                # Try to adjust a debit line (if credit > debit)
                else:
                    for i in range(len(line_vals) - 1, -1, -1):
                        if line_vals[i][2]["debit"] > 0:
                            line_vals[i][2]["debit"] = round(
                                line_vals[i][2]["debit"] - diff, 2
                            )
                            adjusted = True
                            break

                if adjusted:
                    _logger.debug(f"Adjusted by {diff} to balance JE {entry.get('Id')}")
                else:
                    _logger.warning(
                        f"Could not balance JE {entry.get('Id')}: "
                        f"debit={total_debit}, credit={total_credit}"
                    )
                    skipped += 1
                    continue

            # Build move values
            move_val = {
                "move_type": "entry",
                "journal_id": journal.id,
                "date": date,
                "ref": f"QBO-{entry.get('Id')}",
                "narration": entry.get("PrivateNote", ""),
                "line_ids": line_vals,
                "qbo_journal_entry_id": int(entry.get("Id")),
            }

            # Set currency if foreign currency transaction
            if currency_code:
                currency = ctx.env["res.currency"].search(
                    [("name", "=", currency_code)], limit=1
                )
                if currency:
                    move_val["currency_id"] = currency.id

            move_vals.append(move_val)

        _logger.info(f"Transformed {len(move_vals)} journal entries, skipped {skipped}")
        return move_vals

    @ETL.load()
    def load_journal_entries(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load journal entries into Odoo."""
        move_vals = transformed.get("transform_journal_entries", [])

        if not move_vals:
            _logger.info("No new journal entries to create")
            return

        # Create moves one by one to handle potential errors
        created = 0
        errors = 0
        for vals in move_vals:
            try:
                move = ctx.env["account.move"].create(vals)
                created += 1
                # Post the move
                try:
                    move.action_post()
                except RETRYABLE_ERRORS:
                    raise
                except Exception as e:
                    _logger.warning(
                        f"Could not post journal entry {vals.get('ref')}: {e}"
                    )
            except Exception as e:
                errors += 1
                # Log details for debugging
                total_debit = sum(
                    l[2].get("debit", 0) for l in vals.get("line_ids", [])
                )
                total_credit = sum(
                    l[2].get("credit", 0) for l in vals.get("line_ids", [])
                )
                _logger.error(
                    f"Failed to create journal entry {vals.get('ref')}: {e}. "
                    f"Debit={total_debit}, Credit={total_credit}, Diff={total_debit - total_credit}"
                )

        _logger.info(f"Created {created} journal entries")

        # Update last sync timestamp
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection.last_journal_entry_sync = ctx.env.cr.now()
