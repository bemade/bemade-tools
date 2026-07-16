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
        "qbo.tax.importer",
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

        # Preload maps for transform
        extractor.preload("account", "customer", "vendor", "currency")
        extractor.preload_journals("general")
        extractor.preload_undeposited_funds()

        # Tax rate ref → tax account ID for deposits with TxnTaxDetail, plus
        # the per-txn GL-truth override for txns QBO booked to the other
        # variant.
        extractor.preload_tax_rate_account_map(use_suspense=True)
        extractor.preload_txn_tax_gl_accounts(ctx, ("Deposit",))

        # GL-truth per-account nets: a foreign deposit moves money from
        # Undeposited Funds (valued at the original payments' rates) to the
        # bank (deposit-date rate); QBO books the realized-FX difference to
        # the exchange G/L.  Rebuilding lines at the deposit rate misses
        # both, so the builder trues every account's home-currency net to
        # QBO's own GL from the JournalReport cache.
        connection = ctx.env["qbo.connection"].browse(
            ctx.get_config("source_id")
        )
        cache = connection._ensure_journal_cache()
        ctx.env.cr.execute(
            """
            SELECT t.qbo_txn_id, l.account_code, sum(l.debit - l.credit)
            FROM qbo_journal_cache_transaction t
            JOIN qbo_journal_cache_line l ON l.transaction_id = t.id
            WHERE t.cache_id = %(cache_id)s
              AND t.txn_type = 'Deposit'
              AND l.account_code IS NOT NULL
            GROUP BY 1, 2
            """,
            {"cache_id": cache.id},
        )
        gl_nets: Dict[str, Dict[str, float]] = {}
        for qid, code, net in ctx.env.cr.fetchall():
            gl_nets.setdefault(str(qid), {})[code] = round(float(net), 2)
        extractor.extra["deposit_gl_nets"] = gl_nets

        ctx.env.cr.execute(
            """
            SELECT code_store::jsonb ->> %(cid)s, id
            FROM account_account
            WHERE code_store::jsonb ->> %(cid)s IS NOT NULL
            """,
            {"cid": str(ctx.env.company.id)},
        )
        extractor.extra["gl_code_account_map"] = dict(ctx.env.cr.fetchall())

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
                # A bare LinkedTxn line (no DepositLineDetail) is money already
                # held in Undeposited Funds by a linked transaction — e.g. a
                # Sales Tax Payment refund — that this deposit moves to the bank.
                # Clear it against Undeposited Funds so the holding account nets
                # to zero, exactly as QBO's own journal does. Without this the
                # funds stay stranded in Undeposited Funds and the bank line is
                # understated by the same amount.
                linked_amt = float(line.get("Amount", 0) or 0)
                if line.get("LinkedTxn") and linked_amt != 0:
                    uf_id = builder.undeposited_funds_id
                    if uf_id:
                        abs_company = builder.convert_to_company_currency(
                            abs(linked_amt), exchange_rate, is_foreign
                        )
                        uf_line_vals = {
                            "account_id": uf_id,
                            "name": "Deposited from Undeposited Funds",
                            "credit": abs_company if linked_amt > 0 else 0,
                            "debit": 0 if linked_amt > 0 else abs_company,
                        }
                        if is_foreign:
                            uf_line_vals["currency_id"] = currency_id
                            uf_line_vals["amount_currency"] = -linked_amt
                        line_ids.append((0, 0, uf_line_vals))
                        continue
                    _logger.warning(
                        f"Deposit {qbo_id} has a bare LinkedTxn line but no "
                        f"Undeposited Funds account; skipping"
                    )
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

        # Add tax lines from TxnTaxDetail — deposits are income,
        # so tax collected is a credit (liability).
        tax_line_tuples, _total_tax_company = builder.build_tax_lines_from_detail(
            deposit, currency_id, exchange_rate, is_foreign, as_credit=True,
        )
        line_ids.extend(tax_line_tuples)

        # GL-truth per-account truing: adjust each account's home-currency
        # net to what QBO's own GL booked for this deposit.  A foreign
        # deposit's source lines carry the ORIGINAL payments' rates in QBO
        # (Undeposited Funds releases at payment-rate value) and the
        # realized-FX difference goes to the exchange G/L — neither of which
        # `foreign × deposit-rate` reproduces.  The bank leg (excluded here)
        # then self-balances onto QBO's exact amount.
        gl_targets = (builder.get_extra("deposit_gl_nets") or {}).get(qbo_id)
        if gl_targets:
            code_map = builder.get_extra("gl_code_account_map") or {}
            for code, target in gl_targets.items():
                acct_id = code_map.get(code)
                if not acct_id or acct_id == deposit_to_account_id:
                    continue
                built = round(sum(
                    l[2].get("debit", 0) - l[2].get("credit", 0)
                    for l in line_ids
                    if l[2].get("account_id") == acct_id
                ), 2)
                delta = round(target - built, 2)
                if abs(delta) < 0.01:
                    continue
                cands = [
                    l for l in line_ids if l[2].get("account_id") == acct_id
                ]
                if cands:
                    # Fold the delta into the account's largest line
                    # (home-currency only; the foreign amount stays QBO's).
                    lv = max(
                        cands,
                        key=lambda l: abs(
                            l[2].get("debit", 0) - l[2].get("credit", 0)
                        ),
                    )[2]
                    net = round(
                        lv.get("debit", 0) - lv.get("credit", 0) + delta, 2
                    )
                    lv["debit"] = net if net >= 0 else 0.0
                    lv["credit"] = -net if net < 0 else 0.0
                else:
                    # Account QBO booked but no built line carries it —
                    # typically the exchange G/L realized-FX plug.
                    new_vals = {
                        "account_id": acct_id,
                        "name": "GL true-up (QBO)",
                        "debit": delta if delta > 0 else 0.0,
                        "credit": -delta if delta < 0 else 0.0,
                    }
                    if is_foreign:
                        new_vals["currency_id"] = currency_id
                        new_vals["amount_currency"] = 0.0
                    line_ids.append((0, 0, new_vals))

        # Debit line for bank account — computed from actual line totals
        # rather than TotalAmt, which may include linked transactions
        # (e.g. TaxPayment refunds) with no DepositLineDetail.
        total_credits = sum(l[2].get("credit", 0) for l in line_ids)
        total_debits = sum(l[2].get("debit", 0) for l in line_ids)
        debit_company = total_credits - total_debits
        # After GL truing, the self-balanced bank leg must land on QBO's
        # bank amount; a residual means some built line sits on an account
        # QBO's GL doesn't carry for this txn (e.g. a tax-routing fallback).
        if gl_targets:
            code_map = builder.get_extra("gl_code_account_map") or {}
            bank_codes = [
                c for c, aid in code_map.items()
                if aid == deposit_to_account_id and c in gl_targets
            ]
            if bank_codes:
                qbo_bank = gl_targets[bank_codes[0]]
                # Compare account NETS: the deposit may carry other lines on
                # the bank account itself (e.g. a same-account in-and-out).
                built_bank = round(debit_company + sum(
                    l[2].get("debit", 0) - l[2].get("credit", 0)
                    for l in line_ids
                    if l[2].get("account_id") == deposit_to_account_id
                ), 2)
                if abs(round(built_bank - qbo_bank, 2)) >= 0.01:
                    _logger.warning(
                        "Deposit %s: bank account net %.2f != QBO %.2f after "
                        "GL truing — a line sits on an account QBO's GL "
                        "doesn't carry for this txn",
                        qbo_id, built_bank, qbo_bank,
                    )
        debit_line_vals = {
            "account_id": deposit_to_account_id,
            "name": f"Deposit to {deposit_to_ref.get('name', 'bank')}",
            "debit": debit_company,
            "credit": 0,
        }
        if is_foreign:
            # Compute foreign amount from lines rather than TotalAmt
            fc_credits = sum(
                abs(l[2].get("amount_currency", 0))
                for l in line_ids if l[2].get("credit", 0) > 0
            )
            fc_debits = sum(
                abs(l[2].get("amount_currency", 0))
                for l in line_ids if l[2].get("debit", 0) > 0
            )
            debit_line_vals["currency_id"] = currency_id
            debit_line_vals["amount_currency"] = fc_credits - fc_debits

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
