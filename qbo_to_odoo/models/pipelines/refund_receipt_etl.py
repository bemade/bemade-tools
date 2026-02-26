"""QuickBooks Online RefundReceipt ETL Pipeline

This module handles the migration of RefundReceipts from QBO to Odoo
as account.move journal entries, using the ETL framework.

In QBO, a RefundReceipt is the reverse of a SalesReceipt — it represents
a cash refund to a customer. Each line debits a revenue/item account
(reversing the sale), and the total is credited from the bank account
specified by DepositToAccountRef.
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
    importer_name="qbo.refund.receipt.importer",
    sap_source="RefundReceipt",
    depends_on=[
        "qbo.account.importer",
        "qbo.item.importer",
        "qbo.customer.importer",
        "qbo.category.account.fixer",
    ],
)
class QboRefundReceiptImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO RefundReceipts as journal entries."""

    _name = "qbo.refund.receipt.importer"
    _description = "QBO Refund Receipt Importer"

    @ETL.extract("RefundReceipt")
    def extract_refund_receipts(self, ctx: ETLContext) -> ChunkableData:
        """Extract refund receipts from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO refund receipt IDs
        existing_ids = extractor.existing_qbo_ids(
            "account_move", "qbo_refund_receipt_id"
        )
        _logger.info(f"Found {len(existing_ids)} existing refund receipts in Odoo")

        all_receipts = api_client.query_all(
            entity="RefundReceipt", order_by="Id"
        )

        new_receipts = [
            r for r in all_receipts if str(r.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(all_receipts)} refund receipts from QBO, "
            f"{len(new_receipts)} are new"
        )

        # Ensure exchange rates exist for foreign-currency refund receipts
        ExchangeRateEnsurer(ctx.env).ensure_rates(new_receipts)

        # Preload maps for transform
        extractor.preload(
            "account", "customer", "product", "product_income", "currency"
        )
        extractor.preload_journals("general")

        return ChunkableData(
            records=new_receipts,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_refund_receipts(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform QBO refund receipts into Odoo account.move values."""
        data = extracted.get("extract_refund_receipts")
        if not data:
            return []
        receipts = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])

        move_vals_list = []
        skipped = 0

        for receipt in receipts:
            vals = builder.build_entry_move_vals(
                receipt,
                journal_type="general",
                qbo_id_field="qbo_refund_receipt_id",
                qbo_id_as_str=True,
                line_builder_fn=lambda r, cur, rate, foreign: (
                    self._build_refund_lines(builder, r, cur, rate, foreign)
                ),
                ref_prefix="Refund Receipt QBO-",
            )
            if vals:
                partner_id = builder.resolve_partner(receipt, "customer")
                if partner_id:
                    vals["partner_id"] = partner_id
                move_vals_list.append(vals)
            else:
                skipped += 1

        _logger.info(
            f"Transformed {len(move_vals_list)} refund receipts, "
            f"skipped {skipped}"
        )
        return move_vals_list

    @staticmethod
    def _build_refund_lines(
        builder: QBOMoveBuilder,
        receipt: Dict,
        currency_id: int,
        exchange_rate: float,
        is_foreign: bool,
    ) -> Optional[List[tuple]]:
        """Build debit lines + credit counter-line for a refund receipt."""
        qbo_id = str(receipt.get("Id", ""))
        total_amt = float(receipt.get("TotalAmt", 0) or 0)
        if total_amt <= 0:
            _logger.warning(f"RefundReceipt {qbo_id} has no amount, skipping")
            return None

        # Get refund-from account (credit side — bank account)
        deposit_to_ref = receipt.get("DepositToAccountRef", {})
        deposit_to_qbo_id = deposit_to_ref.get("value")
        deposit_to_account_id = (
            builder.account_map.get(int(deposit_to_qbo_id))
            if deposit_to_qbo_id
            else None
        )
        if not deposit_to_account_id:
            _logger.warning(
                f"Deposit-to account not found for QBO ID "
                f"{deposit_to_qbo_id} in RefundReceipt {qbo_id}"
            )
            return None

        partner_id = builder.resolve_partner(receipt, "customer")

        # Build debit lines (reversing revenue)
        line_ids = []
        for line in receipt.get("Line", []):
            if line.get("DetailType") != "SalesItemLineDetail":
                continue
            detail = line.get("SalesItemLineDetail", {})
            if not detail:
                continue

            amount_foreign = float(line.get("Amount", 0) or 0)
            if amount_foreign <= 0:
                continue

            amount_company = builder.convert_to_company_currency(
                amount_foreign, exchange_rate, is_foreign
            )

            # Resolve account: AccountRef -> product income fallback
            product_id = builder.resolve_product(detail)
            account_id = builder.resolve_account(detail, product_id, "income")
            if not account_id:
                _logger.warning(
                    f"No account found for line in RefundReceipt {qbo_id}"
                )
                continue

            line_vals = {
                "account_id": account_id,
                "debit": amount_company,
                "credit": 0,
                "name": line.get("Description") or "/",
            }
            if is_foreign:
                line_vals["currency_id"] = currency_id
                line_vals["amount_currency"] = amount_foreign
            if partner_id:
                line_vals["partner_id"] = partner_id
            line_ids.append((0, 0, line_vals))

        if not line_ids:
            _logger.warning(
                f"RefundReceipt {qbo_id} has no valid lines, skipping"
            )
            return None

        # Credit line for bank account
        credit_company = builder.convert_to_company_currency(
            total_amt, exchange_rate, is_foreign
        )
        credit_line_vals = {
            "account_id": deposit_to_account_id,
            "name": f"Refund Receipt {receipt.get('DocNumber', qbo_id)}",
            "credit": credit_company,
            "debit": 0,
        }
        if is_foreign:
            credit_line_vals["currency_id"] = currency_id
            credit_line_vals["amount_currency"] = -total_amt
        if partner_id:
            credit_line_vals["partner_id"] = partner_id

        line_ids.append((0, 0, credit_line_vals))
        return line_ids

    @ETL.load()
    def load_refund_receipts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load refund receipts as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_refund_receipts", [])

        if not move_vals_list:
            _logger.info("No new refund receipts to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals_list:
            qbo_id = vals.get("qbo_refund_receipt_id", "?")
            with ctx.skippable(f"create refund receipt QBO#{qbo_id}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} refund receipts")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(
                        f"post refund receipt QBO#{move.qbo_refund_receipt_id or '?'}"
                    ):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} refund receipts")
