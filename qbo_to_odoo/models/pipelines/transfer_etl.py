"""QuickBooks Online Transfer ETL Pipeline

This module handles the migration of Transfers from QBO to Odoo
as journal entries using the ETL framework.

QBO Transfers represent bank-to-bank transfers and are imported as
journal entries with a debit to the destination account and credit
to the source account.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.transfer.importer",
    sap_source="Transfer",
    depends_on=["qbo.exchange.rate.importer", "qbo.account.importer"],
)
class QboTransferImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Transfers as journal entries."""

    _name = "qbo.transfer.importer"
    _description = "QBO Transfer Importer"

    @ETL.extract("Transfer")
    def extract_transfers(self, ctx: ETLContext) -> List[Dict]:
        """Extract transfers from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO transfer IDs
        try:
            ctx.env.cr.execute(
                "SELECT qbo_transfer_id FROM account_move WHERE qbo_transfer_id IS NOT NULL"
            )
            existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        except Exception:
            ctx.env.cr.rollback()
            existing_ids = set()
            _logger.warning(
                "qbo_transfer_id column not found - module upgrade required"
            )

        _logger.info(f"Found {len(existing_ids)} existing transfers in Odoo")

        # Fetch all transfers from QBO
        all_transfers = api_client.query_all(entity="Transfer", order_by="Id")

        # Filter out already imported
        new_transfers = [
            t for t in all_transfers if str(t.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(all_transfers)} transfers from QBO, "
            f"{len(new_transfers)} are new"
        )
        return new_transfers

    @ETL.transform()
    def transform_transfers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO transfers into Odoo account.move journal entry values."""
        transfers = extracted.get("extract_transfers", [])

        # Build account lookup by QBO ID
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        # Get general journal for transfers
        journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not journal:
            raise ValueError("No general journal found for transfer entries")

        move_vals_list = []
        skipped = 0

        for transfer in transfers:
            move_vals = self._transform_transfer(
                transfer, account_map, journal, company
            )
            if move_vals:
                move_vals_list.append(move_vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals_list)} transfers, skipped {skipped}")
        return move_vals_list

    def _transform_transfer(
        self,
        transfer: Dict,
        account_map: Dict,
        journal,
        company,
    ) -> Optional[Dict]:
        """Transform a single QBO Transfer into account.move values."""
        qbo_id = transfer.get("Id")
        txn_date = transfer.get("TxnDate")
        amount = float(transfer.get("Amount", 0) or 0)

        if amount <= 0:
            _logger.warning(f"Transfer {qbo_id} has no amount, skipping")
            return None

        # Get source account (FromAccountRef)
        from_ref = transfer.get("FromAccountRef", {})
        from_qbo_id = from_ref.get("value")
        from_account_id = account_map.get(str(from_qbo_id))
        if not from_account_id:
            _logger.warning(
                f"From account not found for QBO ID {from_qbo_id} in transfer {qbo_id}"
            )
            return None

        # Get destination account (ToAccountRef)
        to_ref = transfer.get("ToAccountRef", {})
        to_qbo_id = to_ref.get("value")
        to_account_id = account_map.get(str(to_qbo_id))
        if not to_account_id:
            _logger.warning(
                f"To account not found for QBO ID {to_qbo_id} in transfer {qbo_id}"
            )
            return None

        # Get currency and exchange rate
        currency_code = transfer.get("CurrencyRef", {}).get("value", "CAD")
        exchange_rate = float(transfer.get("ExchangeRate", 1.0) or 1.0)
        currency = company.env["res.currency"].search(
            [("name", "=", currency_code)], limit=1
        )
        if not currency:
            currency = company.currency_id

        # Determine if foreign currency
        is_foreign_currency = currency.id != company.currency_id.id

        # Convert to company currency if needed
        if is_foreign_currency and exchange_rate:
            amount_company = amount * exchange_rate
        else:
            amount_company = amount

        # Build journal entry lines
        from_line_vals = {
            "account_id": from_account_id,
            "name": f"Transfer to {to_ref.get('name', 'account')}",
            "credit": amount_company,
            "debit": 0,
        }
        to_line_vals = {
            "account_id": to_account_id,
            "name": f"Transfer from {from_ref.get('name', 'account')}",
            "debit": amount_company,
            "credit": 0,
        }

        # Add currency fields for foreign currency transfers
        if is_foreign_currency:
            from_line_vals["currency_id"] = currency.id
            from_line_vals["amount_currency"] = -amount  # Credit = negative
            to_line_vals["currency_id"] = currency.id
            to_line_vals["amount_currency"] = amount  # Debit = positive

        line_ids = [
            (0, 0, from_line_vals),
            (0, 0, to_line_vals),
        ]

        return {
            "move_type": "entry",
            "journal_id": journal.id,
            "date": txn_date,
            "ref": f"Transfer QBO-{qbo_id}",
            "qbo_transfer_id": qbo_id,
            "company_id": company.id,
            "currency_id": currency.id,
            "line_ids": line_ids,
        }

    @ETL.load()
    def load_transfers(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load transfers as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_transfers", [])

        if not move_vals_list:
            _logger.info("No new transfers to create")
            return

        created = 0
        posted = 0
        errors = 0

        for vals in move_vals_list:
            move = ctx.env["account.move"].create(vals)
            created += 1
            move.action_post()
            posted += 1
        _logger.info(f"Created {created} transfers ({posted} posted, {errors} errors)")
