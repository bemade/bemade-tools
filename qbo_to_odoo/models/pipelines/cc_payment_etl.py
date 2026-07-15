"""QuickBooks Online Credit Card Payment ETL Pipeline

Imports QBO CreditCardPayment entities as journal entries.  These
represent payments from a bank account to a credit card account
(paying off the CC balance).

Each CreditCardPayment becomes a JE:
    Debit  credit card account (reduces CC liability)
    Credit bank account        (money leaves bank)
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, post_lock

from .extractor import QBOExtractor
from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.cc.payment.importer",
    sap_source="CreditCardPayment",
    depends_on=["qbo.account.importer"],
)
class QboCCPaymentImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Credit Card Payments as journal entries."""

    _name = "qbo.cc.payment.importer"
    _description = "QBO Credit Card Payment Importer"

    @ETL.extract("CreditCardPayment")
    def extract_cc_payments(self, ctx: ETLContext) -> List[Dict]:
        """Extract credit card payments from QBO API."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        existing_ids = extractor.existing_qbo_ids(
            "account_move", "qbo_cc_payment_id"
        )
        _logger.info(f"Found {len(existing_ids)} existing CC payments in Odoo")

        all_payments = api_client.query_all(
            entity="CreditCardPayment", order_by="Id"
        )

        new_payments = [
            p for p in all_payments
            if str(p.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(all_payments)} CC payments from QBO, "
            f"{len(new_payments)} are new"
        )

        extractor.preload("account")
        extractor.preload_journals("general")

        # GL-truth amounts from the cached JournalReport. QBO's
        # CreditCardPayment.Amount can disagree with what actually posted to
        # the GL (observed: API Amount 3647.97 vs GL 5000.00). The
        # JournalReport is authoritative for the trial balance, so the load
        # phase books the GL amount when the two differ.
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        connection._ensure_journal_cache()
        ctx.env.cr.execute(
            """
            SELECT t.qbo_txn_id, sum(l.debit)
            FROM qbo_journal_cache_transaction t
            JOIN qbo_journal_cache_line l ON l.transaction_id = t.id
            WHERE t.txn_type = 'Credit Card Payment'
            GROUP BY t.qbo_txn_id
            """
        )
        gl_amounts = {
            str(tid): float(amt)
            for tid, amt in ctx.env.cr.fetchall()
            if tid
        }

        return {
            "payments": new_payments,
            "extractor": extractor.export(),
            "gl_amounts": gl_amounts,
        }

    @ETL.transform()
    def transform_cc_payments(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform QBO credit card payments into journal entry values."""
        data = extracted.get("extract_cc_payments", {})
        payments = data.get("payments", [])
        extractor_data = data.get("extractor", {})
        gl_amounts = data.get("gl_amounts", {})

        if not payments:
            return []

        from .move_builder import QBOMoveBuilder

        builder = QBOMoveBuilder(extractor_data)
        journal_id = builder.get_journal_id("general")
        company_id = builder._company_id

        move_vals = []
        skipped = 0

        for payment in payments:
            qbo_id = int(payment.get("Id", 0))
            api_amount = float(payment.get("Amount", 0) or 0)
            # Prefer the GL-truth amount from the cached JournalReport: QBO's
            # CreditCardPayment.Amount sometimes disagrees with what actually
            # posted (e.g. API 3647.97 vs GL 5000.00), which understates both
            # the bank and the credit-card clearing account.
            gl_amount = gl_amounts.get(str(qbo_id))
            amount = gl_amount if gl_amount else api_amount
            if gl_amount is not None and abs(gl_amount - api_amount) > 0.01:
                _logger.info(
                    "CC payment %s: booking GL amount %.2f over API Amount %.2f",
                    qbo_id, gl_amount, api_amount,
                )
            if amount == 0:
                skipped += 1
                continue

            # Resolve credit card account (debit side — paying down liability)
            cc_ref = payment.get("CreditCardAccountRef", {})
            cc_qbo_id = cc_ref.get("value")
            cc_account_id = (
                builder.account_map.get(int(cc_qbo_id))
                if cc_qbo_id else None
            )
            if not cc_account_id:
                _logger.warning(
                    f"CC account not found for QBO ID {cc_qbo_id} "
                    f"in CC payment {qbo_id}"
                )
                skipped += 1
                continue

            # Resolve bank account (credit side — money leaving bank)
            bank_ref = payment.get("BankAccountRef", {})
            bank_qbo_id = bank_ref.get("value")
            bank_account_id = (
                builder.account_map.get(int(bank_qbo_id))
                if bank_qbo_id else None
            )
            if not bank_account_id:
                _logger.warning(
                    f"Bank account not found for QBO ID {bank_qbo_id} "
                    f"in CC payment {qbo_id}"
                )
                skipped += 1
                continue

            txn_date = payment.get("TxnDate")
            memo = payment.get("Memo", "") or payment.get("PrivateNote", "")

            lines = [
                (0, 0, {
                    "account_id": cc_account_id,
                    "name": memo or f"CC payment QBO-CCP-{qbo_id}",
                    "debit": amount,
                    "credit": 0,
                }),
                (0, 0, {
                    "account_id": bank_account_id,
                    "name": memo or f"CC payment QBO-CCP-{qbo_id}",
                    "debit": 0,
                    "credit": amount,
                }),
            ]

            move_vals.append({
                "move_type": "entry",
                "journal_id": journal_id,
                "date": txn_date,
                "ref": f"CC Payment QBO-CCP-{qbo_id}",
                "qbo_cc_payment_id": qbo_id,
                "company_id": company_id,
                "line_ids": lines,
            })

        _logger.info(
            f"Transformed {len(move_vals)} CC payments, skipped {skipped}"
        )
        return move_vals

    @ETL.load()
    def load_cc_payments(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load credit card payments as journal entries into Odoo."""
        move_vals = transformed.get("transform_cc_payments", [])

        if not move_vals:
            _logger.info("No new CC payments to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals:
            qbo_id = vals.get("qbo_cc_payment_id", "?")
            with ctx.skippable(f"create CC payment QBO-CCP#{qbo_id}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} CC payment entries")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(
                        f"post CC payment QBO-CCP#{move.qbo_cc_payment_id or '?'}"
                    ):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} CC payment entries")
