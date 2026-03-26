"""QuickBooks Online Transfer ETL Pipeline

This module handles the migration of Transfers from QBO to Odoo
as journal entries using the ETL framework.

QBO Transfers represent bank-to-bank transfers.  Each transfer always
creates **two journal entries** routed through the company's internal
transfer (transit) account — one per bank journal — mirroring Odoo's
native internal-transfer pattern.  The transit-account lines are
auto-reconciled after posting.

For accounts with a secondary currency (e.g. a USD bank account),
the bank line carries ``currency_id`` and ``amount_currency``.
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

        # Transit account — required for all transfers (two-JE pattern)
        company = ctx.env.company
        transit_account = company.transfer_account_id
        if not transit_account:
            _logger.error(
                "No internal transfer account configured on company — "
                "transfers cannot be imported"
            )
        extractor.extra["transit_account_id"] = transit_account.id if transit_account else None

        return ChunkableData(
            records=new_transfers,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_transfers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO transfers into Odoo account.move journal entry pairs.

        Every transfer produces two JEs routed through the company's
        internal transfer (transit) account — matching Odoo's native
        internal-transfer pattern.
        """
        data = extracted.get("extract_transfers")
        if not data:
            return []
        transfers = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])
        transit_account_id = builder.get_extra("transit_account_id")

        if not transit_account_id:
            _logger.error(
                "No internal transfer account configured on company — "
                "cannot import transfers"
            )
            return []

        move_vals_list = []
        skipped = 0

        for transfer in transfers:
            result = self._build_transfer_pair(
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
    def _build_transfer_pair(
        builder: QBOMoveBuilder,
        transfer: Dict,
        transit_account_id: int,
    ) -> Optional[List[Dict]]:
        """Build a pair of JEs for a QBO Transfer via the transit account.

        Always produces two JEs regardless of currency — each JE sits in
        the bank journal of one side, with the transit account as the
        counterpart.  Transit lines are reconciled after posting.

        For accounts with a secondary currency (e.g. USD bank), the bank
        line carries ``currency_id`` and ``amount_currency``.  For
        company-currency accounts, the line uses only ``debit``/``credit``.
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

        # QBO transaction currency and company-currency equivalent
        currency_id, is_foreign, exchange_rate = builder.resolve_currency(transfer)
        amount_company = builder.convert_to_company_currency(
            amount, exchange_rate, is_foreign,
        )
        company_currency_id = builder._company_currency_id

        txn_date = transfer.get("TxnDate")
        ref = f"Transfer QBO-{qbo_id}"

        # Per-account secondary currencies (None = company currency)
        from_acct_currency = builder.account_currency_map.get(int(from_qbo_id))
        to_acct_currency = builder.account_currency_map.get(int(to_qbo_id))

        # --- JE 1: source side (money leaving from_account) ---
        from_journal_id = (
            builder.get_journal_id_for_account(from_account_id, fallback_type=None)
            or builder.get_journal_id("general")
        )
        from_is_foreign = (
            from_acct_currency and from_acct_currency != company_currency_id
        )

        from_bank_line = {
            "account_id": from_account_id,
            "name": f"Transfer to {to_ref.get('name', 'account')}",
            "credit": amount_company,
            "debit": 0,
        }
        if from_is_foreign:
            from_bank_line["currency_id"] = from_acct_currency
            from_bank_line["amount_currency"] = -amount

        from_transit_line = {
            "account_id": transit_account_id,
            "name": f"Transfer to {to_ref.get('name', 'account')}",
            "debit": amount_company,
            "credit": 0,
        }

        je1_lines = [(0, 0, from_bank_line), (0, 0, from_transit_line)]

        je1_currency = from_acct_currency if from_is_foreign else company_currency_id
        je1 = {
            "move_type": "entry",
            "journal_id": from_journal_id,
            "date": txn_date,
            "ref": ref,
            "qbo_transfer_id": int(qbo_id) if qbo_id else 0,
            "company_id": builder._company_id,
            "currency_id": je1_currency,
            "line_ids": je1_lines,
        }

        # --- JE 2: destination side (money arriving to to_account) ---
        to_journal_id = (
            builder.get_journal_id_for_account(to_account_id, fallback_type=None)
            or builder.get_journal_id("general")
        )
        to_is_foreign = (
            to_acct_currency and to_acct_currency != company_currency_id
        )

        to_bank_line = {
            "account_id": to_account_id,
            "name": f"Transfer from {from_ref.get('name', 'account')}",
            "debit": amount_company,
            "credit": 0,
        }
        if to_is_foreign:
            to_bank_line["currency_id"] = to_acct_currency
            to_bank_line["amount_currency"] = amount

        to_transit_line = {
            "account_id": transit_account_id,
            "name": f"Transfer from {from_ref.get('name', 'account')}",
            "credit": amount_company,
            "debit": 0,
        }

        je2_lines = [(0, 0, to_bank_line), (0, 0, to_transit_line)]

        je2_currency = to_acct_currency if to_is_foreign else company_currency_id
        je2 = {
            "move_type": "entry",
            "journal_id": to_journal_id,
            "date": txn_date,
            "ref": ref,
            "qbo_transfer_id": int(qbo_id) if qbo_id else 0,
            "company_id": builder._company_id,
            "currency_id": je2_currency,
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
                try:
                    line_a, line_b = lines
                    # Identify debit and credit sides
                    if line_a.balance >= 0:
                        debit_line, credit_line = line_a, line_b
                    else:
                        debit_line, credit_line = line_b, line_a
                    amount = abs(debit_line.amount_residual)
                    ctx.env["account.partial.reconcile"].create({
                        "debit_move_id": debit_line.id,
                        "credit_move_id": credit_line.id,
                        "amount": amount,
                        "debit_amount_currency": abs(
                            debit_line.amount_residual_currency
                        ),
                        "credit_amount_currency": abs(
                            credit_line.amount_residual_currency
                        ),
                        "company_id": debit_line.company_id.id,
                    })
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
