"""QuickBooks Online Deposit ETL Pipeline

This module handles the migration of Deposits from QBO to Odoo
as account.move journal entries, using the ETL framework.

In QBO, a Deposit groups one or more payments or other funds into
a bank deposit. Each deposit line credits the source account
(e.g. Undeposited Funds, income account) and the total is debited
to the bank account specified by DepositToAccountRef.
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
    importer_name="qbo.deposit.importer",
    sap_source="Deposit",
    depends_on=[
        "qbo.account.importer",
        "qbo.customer.importer",
        "qbo.vendor.importer",
    ],
)
class QboDepositImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Deposits as account.move journal entries."""

    _name = "qbo.deposit.importer"
    _description = "QBO Deposit Importer"

    @ETL.extract("Deposit")
    def extract_deposits(self, ctx: ETLContext) -> ChunkableData:
        """Extract deposits from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO deposit IDs
        existing_ids = extractor.existing_qbo_ids("account_move", "qbo_deposit_id")
        _logger.info(f"Found {len(existing_ids)} existing deposits in Odoo")

        # Fetch all deposits from QBO
        all_deposits = api_client.query_all(entity="Deposit", order_by="Id")

        # Filter out already imported
        new_deposits = [d for d in all_deposits if str(d.get("Id")) not in existing_ids]

        _logger.info(
            f"Extracted {len(all_deposits)} deposits from QBO, "
            f"{len(new_deposits)} are new"
        )

        # Ensure exchange rates exist for foreign-currency deposits
        ExchangeRateEnsurer(ctx.env).ensure_rates(new_deposits)

        # Preload maps for transform
        extractor.preload("account", "customer", "vendor", "currency")
        extractor.preload_journals("general")
        extractor.preload_undeposited_funds()

        return ChunkableData(
            records=new_deposits,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_deposits(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO deposits into Odoo account.move journal entry values."""
        data = extracted.get("extract_deposits")
        if not data:
            return []
        deposits = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])

        move_vals_list = []
        skipped = 0

        for deposit in deposits:
            vals = builder.build_entry_move_vals(
                deposit,
                journal_type="general",
                qbo_id_field="qbo_deposit_id",
                qbo_id_as_str=True,
                line_builder_fn=lambda d, cur, rate, foreign: (
                    self._build_deposit_lines(builder, d, cur, rate, foreign)
                ),
                ref_prefix="Deposit QBO-",
            )
            if vals:
                move_vals_list.append(vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals_list)} deposits, skipped {skipped}")
        return move_vals_list

    @staticmethod
    def _build_deposit_lines(
        builder: QBOMoveBuilder,
        deposit: Dict,
        currency_id: int,
        exchange_rate: float,
        is_foreign: bool,
    ) -> Optional[List[tuple]]:
        """Build credit lines + debit counter-line for a deposit."""
        qbo_id = str(deposit.get("Id", ""))
        total_amt = float(deposit.get("TotalAmt", 0) or 0)
        if total_amt <= 0:
            _logger.warning(f"Deposit {qbo_id} has no amount, skipping")
            return None

        # Get bank account (DepositToAccountRef) — debit side
        deposit_to_ref = deposit.get("DepositToAccountRef", {})
        deposit_to_qbo_id = deposit_to_ref.get("value")
        deposit_to_account_id = (
            builder.account_map.get(int(deposit_to_qbo_id))
            if deposit_to_qbo_id
            else None
        )
        if not deposit_to_account_id:
            _logger.warning(
                f"Deposit-to account not found for QBO ID {deposit_to_qbo_id} "
                f"in deposit {qbo_id}"
            )
            return None

        # Build credit lines from deposit lines
        line_ids = []
        for line in deposit.get("Line", []):
            if "DepositLineDetail" not in line:
                _logger.debug(
                    f"Deposit {qbo_id} line has no DepositLineDetail, "
                    f"keys={list(line.keys())}"
                )
                continue

            detail = line.get("DepositLineDetail", {})
            if not detail:
                continue

            amount_foreign = float(line.get("Amount", 0) or 0)
            if amount_foreign == 0:
                continue

            # Resolve account from detail, fallback to Undeposited Funds
            account_ref = detail.get("AccountRef", {})
            qbo_account_id = account_ref.get("value") if account_ref else None
            account_id = (
                builder.account_map.get(int(qbo_account_id))
                if qbo_account_id
                else None
            )
            if not account_id:
                uf_id = builder.undeposited_funds_id
                if not uf_id:
                    _logger.warning(
                        f"No account and no Undeposited Funds fallback "
                        f"for deposit {qbo_id}"
                    )
                    continue
                account_id = uf_id

            abs_foreign = abs(amount_foreign)
            abs_company = builder.convert_to_company_currency(
                abs_foreign, exchange_rate, is_foreign
            )

            # Positive = credit (funds deposited), negative = debit (bank charges)
            if amount_foreign > 0:
                line_vals = {
                    "account_id": account_id,
                    "credit": abs_company,
                    "debit": 0,
                    "name": line.get("Description") or detail.get("CheckNum") or "/",
                }
            else:
                line_vals = {
                    "account_id": account_id,
                    "debit": abs_company,
                    "credit": 0,
                    "name": line.get("Description") or detail.get("CheckNum") or "/",
                }

            if is_foreign:
                line_vals["currency_id"] = currency_id
                line_vals["amount_currency"] = -amount_foreign

            # Resolve partner from Entity reference
            entity = detail.get("Entity", {})
            entity_value = entity.get("value")
            if entity_value:
                entity_type = entity.get("type", "")
                partner_id = None
                if entity_type == "CUSTOMER":
                    partner_id = builder.customer_map.get(int(entity_value))
                elif entity_type == "VENDOR":
                    partner_id = builder.vendor_map.get(int(entity_value))
                else:
                    try:
                        ev = int(entity_value)
                    except (ValueError, TypeError):
                        ev = None
                    if ev is not None:
                        partner_id = (
                            builder.customer_map.get(ev)
                            or builder.vendor_map.get(ev)
                        )
                if partner_id:
                    line_vals["partner_id"] = partner_id

            line_ids.append((0, 0, line_vals))

        if not line_ids:
            detail_types = [
                l.get("DetailType", "MISSING") for l in deposit.get("Line", [])
            ]
            _logger.warning(
                f"Deposit {qbo_id} has no valid lines, skipping. "
                f"Line count={len(deposit.get('Line', []))}, "
                f"DetailTypes={detail_types}"
            )
            return None

        # Debit line for bank account (DepositToAccountRef)
        debit_company = builder.convert_to_company_currency(
            total_amt, exchange_rate, is_foreign
        )
        debit_line_vals = {
            "account_id": deposit_to_account_id,
            "name": f"Deposit to {deposit_to_ref.get('name', 'bank')}",
            "debit": debit_company,
            "credit": 0,
        }
        if is_foreign:
            debit_line_vals["currency_id"] = currency_id
            debit_line_vals["amount_currency"] = total_amt

        line_ids.append((0, 0, debit_line_vals))
        return line_ids

    @ETL.load()
    def load_deposits(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load deposits as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_deposits", [])

        if not move_vals_list:
            _logger.info("No new deposits to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals_list:
            qbo_id = vals.get("qbo_deposit_id", "?")
            with ctx.skippable(f"create deposit QBO#{qbo_id}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} deposits")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post deposit QBO#{move.qbo_deposit_id or '?'}"):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} deposits")
