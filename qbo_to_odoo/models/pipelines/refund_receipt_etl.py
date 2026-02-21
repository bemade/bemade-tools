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

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.refund.receipt.importer",
    sap_source="RefundReceipt",
    depends_on=[
        "qbo.exchange.rate.importer",
        "qbo.account.importer",
        "qbo.item.importer",
        "qbo.customer.importer",
    ],
)
class QboRefundReceiptImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO RefundReceipts as journal entries."""

    _name = "qbo.refund.receipt.importer"
    _description = "QBO Refund Receipt Importer"

    @ETL.extract("RefundReceipt")
    def extract_refund_receipts(self, ctx: ETLContext) -> List[Dict]:
        """Extract refund receipts from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO refund receipt IDs
        ctx.env.cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'account_move' "
            "AND column_name = 'qbo_refund_receipt_id'"
        )
        if ctx.env.cr.fetchone():
            ctx.env.cr.execute(
                "SELECT qbo_refund_receipt_id FROM account_move "
                "WHERE qbo_refund_receipt_id IS NOT NULL"
            )
            existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        else:
            existing_ids = set()
            _logger.warning(
                "qbo_refund_receipt_id column not found - module upgrade required"
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
        return new_receipts

    @ETL.transform()
    def transform_refund_receipts(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform QBO refund receipts into Odoo account.move values."""
        receipts = extracted.get("extract_refund_receipts", [])

        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_item_id, id FROM product_product "
            "WHERE qbo_item_id IS NOT NULL"
        )
        product_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner "
            "WHERE qbo_customer_id IS NOT NULL"
        )
        customer_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not journal:
            raise ValueError("No general journal found for refund receipt entries")

        move_vals_list = []
        skipped = 0

        for receipt in receipts:
            move_vals = self._transform_refund(
                receipt, account_map, product_map, customer_map,
                journal, company,
            )
            if move_vals:
                move_vals_list.append(move_vals)
            else:
                skipped += 1

        _logger.info(
            f"Transformed {len(move_vals_list)} refund receipts, "
            f"skipped {skipped}"
        )
        return move_vals_list

    def _transform_refund(
        self,
        receipt: Dict,
        account_map: Dict,
        product_map: Dict,
        customer_map: Dict,
        journal,
        company,
    ) -> Optional[Dict]:
        """Transform a single QBO RefundReceipt into account.move values."""
        qbo_id = str(receipt.get("Id", ""))
        txn_date = receipt.get("TxnDate")
        total_amt = float(receipt.get("TotalAmt", 0) or 0)

        if total_amt <= 0:
            _logger.warning(f"RefundReceipt {qbo_id} has no amount, skipping")
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

        # Get refund-from account (credit side — bank account)
        deposit_to_ref = receipt.get("DepositToAccountRef", {})
        deposit_to_qbo_id = deposit_to_ref.get("value")
        deposit_to_account_id = account_map.get(str(deposit_to_qbo_id))
        if not deposit_to_account_id:
            _logger.warning(
                f"Deposit-to account not found for QBO ID "
                f"{deposit_to_qbo_id} in RefundReceipt {qbo_id}"
            )
            return None

        partner_id = customer_map.get(
            str(receipt.get("CustomerRef", {}).get("value"))
        )

        # Build debit lines (reversing revenue)
        line_ids = []
        total_debit_company = 0.0

        for line in receipt.get("Line", []):
            if line.get("DetailType") != "SalesItemLineDetail":
                continue

            line_vals = self._transform_refund_line(
                line, account_map, product_map, currency,
                exchange_rate, is_foreign_currency, company, qbo_id,
            )
            if line_vals:
                line_vals.pop("_amount_foreign", 0)
                total_debit_company += line_vals["debit"]
                if partner_id:
                    line_vals["partner_id"] = partner_id
                line_ids.append((0, 0, line_vals))

        if not line_ids:
            _logger.warning(
                f"RefundReceipt {qbo_id} has no valid lines, skipping"
            )
            return None

        # Credit line for bank account
        if is_foreign_currency and exchange_rate:
            credit_company = round(total_amt * exchange_rate, 2)
        else:
            credit_company = total_amt

        credit_line_vals = {
            "account_id": deposit_to_account_id,
            "name": f"Refund Receipt {receipt.get('DocNumber', qbo_id)}",
            "credit": credit_company,
            "debit": 0,
        }
        if is_foreign_currency:
            credit_line_vals["currency_id"] = currency.id
            credit_line_vals["amount_currency"] = -total_amt
        if partner_id:
            credit_line_vals["partner_id"] = partner_id

        line_ids.append((0, 0, credit_line_vals))

        # Balance rounding differences
        self._balance_lines(line_ids, receipt, is_foreign_currency)

        move_vals = {
            "move_type": "entry",
            "journal_id": journal.id,
            "date": txn_date,
            "ref": f"Refund Receipt QBO-{qbo_id}",
            "qbo_refund_receipt_id": qbo_id,
            "company_id": company.id,
            "currency_id": currency.id,
            "line_ids": line_ids,
        }
        if partner_id:
            move_vals["partner_id"] = partner_id

        return move_vals

    def _transform_refund_line(
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
        """Transform a refund receipt line — debit (reverse revenue)."""
        detail = line.get("SalesItemLineDetail", {})
        if not detail:
            return None

        amount_foreign = float(line.get("Amount", 0) or 0)
        if amount_foreign <= 0:
            return None

        # Get account from line detail or product
        account_id = None
        account_ref = detail.get("AccountRef", {})
        if account_ref.get("value"):
            account_id = account_map.get(str(account_ref.get("value")))

        if not account_id:
            item_ref = detail.get("ItemRef", {})
            product_id = product_map.get(str(item_ref.get("value")))
            if product_id:
                product = company.env["product.product"].browse(product_id)
                account_id = (
                    product.property_account_income_id.id
                    or product.categ_id.property_account_income_categ_id.id
                )

        if not account_id:
            _logger.warning(
                f"No account found for line in RefundReceipt {receipt_qbo_id}"
            )
            return None

        if is_foreign_currency and exchange_rate:
            amount_company = round(amount_foreign * exchange_rate, 2)
        else:
            amount_company = amount_foreign

        line_vals = {
            "account_id": account_id,
            "debit": amount_company,
            "credit": 0,
            "name": line.get("Description") or "/",
            "_amount_foreign": amount_foreign,
        }

        if is_foreign_currency:
            line_vals["currency_id"] = currency.id
            line_vals["amount_currency"] = amount_foreign  # Debit = positive

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
                f"RefundReceipt {receipt.get('Id')}"
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
                            l for l in line_ids
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
                            l for l in line_ids
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
                    f"RefundReceipt {receipt.get('Id')}"
                )

    @ETL.load()
    def load_refund_receipts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load refund receipts as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_refund_receipts", [])

        if not move_vals_list:
            _logger.info("No new refund receipts to create")
            return

        created = 0
        posted = 0

        for vals in move_vals_list:
            qbo_id = vals.get("qbo_refund_receipt_id", "?")
            with ctx.skippable(f"refund receipt QBO#{qbo_id}"):
                move = ctx.env["account.move"].create(vals)
                created += 1
                move.action_post()
                posted += 1

        _logger.info(
            f"Created {created} refund receipts ({posted} posted)"
        )
