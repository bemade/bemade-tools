"""QuickBooks Online Expense ETL Pipeline

This module handles the migration of Purchases (Expenses) from QBO to Odoo
as account.move journal entries, using the ETL framework.

In QBO, the Purchase entity represents expense transactions including
Cash, Check, and Credit Card payments. These are imported as journal
entries with debit lines for each expense and a credit line for the
payment account.
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
    importer_name="qbo.expense.importer",
    sap_source="Purchase",
    depends_on=[
        "qbo.account.importer",
        "qbo.item.importer",
        "qbo.tax.importer",
        "qbo.category.account.fixer",
    ],
)
class QboExpenseImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Purchases as account.move journal entries."""

    _name = "qbo.expense.importer"
    _description = "QBO Expense Importer"

    @ETL.extract("Purchase")
    def extract_expenses(self, ctx: ETLContext) -> ChunkableData:
        """Extract purchases from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO expense IDs
        existing_ids = extractor.existing_qbo_ids("account_move", "qbo_expense_id")
        _logger.info(f"Found {len(existing_ids)} existing expenses in Odoo")

        # Fetch all purchases from QBO
        all_purchases = api_client.query_all(entity="Purchase", order_by="Id")

        # Filter out already imported
        new_purchases = [
            p for p in all_purchases if str(p.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(all_purchases)} purchases from QBO, "
            f"{len(new_purchases)} are new"
        )

        # Preload maps for transform
        extractor.preload("account", "product", "product_expense", "currency")
        extractor.preload_journals("general")

        # Tax rate ref → tax account ID for expenses with TxnTaxDetail
        extractor.preload_tax_rate_account_map()

        return ChunkableData(
            records=new_purchases,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_expenses(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO purchases into Odoo account.move journal entry values."""
        data = extracted.get("extract_expenses")
        if not data:
            return []
        purchases = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])

        move_vals_list = []
        skipped = 0

        for purchase in purchases:
            vals = builder.build_entry_move_vals(
                purchase,
                journal_type="general",
                qbo_id_field="qbo_expense_id",
                line_builder_fn=lambda p, cur, rate, foreign: (
                    self._build_expense_lines(builder, p, cur, rate, foreign)
                ),
            )
            if vals:
                move_vals_list.append(vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals_list)} purchases, skipped {skipped}")
        return move_vals_list

    @staticmethod
    def _build_expense_lines(
        builder: QBOMoveBuilder,
        purchase: Dict,
        currency_id: int,
        exchange_rate: float,
        is_foreign: bool,
    ) -> Optional[List[tuple]]:
        """Build expense debit lines + credit counter-line."""
        # Get payment account (credit side)
        account_ref = purchase.get("AccountRef", {})
        payment_account_qbo_id = account_ref.get("value")
        payment_account_id = (
            builder.account_map.get(int(payment_account_qbo_id))
            if payment_account_qbo_id
            else None
        )
        if not payment_account_id:
            _logger.warning(
                f"Payment account not found for QBO ID {payment_account_qbo_id}"
            )
            return None

        line_ids = []
        total_amount_foreign = 0.0
        total_amount_company = 0.0

        for line in purchase.get("Line", []):
            detail_type = line.get("DetailType", "")
            if detail_type not in (
                "AccountBasedExpenseLineDetail",
                "ItemBasedExpenseLineDetail",
            ):
                continue
            detail = line.get(detail_type, {})
            if not detail:
                continue

            line_vals = builder.build_entry_line(
                line, detail, detail_type,
                currency_id, exchange_rate, is_foreign,
                direction="expense",
            )
            if line_vals:
                amount_foreign = line_vals.pop("_amount_foreign", 0)
                total_amount_foreign += amount_foreign
                total_amount_company += line_vals.get("debit", 0) - line_vals.get("credit", 0)
                line_ids.append((0, 0, line_vals))

        if not line_ids:
            return None

        # Add tax lines from TxnTaxDetail (not included in Line entries)
        tax_line_tuples, total_tax_company = builder.build_tax_lines_from_detail(
            purchase, currency_id, exchange_rate, is_foreign,
        )
        line_ids.extend(tax_line_tuples)
        # Tax amounts affect the total owed to/from the payment account
        total_amount_company += total_tax_company

        # Compute foreign-currency tax total for the credit line
        total_tax_foreign = 0.0
        if is_foreign:
            for _, _, tl in tax_line_tuples:
                total_tax_foreign += tl.get("amount_currency", 0)
            total_amount_foreign += total_tax_foreign

        # Credit line for payment account
        credit_line_vals = {
            "account_id": payment_account_id,
            "credit": total_amount_company,
            "debit": 0,
            "name": f"Payment - {purchase.get('PaymentType', 'Expense')}",
        }
        if is_foreign:
            credit_line_vals["currency_id"] = currency_id
            credit_line_vals["amount_currency"] = -total_amount_foreign

        line_ids.append((0, 0, credit_line_vals))

        # QBO Purchase entities with Credit=true are refunds/credits —
        # the debit/credit sides are reversed.
        if purchase.get("Credit"):
            for _, _, lv in line_ids:
                d, c = lv.get("debit", 0), lv.get("credit", 0)
                lv["debit"] = c
                lv["credit"] = d
                if "amount_currency" in lv:
                    lv["amount_currency"] = -lv["amount_currency"]

        return line_ids

    @ETL.load()
    def load_expenses(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load purchases as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_expenses", [])

        if not move_vals_list:
            _logger.info("No new purchases to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals_list:
            qbo_id = vals.get("qbo_expense_id", "?")
            with ctx.skippable(f"create expense QBO#{qbo_id}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} expense entries")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post expense QBO#{move.qbo_expense_id or '?'}"):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} expense entries")
