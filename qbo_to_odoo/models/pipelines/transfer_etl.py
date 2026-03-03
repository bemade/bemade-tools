"""QuickBooks Online Transfer ETL Pipeline

This module handles the migration of Transfers from QBO to Odoo
as journal entries using the ETL framework.

QBO Transfers represent bank-to-bank transfers. Each transfer creates
one or two journal entries depending on whether the source and
destination accounts are in the same currency:

* **Same-currency**: a single JE in the bank journal of the source
  account debiting the destination and crediting the source.
* **Cross-currency**: two JEs routed through the company's internal
  transfer (transit) account — one per bank journal — mirroring
  Odoo's native internal-transfer pattern.  The transit-account lines
  are auto-reconciled after posting.
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
    depends_on=["qbo.account.importer", "qbo.bank.journal.processor"],
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

        # Preload maps for transform (account_currency needed for
        # cross-currency detection; account_journal for bank journal routing)
        extractor.preload("account", "account_currency", "currency")
        extractor.preload_journals("general")
        extractor.preload_account_journal_map()

        # Transit account for cross-currency transfers
        company = ctx.env.company
        transit_account = company.transfer_account_id
        if not transit_account:
            _logger.warning(
                "No internal transfer account configured on company — "
                "cross-currency transfers will fall back to single-JE mode"
            )
        extractor.extra["transit_account_id"] = transit_account.id if transit_account else None

        return ChunkableData(
            records=new_transfers,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_transfers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO transfers into Odoo account.move journal entry values.

        Same-currency transfers produce one move; cross-currency transfers
        produce a pair of moves routed through the transit account.
        """
        data = extracted.get("extract_transfers")
        if not data:
            return []
        transfers = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])
        transit_account_id = builder.get_extra("transit_account_id")

        move_vals_list = []
        skipped = 0

        for transfer in transfers:
            result = self._build_transfer_moves(
                builder, transfer, transit_account_id,
            )
            if result:
                move_vals_list.extend(result)
            else:
                skipped += 1

        _logger.info(
            f"Transformed {len(transfers) - skipped} transfers into "
            f"{len(move_vals_list)} journal entries, skipped {skipped}"
        )
        return move_vals_list

    # ------------------------------------------------------------------
    # Transform helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_transfer_moves(
        builder: QBOMoveBuilder,
        transfer: Dict,
        transit_account_id: Optional[int],
    ) -> Optional[List[Dict]]:
        """Build one or two JE vals dicts for a single QBO Transfer.

        Returns a list of move vals (length 1 for same-currency, 2 for
        cross-currency) or None to skip.
        """
        qbo_id = transfer.get("Id")
        amount = float(transfer.get("Amount", 0) or 0)
        if amount <= 0:
            _logger.warning(f"Transfer {qbo_id} has no amount, skipping")
            return None

        # Resolve source and destination accounts
        from_ref = transfer.get("FromAccountRef", {})
        from_qbo_id = from_ref.get("value")
        from_account_id = (
            builder.account_map.get(int(from_qbo_id)) if from_qbo_id else None
        )
        if not from_account_id:
            _logger.warning(
                f"From account not found for QBO ID {from_qbo_id} "
                f"in transfer {qbo_id}"
            )
            return None

        to_ref = transfer.get("ToAccountRef", {})
        to_qbo_id = to_ref.get("value")
        to_account_id = (
            builder.account_map.get(int(to_qbo_id)) if to_qbo_id else None
        )
        if not to_account_id:
            _logger.warning(
                f"To account not found for QBO ID {to_qbo_id} "
                f"in transfer {qbo_id}"
            )
            return None

        # Resolve QBO transaction currency
        currency_id, is_foreign, exchange_rate = builder.resolve_currency(transfer)
        amount_company = builder.convert_to_company_currency(
            amount, exchange_rate, is_foreign,
        )

        # Determine whether the two accounts are in different currencies.
        # account_currency_map is keyed by QBO ID and holds the Odoo
        # currency_id (None = company currency).
        from_currency = builder.account_currency_map.get(int(from_qbo_id))
        to_currency = builder.account_currency_map.get(int(to_qbo_id))
        is_cross_currency = (
            is_foreign
            and transit_account_id
            and from_currency != to_currency
        )

        txn_date = transfer.get("TxnDate")
        ref = f"Transfer QBO-{qbo_id}"

        if is_cross_currency:
            assert transit_account_id is not None  # guarded by is_cross_currency
            return QboTransferImporter._build_cross_currency_pair(
                builder, qbo_id, txn_date, ref,
                amount, amount_company, currency_id,
                from_account_id, to_account_id,
                from_ref, to_ref,
                transit_account_id,
            )

        # Same-currency: single JE (source journal preferred)
        return QboTransferImporter._build_same_currency_move(
            builder, qbo_id, txn_date, ref,
            amount, amount_company, currency_id, is_foreign,
            from_account_id, to_account_id,
            from_ref, to_ref,
        )

    @staticmethod
    def _build_same_currency_move(
        builder: QBOMoveBuilder,
        qbo_id, txn_date, ref,
        amount, amount_company, currency_id, is_foreign,
        from_account_id, to_account_id,
        from_ref, to_ref,
    ) -> Optional[List[Dict]]:
        """Single JE for a same-currency transfer."""
        # Prefer the bank journal of the source account; fall back to general
        journal_id = (
            builder.get_journal_id_for_account(from_account_id, fallback_type=None)
            or builder.get_journal_id_for_account(to_account_id, fallback_type=None)
            or builder.get_journal_id("general")
        )

        from_line = {
            "account_id": from_account_id,
            "name": f"Transfer to {to_ref.get('name', 'account')}",
            "credit": amount_company,
            "debit": 0,
        }
        to_line = {
            "account_id": to_account_id,
            "name": f"Transfer from {from_ref.get('name', 'account')}",
            "debit": amount_company,
            "credit": 0,
        }

        if is_foreign:
            from_line["currency_id"] = currency_id
            from_line["amount_currency"] = -amount
            to_line["currency_id"] = currency_id
            to_line["amount_currency"] = amount

        line_ids = [(0, 0, from_line), (0, 0, to_line)]
        builder.balance_lines(line_ids, is_foreign, ref)

        return [{
            "move_type": "entry",
            "journal_id": journal_id,
            "date": txn_date,
            "ref": ref,
            "qbo_transfer_id": int(qbo_id) if qbo_id else 0,
            "company_id": builder._company_id,
            "currency_id": currency_id,
            "line_ids": line_ids,
        }]

    @staticmethod
    def _build_cross_currency_pair(
        builder: QBOMoveBuilder,
        qbo_id, txn_date, ref,
        amount_foreign, amount_company,
        foreign_currency_id,
        from_account_id, to_account_id,
        from_ref, to_ref,
        transit_account_id: int,
    ) -> Optional[List[Dict]]:
        """Two JEs for a cross-currency transfer via the transit account.

        QBO CurrencyRef/Amount are in the foreign currency (e.g. USD).
        amount_company is the CAD equivalent (amount × rate).

        JE 1 — source bank journal (foreign currency):
            Credit source account  (foreign amount)
            Debit  transit account (company amount)

        JE 2 — destination bank journal (company currency):
            Debit  destination account (company amount)
            Credit transit account     (company amount)
        """
        company_currency_id = builder._company_currency_id

        # --- JE 1: outgoing from the foreign-currency bank ---
        from_journal_id = (
            builder.get_journal_id_for_account(from_account_id, fallback_type=None)
            or builder.get_journal_id("general")
        )

        from_line = {
            "account_id": from_account_id,
            "name": f"Transfer to {to_ref.get('name', 'account')}",
            "credit": amount_company,
            "debit": 0,
            "currency_id": foreign_currency_id,
            "amount_currency": -amount_foreign,
        }
        transit_debit_line = {
            "account_id": transit_account_id,
            "name": f"Transfer to {to_ref.get('name', 'account')}",
            "debit": amount_company,
            "credit": 0,
        }

        je1_lines = [(0, 0, from_line), (0, 0, transit_debit_line)]
        builder.balance_lines(je1_lines, True, f"{ref} (out)")

        je1 = {
            "move_type": "entry",
            "journal_id": from_journal_id,
            "date": txn_date,
            "ref": ref,
            "qbo_transfer_id": int(qbo_id) if qbo_id else 0,
            "company_id": builder._company_id,
            "currency_id": foreign_currency_id,
            "line_ids": je1_lines,
        }

        # --- JE 2: incoming to the company-currency bank ---
        to_journal_id = (
            builder.get_journal_id_for_account(to_account_id, fallback_type=None)
            or builder.get_journal_id("general")
        )

        to_line = {
            "account_id": to_account_id,
            "name": f"Transfer from {from_ref.get('name', 'account')}",
            "debit": amount_company,
            "credit": 0,
        }
        transit_credit_line = {
            "account_id": transit_account_id,
            "name": f"Transfer from {from_ref.get('name', 'account')}",
            "credit": amount_company,
            "debit": 0,
        }

        je2_lines = [(0, 0, to_line), (0, 0, transit_credit_line)]
        builder.balance_lines(je2_lines, False, f"{ref} (in)")

        je2 = {
            "move_type": "entry",
            "journal_id": to_journal_id,
            "date": txn_date,
            "ref": ref,
            "qbo_transfer_id": int(qbo_id) if qbo_id else 0,
            "company_id": builder._company_id,
            "currency_id": company_currency_id,
            "line_ids": je2_lines,
        }

        return [je1, je2]

    @ETL.load()
    def load_transfers(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load transfers as journal entries into Odoo and reconcile transit lines."""
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
                    with ctx.skippable(
                        f"post transfer QBO#{move.qbo_transfer_id or '?'}"
                    ):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} transfers")

        # Reconcile transit-account lines for cross-currency transfer pairs
        self._reconcile_transit_lines(ctx, moves)

    @staticmethod
    def _reconcile_transit_lines(ctx: ETLContext, moves) -> None:
        """Auto-reconcile transit-account lines that share the same qbo_transfer_id.

        Cross-currency transfers create two JEs with the same
        ``qbo_transfer_id``.  Each JE has one line on the transit account.
        Reconciling these two lines zeroes out the transit account.
        """
        transit_account = ctx.env.company.transfer_account_id
        if not transit_account:
            return

        # Group transit-account lines by qbo_transfer_id
        transit_lines = moves.mapped("line_ids").filtered(
            lambda l: l.account_id == transit_account
        )
        if not transit_lines:
            return

        groups: Dict[int, list] = {}
        for line in transit_lines:
            qbo_tid = line.move_id.qbo_transfer_id
            if qbo_tid:
                groups.setdefault(qbo_tid, [])
                groups[qbo_tid].append(line)

        reconciled = 0
        for qbo_tid, lines in groups.items():
            if len(lines) == 2:
                pair = lines[0] | lines[1]
                try:
                    pair.reconcile()
                    reconciled += 1
                except Exception:
                    _logger.warning(
                        f"Could not reconcile transit lines for "
                        f"transfer QBO#{qbo_tid}",
                        exc_info=True,
                    )

        if reconciled:
            _logger.info(
                f"Reconciled {reconciled} transit-account line pairs"
            )
