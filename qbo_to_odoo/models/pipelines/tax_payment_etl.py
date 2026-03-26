"""QuickBooks Online Tax Payment ETL Pipeline

Imports QBO TaxPayment entities as journal entries.  These are sales tax
remittances (GST/QST payments to the government).

Each TaxPayment becomes a JE:
    Payment (Refund=false): Debit tax payable, Credit bank
    Refund  (Refund=true):  Debit bank, Credit tax payable
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
    importer_name="qbo.tax.payment.importer",
    sap_source="TaxPayment",
    depends_on=["qbo.account.importer", "qbo.tax.importer"],
)
class QboTaxPaymentImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Tax Payments as journal entries."""

    _name = "qbo.tax.payment.importer"
    _description = "QBO Tax Payment Importer"

    @ETL.extract("TaxPayment")
    def extract_tax_payments(self, ctx: ETLContext) -> List[Dict]:
        """Extract tax payments from QBO API."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        existing_ids = extractor.existing_qbo_ids(
            "account_move", "qbo_tax_payment_id"
        )
        _logger.info(f"Found {len(existing_ids)} existing tax payments in Odoo")

        all_payments = api_client.query_all(
            entity="TaxPayment", order_by="Id"
        )

        new_payments = [
            p for p in all_payments
            if str(p.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(all_payments)} tax payments from QBO, "
            f"{len(new_payments)} are new"
        )

        # Preload account map and journal map
        extractor.preload("account")
        extractor.preload_journals("general")

        # QBO routes tax payments through the GlobalTaxSuspense account
        # (2310 GST/HST - QST Suspense), not the payable account.
        tax_suspense = ctx.env["account.account"].search(
            [
                ("qbo_id", "!=", False),
                ("code", "=", "2310"),
                ("company_ids", "in", [ctx.env.company.id]),
            ],
            limit=1,
        )
        if not tax_suspense:
            # Broader fallback — look for GlobalTaxSuspense subtype
            tax_suspense = ctx.env["account.account"].search(
                [
                    ("qbo_id", "!=", False),
                    ("name", "ilike", "Suspense"),
                    ("name", "ilike", "GST"),
                    ("company_ids", "in", [ctx.env.company.id]),
                ],
                limit=1,
            )
        extractor.extra["tax_suspense_account_id"] = (
            tax_suspense.id if tax_suspense else None
        )
        if not tax_suspense:
            _logger.error(
                "No tax suspense account found — tax payments cannot be imported"
            )

        return {"payments": new_payments, "extractor": extractor.export()}

    @ETL.transform()
    def transform_tax_payments(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform QBO tax payments into journal entry values."""
        data = extracted.get("extract_tax_payments", {})
        payments = data.get("payments", [])
        extractor_data = data.get("extractor", {})

        if not payments:
            return []

        from .move_builder import QBOMoveBuilder

        builder = QBOMoveBuilder(extractor_data)
        tax_suspense_id = builder.get_extra("tax_suspense_account_id")

        if not tax_suspense_id:
            _logger.error("No tax suspense account — skipping all tax payments")
            return []

        journal_id = builder.get_journal_id("general")
        company_id = builder._company_id

        move_vals = []
        skipped = 0

        for payment in payments:
            qbo_id = int(payment.get("Id", 0))
            amount = float(payment.get("PaymentAmount", 0) or 0)
            if amount == 0:
                skipped += 1
                continue

            # Resolve bank account
            acct_ref = payment.get("PaymentAccountRef", {})
            acct_qbo_id = acct_ref.get("value")
            bank_account_id = (
                builder.account_map.get(int(acct_qbo_id))
                if acct_qbo_id else None
            )
            if not bank_account_id:
                _logger.warning(
                    f"Bank account not found for QBO ID {acct_qbo_id} "
                    f"in tax payment {qbo_id}"
                )
                skipped += 1
                continue

            txn_date = payment.get("PaymentDate")
            abs_amount = abs(amount)

            # QBO: negative amount with Refund=true means money leaving
            # the bank (paying the government).  Positive with Refund=false
            # means money coming back (government refund to bank).
            # Confusingly, QBO labels paying the government as "Refund".
            is_refund = payment.get("Refund", False)

            if is_refund:
                # Money leaves bank → pays down tax liability
                # Debit tax payable, Credit bank
                lines = [
                    (0, 0, {
                        "account_id": tax_suspense_id,
                        "name": f"Sales tax remittance QBO-TP-{qbo_id}",
                        "debit": abs_amount,
                        "credit": 0,
                    }),
                    (0, 0, {
                        "account_id": bank_account_id,
                        "name": f"Sales tax remittance QBO-TP-{qbo_id}",
                        "debit": 0,
                        "credit": abs_amount,
                    }),
                ]
            else:
                # Money enters bank → tax refund from government
                # Debit bank, Credit tax payable
                lines = [
                    (0, 0, {
                        "account_id": bank_account_id,
                        "name": f"Sales tax refund QBO-TP-{qbo_id}",
                        "debit": abs_amount,
                        "credit": 0,
                    }),
                    (0, 0, {
                        "account_id": tax_suspense_id,
                        "name": f"Sales tax refund QBO-TP-{qbo_id}",
                        "debit": 0,
                        "credit": abs_amount,
                    }),
                ]

            move_vals.append({
                "move_type": "entry",
                "journal_id": journal_id,
                "date": txn_date,
                "ref": f"Tax Payment QBO-TP-{qbo_id}",
                "qbo_tax_payment_id": qbo_id,
                "company_id": company_id,
                "line_ids": lines,
            })

        _logger.info(
            f"Transformed {len(move_vals)} tax payments, skipped {skipped}"
        )
        return move_vals

    @ETL.load()
    def load_tax_payments(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load tax payments as journal entries into Odoo."""
        move_vals = transformed.get("transform_tax_payments", [])

        if not move_vals:
            _logger.info("No new tax payments to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals:
            qbo_id = vals.get("qbo_tax_payment_id", "?")
            with ctx.skippable(f"create tax payment QBO-TP#{qbo_id}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} tax payment entries")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(
                        f"post tax payment QBO-TP#{move.qbo_tax_payment_id or '?'}"
                    ):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} tax payment entries")
