"""QuickBooks Online SalesReceipt ETL Pipeline

This module handles the migration of SalesReceipts from QBO to Odoo
as account.move journal entries, using the ETL framework.

In QBO, a SalesReceipt represents a cash sale — an invoice and payment
combined into one transaction. Each line credits a revenue/item account,
and the total is debited to the bank account specified by
DepositToAccountRef (or Undeposited Funds if not specified).
"""

import logging
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.sales.receipt.importer",
    sap_source="SalesReceipt",
    depends_on=[
        "qbo.exchange.rate.importer",
        "qbo.account.importer",
        "qbo.item.importer",
        "qbo.customer.importer",
    ],
)
class QboSalesReceiptImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO SalesReceipts as journal entries."""

    _name = "qbo.sales.receipt.importer"
    _description = "QBO Sales Receipt Importer"

    @ETL.extract("SalesReceipt")
    def extract_sales_receipts(self, ctx: ETLContext) -> List[Dict]:
        """Extract sales receipts from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO sales receipt IDs
        ctx.env.cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'account_move' "
            "AND column_name = 'qbo_sales_receipt_id'"
        )
        if ctx.env.cr.fetchone():
            ctx.env.cr.execute(
                "SELECT qbo_sales_receipt_id FROM account_move "
                "WHERE qbo_sales_receipt_id IS NOT NULL"
            )
            existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        else:
            existing_ids = set()
            _logger.warning(
                "qbo_sales_receipt_id column not found - module upgrade required"
            )

        _logger.info(f"Found {len(existing_ids)} existing sales receipts in Odoo")

        # Fetch all sales receipts from QBO
        all_receipts = api_client.query_all(
            entity="SalesReceipt", order_by="Id"
        )

        # Filter out already imported
        new_receipts = [
            r for r in all_receipts if str(r.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(all_receipts)} sales receipts from QBO, "
            f"{len(new_receipts)} are new"
        )
        return new_receipts

    @ETL.transform()
    def transform_sales_receipts(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform QBO sales receipts into Odoo account.move values."""
        receipts = extracted.get("extract_sales_receipts", [])

        # Build account lookup
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        # Build product lookup
        ctx.env.cr.execute(
            "SELECT qbo_item_id, id FROM product_product "
            "WHERE qbo_item_id IS NOT NULL"
        )
        product_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        # Build customer lookup
        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner "
            "WHERE qbo_customer_id IS NOT NULL"
        )
        customer_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        # Get general journal
        journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not journal:
            raise ValueError("No general journal found for sales receipt entries")

        # Find Undeposited Funds account as fallback
        undeposited_funds = ctx.env["account.account"].search(
            [
                ("code", "=like", "1408%"),
                ("company_ids", "in", [company.id]),
            ],
            limit=1,
        )

        move_vals_list = []
        skipped = 0

        for receipt in receipts:
            move_vals = self._transform_receipt(
                receipt,
                account_map,
                product_map,
                customer_map,
                journal,
                company,
                undeposited_funds,
            )
            if move_vals:
                move_vals_list.append(move_vals)
            else:
                skipped += 1

        _logger.info(
            f"Transformed {len(move_vals_list)} sales receipts, "
            f"skipped {skipped}"
        )
        return move_vals_list

    def _transform_receipt(
        self,
        receipt: Dict,
        account_map: Dict,
        product_map: Dict,
        customer_map: Dict,
        journal,
        company,
        undeposited_funds,
    ) -> Optional[Dict]:
        """Transform a single QBO SalesReceipt into account.move values."""
        qbo_id = str(receipt.get("Id", ""))
        txn_date = receipt.get("TxnDate")
        total_amt = float(receipt.get("TotalAmt", 0) or 0)

        if total_amt <= 0:
            _logger.warning(f"SalesReceipt {qbo_id} has no amount, skipping")
            return None

        # Get currency and exchange rate
        currency_code = receipt.get("CurrencyRef", {}).get("value", "CAD")
        exchange_rate = float(receipt.get("ExchangeRate", 1.0) or 1.0)
        currency = company.env["res.currency"].search(
            [("name", "=", currency_code)], limit=1
        )
        if not currency:
            currency = company.currency_id

        is_foreign_currency = currency.id != company.currency_id.id

        # Get deposit-to account (debit side — bank or Undeposited Funds)
        deposit_to_ref = receipt.get("DepositToAccountRef", {})
        deposit_to_qbo_id = deposit_to_ref.get("value")
        deposit_to_account_id = account_map.get(str(deposit_to_qbo_id))
        if not deposit_to_account_id:
            if undeposited_funds:
                deposit_to_account_id = undeposited_funds.id
            else:
                _logger.warning(
                    f"Deposit-to account not found for QBO ID "
                    f"{deposit_to_qbo_id} in SalesReceipt {qbo_id}"
                )
                return None

        # Get customer partner
        customer_ref = receipt.get("CustomerRef", {})
        partner_id = customer_map.get(str(customer_ref.get("value")))

        # Build credit lines from receipt lines
        line_ids = []
        total_credit_foreign = 0.0
        total_credit_company = 0.0

        for line in receipt.get("Line", []):
            detail_type = line.get("DetailType", "")
            # Skip SubTotalLineDetail and other non-item lines
            if detail_type != "SalesItemLineDetail":
                continue

            line_vals = self._transform_receipt_line(
                line,
                account_map,
                product_map,
                currency,
                exchange_rate,
                is_foreign_currency,
                company,
                qbo_id,
            )
            if line_vals:
                amount_foreign = line_vals.pop("_amount_foreign", 0)
                total_credit_foreign += amount_foreign
                total_credit_company += line_vals["credit"]
                if partner_id:
                    line_vals["partner_id"] = partner_id
                line_ids.append((0, 0, line_vals))

        if not line_ids:
            _logger.warning(
                f"SalesReceipt {qbo_id} has no valid lines, skipping"
            )
            return None

        # Debit line for bank/deposit account
        if is_foreign_currency and exchange_rate:
            debit_company = round(total_amt * exchange_rate, 2)
        else:
            debit_company = total_amt

        debit_line_vals = {
            "account_id": deposit_to_account_id,
            "name": f"Sales Receipt {receipt.get('DocNumber', qbo_id)}",
            "debit": debit_company,
            "credit": 0,
        }
        if is_foreign_currency:
            debit_line_vals["currency_id"] = currency.id
            debit_line_vals["amount_currency"] = total_amt
        if partner_id:
            debit_line_vals["partner_id"] = partner_id

        line_ids.append((0, 0, debit_line_vals))

        # Balance rounding differences
        self._balance_lines(line_ids, receipt, is_foreign_currency)

        move_vals = {
            "move_type": "entry",
            "journal_id": journal.id,
            "date": txn_date,
            "ref": f"Sales Receipt QBO-{qbo_id}",
            "qbo_sales_receipt_id": qbo_id,
            "company_id": company.id,
            "currency_id": currency.id,
            "line_ids": line_ids,
        }
        if partner_id:
            move_vals["partner_id"] = partner_id

        return move_vals

    def _transform_receipt_line(
        self,
        line: Dict,
        account_map: Dict,
        product_map: Dict,
        currency,
        exchange_rate: float,
        is_foreign_currency: bool,
        company,
        receipt_qbo_id: str,
    ) -> Optional[Dict]:
        """Transform a single sales receipt line into account.move.line values."""
        detail = line.get("SalesItemLineDetail", {})
        if not detail:
            return None

        amount_foreign = float(line.get("Amount", 0) or 0)
        if amount_foreign <= 0:
            return None

        # Get account from line detail or product
        account_id = None

        # Try direct AccountRef on the line detail
        account_ref = detail.get("AccountRef", {})
        if account_ref.get("value"):
            account_id = account_map.get(str(account_ref.get("value")))

        # Fall back to product's income account
        if not account_id:
            item_ref = detail.get("ItemRef", {})
            qbo_item_id = item_ref.get("value")
            product_id = product_map.get(str(qbo_item_id))
            if product_id:
                product = company.env["product.product"].browse(product_id)
                account_id = (
                    product.property_account_income_id.id
                    or product.categ_id.property_account_income_categ_id.id
                )

        if not account_id:
            _logger.warning(
                f"No account found for line in SalesReceipt {receipt_qbo_id}"
            )
            return None

        # Convert to company currency
        if is_foreign_currency and exchange_rate:
            amount_company = round(amount_foreign * exchange_rate, 2)
        else:
            amount_company = amount_foreign

        line_vals = {
            "account_id": account_id,
            "credit": amount_company,
            "debit": 0,
            "name": line.get("Description") or "/",
            "_amount_foreign": amount_foreign,
        }

        if is_foreign_currency:
            line_vals["currency_id"] = currency.id
            line_vals["amount_currency"] = -amount_foreign  # Credit = negative

        return line_vals

    @staticmethod
    def _balance_lines(
        line_ids: list, receipt: dict, is_foreign_currency: bool
    ) -> None:
        """Adjust lines so debit/credit (and amount_currency) balance."""
        total_debit = sum(l[2]["debit"] for l in line_ids)
        total_credit = sum(l[2]["credit"] for l in line_ids)
        diff = round(total_debit - total_credit, 2)

        if diff != 0:
            if diff > 0:
                target = max(
                    (l for l in line_ids if l[2]["credit"] > 0),
                    key=lambda l: l[2]["credit"],
                    default=None,
                )
                if target:
                    target[2]["credit"] = round(target[2]["credit"] + diff, 2)
            else:
                target = max(
                    (l for l in line_ids if l[2]["debit"] > 0),
                    key=lambda l: l[2]["debit"],
                    default=None,
                )
                if target:
                    target[2]["debit"] = round(target[2]["debit"] - diff, 2)

            _logger.debug(
                f"Adjusted company currency by {diff} to balance "
                f"SalesReceipt {receipt.get('Id')}"
            )

        if is_foreign_currency:
            total_amount_currency = sum(
                l[2].get("amount_currency", 0) for l in line_ids
            )
            fc_diff = round(total_amount_currency, 2)

            if fc_diff != 0:
                if fc_diff > 0:
                    target = min(
                        (
                            l
                            for l in line_ids
                            if l[2].get("amount_currency", 0) < 0
                        ),
                        key=lambda l: l[2]["amount_currency"],
                        default=None,
                    )
                    if target:
                        target[2]["amount_currency"] = round(
                            target[2]["amount_currency"] - fc_diff, 2
                        )
                else:
                    target = max(
                        (
                            l
                            for l in line_ids
                            if l[2].get("amount_currency", 0) > 0
                        ),
                        key=lambda l: l[2]["amount_currency"],
                        default=None,
                    )
                    if target:
                        target[2]["amount_currency"] = round(
                            target[2]["amount_currency"] - fc_diff, 2
                        )

                _logger.debug(
                    f"Adjusted foreign currency by {fc_diff} to balance "
                    f"SalesReceipt {receipt.get('Id')}"
                )

    @ETL.load()
    def load_sales_receipts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load sales receipts as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_sales_receipts", [])

        if not move_vals_list:
            _logger.info("No new sales receipts to create")
            return

        created = 0
        posted = 0

        for vals in move_vals_list:
            qbo_id = vals.get("qbo_sales_receipt_id", "?")
            with ctx.skippable(f"sales receipt QBO#{qbo_id}"):
                move = ctx.env["account.move"].create(vals)
                created += 1
                move.action_post()
                posted += 1

        _logger.info(
            f"Created {created} sales receipts ({posted} posted)"
        )
