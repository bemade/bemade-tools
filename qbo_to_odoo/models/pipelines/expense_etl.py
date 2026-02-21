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

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.expense.importer",
    sap_source="Purchase",
    depends_on=[
        "qbo.exchange.rate.importer",
        "qbo.account.importer",
        "qbo.item.importer",
    ],
)
class QboExpenseImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Purchases as account.move journal entries."""

    _name = "qbo.expense.importer"
    _description = "QBO Expense Importer"

    @ETL.extract("Purchase")
    def extract_expenses(self, ctx: ETLContext) -> List[Dict]:
        """Extract purchases from QBO API.

        Note: In QBO, expenses are stored as "Purchase" entities which cover
        Cash Expense, Check, and Credit Card transactions.
        """
        api_client = get_api_client(ctx)

        # Get existing QBO expense IDs
        ctx.env.cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'account_move' AND column_name = 'qbo_expense_id'"
        )
        if ctx.env.cr.fetchone():
            ctx.env.cr.execute(
                "SELECT qbo_expense_id FROM account_move WHERE qbo_expense_id IS NOT NULL"
            )
            existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        else:
            existing_ids = set()
            _logger.warning("qbo_expense_id column not found - module upgrade required")
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
        return new_purchases

    @ETL.transform()
    def transform_expenses(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO purchases into Odoo account.move journal entry values."""
        purchases = extracted.get("extract_expenses", [])

        # Build account lookup
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        # Build product lookup
        ctx.env.cr.execute(
            "SELECT qbo_item_id, id FROM product_product WHERE qbo_item_id IS NOT NULL"
        )
        product_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        # Get expense journal from company config or find default
        company = ctx.env.company
        expense_journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not expense_journal:
            raise ValueError("No general journal found for expense entries")

        move_vals_list = []
        skipped = 0

        for purchase in purchases:
            move_vals = self._transform_purchase(
                purchase,
                account_map,
                product_map,
                expense_journal,
                company,
            )
            if move_vals:
                move_vals_list.append(move_vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals_list)} purchases, skipped {skipped}")
        return move_vals_list

    def _transform_purchase(
        self,
        purchase: Dict,
        account_map: Dict,
        product_map: Dict,
        journal,
        company,
    ) -> Optional[Dict]:
        """Transform a single QBO Purchase into account.move values."""
        qbo_id = purchase.get("Id")
        txn_date = purchase.get("TxnDate")

        # Get payment account (credit side)
        account_ref = purchase.get("AccountRef", {})
        payment_account_qbo_id = account_ref.get("value")
        payment_account_id = account_map.get(str(payment_account_qbo_id))
        if not payment_account_id:
            raise ValueError(
                f"Payment account not found for QBO ID {payment_account_qbo_id}"
            )

        # Get currency and exchange rate
        currency_code = purchase.get("CurrencyRef", {}).get("value", "USD")
        exchange_rate = float(purchase.get("ExchangeRate", 1.0) or 1.0)
        currency = company.env["res.currency"].search(
            [("name", "=", currency_code)], limit=1
        )
        if not currency:
            currency = company.currency_id

        # Determine if foreign currency
        is_foreign_currency = currency.id != company.currency_id.id

        # Build journal entry lines
        line_ids = []
        total_amount_foreign = 0.0
        total_amount_company = 0.0

        for line in purchase.get("Line", []):
            line_vals = self._transform_purchase_line(
                line,
                account_map,
                product_map,
                currency,
                exchange_rate,
                is_foreign_currency,
                company,
            )
            if line_vals:
                line_ids.append((0, 0, line_vals))
                total_amount_foreign += line_vals.get("_amount_foreign", 0)
                total_amount_company += line_vals.get("debit", 0)
                # Remove internal tracking field
                if "_amount_foreign" in line_vals:
                    del line_vals["_amount_foreign"]

        if not line_ids:
            return None

        # Add credit line for payment account
        credit_line_vals = {
            "account_id": payment_account_id,
            "credit": total_amount_company,
            "debit": 0,
            "name": f"Payment - {purchase.get('PaymentType', 'Expense')}",
        }
        if is_foreign_currency:
            credit_line_vals["currency_id"] = currency.id
            credit_line_vals["amount_currency"] = -total_amount_foreign

        line_ids.append((0, 0, credit_line_vals))

        return {
            "move_type": "entry",
            "journal_id": journal.id,
            "date": txn_date,
            "qbo_expense_id": qbo_id,
            "company_id": company.id,
            "currency_id": currency.id,
            "line_ids": line_ids,
        }

    def _transform_purchase_line(
        self,
        line: Dict,
        account_map: Dict,
        product_map: Dict,
        currency,
        exchange_rate: float,
        is_foreign_currency: bool,
        company,
    ) -> Optional[Dict]:
        """Transform a single purchase line into account.move.line values."""
        detail_type = line.get("DetailType", "")
        amount_foreign = float(line.get("Amount", 0) or 0)

        if amount_foreign <= 0:
            return None

        # Convert to company currency if needed
        if is_foreign_currency and exchange_rate:
            amount_company = amount_foreign * exchange_rate
        else:
            amount_company = amount_foreign

        line_vals = {
            "debit": amount_company,
            "credit": 0,
            "_amount_foreign": amount_foreign,  # Internal tracking, removed later
        }

        # Add currency fields for foreign currency
        if is_foreign_currency:
            line_vals["currency_id"] = currency.id
            line_vals["amount_currency"] = amount_foreign  # Debit = positive

        if line.get("Description"):
            line_vals["name"] = line.get("Description")

        # Handle account-based expense lines
        if detail_type == "AccountBasedExpenseLineDetail":
            detail = line.get("AccountBasedExpenseLineDetail", {})
            if not detail:
                return None

            account_ref = detail.get("AccountRef", {})
            qbo_account_id = account_ref.get("value")
            account_id = account_map.get(str(qbo_account_id))
            if not account_id:
                _logger.warning(f"Account not found for QBO ID {qbo_account_id}")
                return None

            line_vals["account_id"] = account_id

            if not line_vals.get("name"):
                line_vals["name"] = account_ref.get("name", "Expense")

        # Handle item-based expense lines
        elif detail_type == "ItemBasedExpenseLineDetail":
            detail = line.get("ItemBasedExpenseLineDetail", {})
            if not detail:
                return None

            item_ref = detail.get("ItemRef", {})
            qbo_item_id = item_ref.get("value")
            product_id = product_map.get(str(qbo_item_id))

            if product_id:
                product = company.env["product.product"].browse(product_id)
                account_id = (
                    product.property_account_expense_id.id
                    or product.categ_id.property_account_expense_categ_id.id
                )
                if account_id:
                    line_vals["account_id"] = account_id
                else:
                    _logger.warning(
                        f"No expense account for product {item_ref.get('name')}"
                    )
                    return None
            else:
                _logger.warning(f"Product not found for QBO ID {qbo_item_id}")
                return None

            if not line_vals.get("name"):
                line_vals["name"] = item_ref.get("name", "Expense")

        else:
            return None

        return line_vals

    @ETL.load()
    def load_expenses(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load purchases as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_expenses", [])

        if not move_vals_list:
            _logger.info("No new purchases to create")
            return

        created = 0
        posted = 0

        for vals in move_vals_list:
            qbo_id = vals.get("qbo_expense_id", "?")
            with ctx.skippable(f"expense QBO#{qbo_id}"):
                move = ctx.env["account.move"].create(vals)
                created += 1
                move.action_post()
                posted += 1

        _logger.info(f"Created {created} expense entries ({posted} posted)")
