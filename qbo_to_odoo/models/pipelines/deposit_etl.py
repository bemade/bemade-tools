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

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.deposit.importer",
    sap_source="Deposit",
    depends_on=[
        "qbo.exchange.rate.importer",
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
    def extract_deposits(self, ctx: ETLContext) -> List[Dict]:
        """Extract deposits from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO deposit IDs
        ctx.env.cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'account_move' AND column_name = 'qbo_deposit_id'"
        )
        if ctx.env.cr.fetchone():
            ctx.env.cr.execute(
                "SELECT qbo_deposit_id FROM account_move "
                "WHERE qbo_deposit_id IS NOT NULL"
            )
            existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        else:
            existing_ids = set()
            _logger.warning("qbo_deposit_id column not found - module upgrade required")

        _logger.info(f"Found {len(existing_ids)} existing deposits in Odoo")

        # Fetch all deposits from QBO
        all_deposits = api_client.query_all(entity="Deposit", order_by="Id")

        # Filter out already imported
        new_deposits = [d for d in all_deposits if str(d.get("Id")) not in existing_ids]

        _logger.info(
            f"Extracted {len(all_deposits)} deposits from QBO, "
            f"{len(new_deposits)} are new"
        )
        return new_deposits

    @ETL.transform()
    def transform_deposits(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO deposits into Odoo account.move journal entry values."""
        deposits = extracted.get("extract_deposits", [])

        # Build account lookup
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        # Build partner lookup (customer + vendor)
        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner "
            "WHERE qbo_customer_id IS NOT NULL"
        )
        partner_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}
        ctx.env.cr.execute(
            "SELECT qbo_vendor_id, id FROM res_partner "
            "WHERE qbo_vendor_id IS NOT NULL"
        )
        vendor_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        # Get general journal for deposits
        journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not journal:
            raise ValueError("No general journal found for deposit entries")

        move_vals_list = []
        skipped = 0

        for deposit in deposits:
            move_vals = self._transform_deposit(
                deposit, account_map, partner_map, vendor_map, journal, company
            )
            if move_vals:
                move_vals_list.append(move_vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals_list)} deposits, skipped {skipped}")
        return move_vals_list

    def _transform_deposit(
        self,
        deposit: Dict,
        account_map: Dict,
        partner_map: Dict,
        vendor_map: Dict,
        journal,
        company,
    ) -> Optional[Dict]:
        """Transform a single QBO Deposit into account.move values."""
        qbo_id = str(deposit.get("Id", ""))
        txn_date = deposit.get("TxnDate")
        total_amt = float(deposit.get("TotalAmt", 0) or 0)

        if total_amt <= 0:
            _logger.warning(f"Deposit {qbo_id} has no amount, skipping")
            return None

        # Get bank account (DepositToAccountRef) — debit side
        deposit_to_ref = deposit.get("DepositToAccountRef", {})
        deposit_to_qbo_id = deposit_to_ref.get("value")
        deposit_to_account_id = account_map.get(str(deposit_to_qbo_id))
        if not deposit_to_account_id:
            _logger.warning(
                f"Deposit-to account not found for QBO ID {deposit_to_qbo_id} "
                f"in deposit {qbo_id}"
            )
            return None

        # Get currency and exchange rate
        currency_code = deposit.get("CurrencyRef", {}).get("value", "CAD")
        exchange_rate = float(deposit.get("ExchangeRate", 1.0) or 1.0)
        currency = company.env["res.currency"].search(
            [("name", "=", currency_code)], limit=1
        )
        if not currency:
            currency = company.currency_id

        is_foreign_currency = currency.id != company.currency_id.id

        # Build credit lines from deposit lines
        line_ids = []
        total_credit_foreign = 0.0
        total_credit_company = 0.0

        for line in deposit.get("Line", []):
            # QBO deposit lines may have DetailType="DepositLineDetail" OR
            # just a "DepositLineDetail" key with no DetailType (payment
            # sweep lines linked to a Payment transaction).
            if "DepositLineDetail" not in line:
                _logger.debug(
                    f"Deposit {qbo_id} line has no DepositLineDetail, "
                    f"keys={list(line.keys())}"
                )
                continue

            line_vals = self._transform_deposit_line(
                line,
                account_map,
                partner_map,
                vendor_map,
                currency,
                exchange_rate,
                is_foreign_currency,
                company,
                qbo_id,
            )
            if line_vals:
                amount_foreign = line_vals.pop("_amount_foreign", 0)
                total_credit_foreign += amount_foreign
                total_credit_company += line_vals["credit"] - line_vals["debit"]
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
            if not detail_types:
                _logger.warning(f"Deposit {qbo_id} raw keys: {list(deposit.keys())}")
            return None

        # Debit line for bank account (DepositToAccountRef)
        if is_foreign_currency and exchange_rate:
            debit_company = round(total_amt * exchange_rate, 2)
        else:
            debit_company = total_amt

        debit_line_vals = {
            "account_id": deposit_to_account_id,
            "name": f"Deposit to {deposit_to_ref.get('name', 'bank')}",
            "debit": debit_company,
            "credit": 0,
        }
        if is_foreign_currency:
            debit_line_vals["currency_id"] = currency.id
            debit_line_vals["amount_currency"] = total_amt

        line_ids.append((0, 0, debit_line_vals))

        # Balance rounding differences
        self._balance_deposit_lines(line_ids, deposit, is_foreign_currency)

        return {
            "move_type": "entry",
            "journal_id": journal.id,
            "date": txn_date,
            "ref": f"Deposit QBO-{qbo_id}",
            "qbo_deposit_id": qbo_id,
            "company_id": company.id,
            "currency_id": currency.id,
            "line_ids": line_ids,
        }

    def _transform_deposit_line(
        self,
        line: Dict,
        account_map: Dict,
        partner_map: Dict,
        vendor_map: Dict,
        currency,
        exchange_rate: float,
        is_foreign_currency: bool,
        company,
        deposit_qbo_id: str,
    ) -> Optional[Dict]:
        """Transform a single deposit line into account.move.line values."""
        detail = line.get("DepositLineDetail", {})
        if not detail:
            return None

        amount_foreign = float(line.get("Amount", 0) or 0)
        if amount_foreign == 0:
            return None

        # Get credit account from line.  Payment sweep lines (linked to a
        # QBO Payment) typically have no AccountRef — they clear Undeposited
        # Funds.
        account_ref = detail.get("AccountRef", {})
        qbo_account_id = account_ref.get("value")
        account_id = account_map.get(str(qbo_account_id)) if qbo_account_id else None
        if not account_id:
            # Fall back to Undeposited Funds (payment sweep lines)
            uf_account = company.env["account.account"].search(
                [
                    ("name", "ilike", "Undeposited Funds"),
                    ("company_ids", "in", [company.id]),
                ],
                limit=1,
            )
            if not uf_account:
                _logger.warning(
                    f"No account and no Undeposited Funds fallback "
                    f"for deposit {deposit_qbo_id}"
                )
                return None
            account_id = uf_account.id

        # Convert to company currency
        abs_foreign = abs(amount_foreign)
        if is_foreign_currency and exchange_rate:
            abs_company = round(abs_foreign * exchange_rate, 2)
        else:
            abs_company = abs_foreign

        # Positive amounts are credits (funds deposited), negative amounts
        # are debits (e.g. bank service charges deducted from deposit).
        if amount_foreign > 0:
            line_vals = {
                "account_id": account_id,
                "credit": abs_company,
                "debit": 0,
                "name": line.get("Description") or detail.get("CheckNum") or "/",
                "_amount_foreign": amount_foreign,
            }
        else:
            line_vals = {
                "account_id": account_id,
                "debit": abs_company,
                "credit": 0,
                "name": line.get("Description") or detail.get("CheckNum") or "/",
                "_amount_foreign": amount_foreign,
            }

        if is_foreign_currency:
            line_vals["currency_id"] = currency.id
            # amount_currency sign follows debit/credit: negative for credit,
            # positive for debit.
            line_vals["amount_currency"] = -amount_foreign

        # Resolve partner from Entity reference
        entity = detail.get("Entity", {})
        entity_value = entity.get("value")
        if entity_value:
            entity_type = entity.get("type", "")
            partner_id = None
            if entity_type == "CUSTOMER":
                partner_id = partner_map.get(str(entity_value))
            elif entity_type == "VENDOR":
                partner_id = vendor_map.get(str(entity_value))
            else:
                # Try customer first, then vendor
                partner_id = partner_map.get(str(entity_value)) or vendor_map.get(
                    str(entity_value)
                )
            if partner_id:
                line_vals["partner_id"] = partner_id

        return line_vals

    @staticmethod
    def _balance_deposit_lines(
        line_ids: list, deposit: dict, is_foreign_currency: bool
    ) -> None:
        """Adjust deposit lines so debit/credit (and amount_currency) balance.

        Same approach as journal_entry_etl: adjust the largest line on the
        side that needs correction to fix rounding differences.
        """
        total_debit = sum(l[2]["debit"] for l in line_ids)
        total_credit = sum(l[2]["credit"] for l in line_ids)
        diff = round(total_debit - total_credit, 2)

        if diff != 0:
            if diff > 0:
                # Debit exceeds credit — increase the largest credit line
                target = max(
                    (l for l in line_ids if l[2]["credit"] > 0),
                    key=lambda l: l[2]["credit"],
                    default=None,
                )
                if target:
                    target[2]["credit"] = round(target[2]["credit"] + diff, 2)
            else:
                # Credit exceeds debit — increase the largest debit line
                target = max(
                    (l for l in line_ids if l[2]["debit"] > 0),
                    key=lambda l: l[2]["debit"],
                    default=None,
                )
                if target:
                    target[2]["debit"] = round(target[2]["debit"] - diff, 2)

            _logger.debug(
                f"Adjusted company currency by {diff} to balance "
                f"deposit {deposit.get('Id')}"
            )

        # Balance foreign currency (amount_currency) if applicable
        if is_foreign_currency:
            total_amount_currency = sum(
                l[2].get("amount_currency", 0) for l in line_ids
            )
            fc_diff = round(total_amount_currency, 2)

            if fc_diff != 0:
                if fc_diff > 0:
                    target = min(
                        (l for l in line_ids if l[2].get("amount_currency", 0) < 0),
                        key=lambda l: l[2]["amount_currency"],
                        default=None,
                    )
                    if target:
                        target[2]["amount_currency"] = round(
                            target[2]["amount_currency"] - fc_diff, 2
                        )
                else:
                    target = max(
                        (l for l in line_ids if l[2].get("amount_currency", 0) > 0),
                        key=lambda l: l[2]["amount_currency"],
                        default=None,
                    )
                    if target:
                        target[2]["amount_currency"] = round(
                            target[2]["amount_currency"] - fc_diff, 2
                        )

                _logger.debug(
                    f"Adjusted foreign currency by {fc_diff} to balance "
                    f"deposit {deposit.get('Id')}"
                )

    @ETL.load()
    def load_deposits(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load deposits as journal entries into Odoo."""
        move_vals_list = transformed.get("transform_deposits", [])

        if not move_vals_list:
            _logger.info("No new deposits to create")
            return

        created = 0
        posted = 0

        for vals in move_vals_list:
            qbo_id = vals.get("qbo_deposit_id", "?")
            with ctx.skippable(f"deposit QBO#{qbo_id}"):
                move = ctx.env["account.move"].create(vals)
                created += 1
                move.action_post()
                posted += 1

        _logger.info(f"Created {created} deposits ({posted} posted)")
