"""QuickBooks Online Transfer ETL Pipeline

This module handles the migration of Transfers from QBO to Odoo
as journal entries using the ETL framework.

QBO Transfers represent bank-to-bank transfers and are imported as
journal entries with a debit to the destination account and credit
to the source account.
"""

import logging
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData, post_lock

from .exchange_rate_helper import ExchangeRateEnsurer
from .extractor import QBOExtractor
from .move_builder import QBOMoveBuilder
from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.transfer.importer",
    sap_source="Transfer",
    depends_on=["qbo.account.importer"],
)
class QboTransferImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Transfers as journal entries."""

    _name = "qbo.transfer.importer"
    _description = "QBO Transfer Importer"

    @ETL.extract("Transfer")
    def extract_transfers(self, ctx: ETLContext) -> ChunkableData:
        """Extract transfers from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO transfer IDs
        existing_ids = extractor.existing_qbo_ids("account_move", "qbo_transfer_id")
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

        # Ensure exchange rates exist for foreign-currency transfers
        ExchangeRateEnsurer(ctx.env).ensure_rates(new_transfers)

        # Preload maps for transform
        extractor.preload("account", "currency")
        extractor.preload_journals("general")

        return ChunkableData(
            records=new_transfers,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_transfers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO transfers into Odoo account.move journal entry values."""
        data = extracted.get("extract_transfers")
        if not data:
            return []
        transfers = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])

        move_vals_list = []
        skipped = 0

        for transfer in transfers:
            vals = builder.build_entry_move_vals(
                transfer,
                journal_type="general",
                qbo_id_field="qbo_transfer_id",
                line_builder_fn=lambda t, cur, rate, foreign: (
                    self._build_transfer_lines(builder, t, cur, rate, foreign)
                ),
                ref_prefix="Transfer QBO-",
            )
            if vals:
                move_vals_list.append(vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals_list)} transfers, skipped {skipped}")
        return move_vals_list

    @staticmethod
    def _build_transfer_lines(
        builder: QBOMoveBuilder,
        transfer: Dict,
        currency_id: int,
        exchange_rate: float,
        is_foreign: bool,
    ) -> Optional[List[tuple]]:
        """Build the two-line journal entry for a transfer."""
        qbo_id = transfer.get("Id")
        amount = float(transfer.get("Amount", 0) or 0)
        if amount <= 0:
            _logger.warning(f"Transfer {qbo_id} has no amount, skipping")
            return None

        # Get source account (FromAccountRef)
        from_ref = transfer.get("FromAccountRef", {})
        from_qbo_id = from_ref.get("value")
        from_account_id = builder.account_map.get(int(from_qbo_id)) if from_qbo_id else None
        if not from_account_id:
            _logger.warning(
                f"From account not found for QBO ID {from_qbo_id} in transfer {qbo_id}"
            )
            return None

        # Get destination account (ToAccountRef)
        to_ref = transfer.get("ToAccountRef", {})
        to_qbo_id = to_ref.get("value")
        to_account_id = builder.account_map.get(int(to_qbo_id)) if to_qbo_id else None
        if not to_account_id:
            _logger.warning(
                f"To account not found for QBO ID {to_qbo_id} in transfer {qbo_id}"
            )
            return None

        amount_company = builder.convert_to_company_currency(
            amount, exchange_rate, is_foreign
        )

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

        if is_foreign:
            from_line_vals["currency_id"] = currency_id
            from_line_vals["amount_currency"] = -amount
            to_line_vals["currency_id"] = currency_id
            to_line_vals["amount_currency"] = amount

        return [(0, 0, from_line_vals), (0, 0, to_line_vals)]

    @ETL.load()
    def load_transfers(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load transfers as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_transfers", [])

        if not move_vals_list:
            _logger.info("No new transfers to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals_list:
            qbo_id = vals.get("qbo_transfer_id", "?")
            with ctx.skippable(f"create transfer QBO#{qbo_id}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} transfers")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post transfer QBO#{move.qbo_transfer_id or '?'}"):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} transfers")
