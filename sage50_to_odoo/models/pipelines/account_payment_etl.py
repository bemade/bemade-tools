"""Sage applications -> `account.payment`, reconciled against the document.

Sage records every receipt, payment and applied credit against a document as
a row in `tcustrdt` / `tventrdt`. Those rows are what makes an imported
document's residual differ from what the document was worth, so re-creating
them is the other half of importing a document at its full amount: without
them every open document would show as unpaid for its whole original value.

Each application becomes a payment in the bank journal Sage recorded it
against, and is reconciled with the document's own receivable or payable
line. The residual then comes out of the reconciliation, the way it would if
the client had entered it — rather than being baked into the invoice.

Only what Sage still shows as outstanding is imported, so these are the
applications against documents that are *not* fully settled. A document paid
off in full does not come across at all, and neither do its payments.
"""

import logging

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from .account_move_open_item_etl import SIDES

_logger = logging.getLogger(__name__)

#: Which side of a payment Sage's application represents, per document side.
PAYMENT_SHAPE = {
    "customer": {"partner_type": "customer", "control": "asset_receivable"},
    "vendor": {"partner_type": "supplier", "control": "liability_payable"},
}


@ETL.pipeline(
    target_model="account.payment",
    importer_name="sage.payment.importer",
    sap_source="tcustrdt",
    depends_on=[
        "sage.open.item.importer",
        "sage.bank.journal.importer",
    ],
    allow_multiprocessing=False,
)
class SagePaymentImporter(models.AbstractModel):
    _name = "sage.payment.importer"
    _description = "Sage 50 Payment Importer"

    @ETL.extract("tcustrdt")
    def extract_applications(self, ctx: ETLContext) -> list:
        """Reads what the open-item pipeline already staged.

        The applications were collected alongside their documents, because
        that is where the control account and the reversal pairing are known.
        Re-deriving them here would duplicate that logic and risk the two
        drifting apart.
        """
        Move = ctx.env["account.move"]
        applications = []
        documents = Move.search([
            ("sage_doc_id", "!=", 0),
            ("company_id", "=", ctx.get_config("company_id")),
            ("state", "=", "posted"),
        ])
        staged = ctx.env["sage.open.item.importer"].extract_open_items(ctx)
        by_doc = {doc["sage_doc_id"]: doc for doc in staged}
        for move in documents:
            document = by_doc.get(move.sage_doc_id)
            if not document:
                continue
            for application in document.get("applications", []):
                applications.append(
                    dict(application, move_id=move.id, side=document["side"])
                )
        return applications

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted: dict) -> list:
        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        journals = {
            journal.default_account_id.id: journal.id
            for journal in ctx.env["account.journal"].search([
                ("type", "=", "bank"),
                ("company_id", "=", ctx.get_config("company_id")),
            ])
        }
        values = []
        for application in extracted["extract_applications"]:
            account_id = accounts.get(application["sage_bank_account"])
            journal_id = journals.get(account_id)
            if not journal_id:
                ctx.report.failure(
                    f"No bank journal for Sage account "
                    f"{application['sage_bank_account']}",
                    source_ref=application["reference"],
                )
                continue
            amount = application["amount"]
            side = application["side"]
            if not amount:
                continue
            # The direction follows the SIGN, not the side. The staged amount
            # is debit-positive, so a negative application credits a control
            # account — money coming in against a receivable, or a payable
            # being increased — and a positive one debits it.
            #
            # Assuming every customer application is a receipt is wrong and
            # quietly so: a returned cheque or a reversed receipt is a
            # positive application against a receivable, and treating it as a
            # receipt subtracts it twice.
            values.append({
                "sage_application_id": application["sage_id"],
                "sage_gl_entry_id": application["sage_gl_entry_id"],
                "move_id": application["move_id"],
                "journal_id": journal_id,
                "date": application["date"],
                "amount": abs(amount),
                "payment_type": "inbound" if amount < 0 else "outbound",
                "partner_type": PAYMENT_SHAPE[side]["partner_type"],
                "control_type": PAYMENT_SHAPE[side]["control"],
                "reference": application["reference"],
                "cheque_id": application["cheque_id"],
            })
        return values

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed: dict) -> None:
        Payment = ctx.env["account.payment"]
        Move = ctx.env["account.move"]
        company_id = ctx.get_config("company_id")
        already = set(Payment.search([
            ("sage_application_id", "!=", 0),
            ("company_id", "=", company_id),
        ]).mapped("sage_application_id"))

        created = skipped = reconciled = 0
        for values in transformed["transform_payments"]:
            if values["sage_application_id"] in already:
                skipped += 1
                continue
            move = Move.browse(values["move_id"])
            memo = values["reference"] or ""
            if values["cheque_id"]:
                memo = f"{memo} (chq {values['cheque_id']})".strip()
            payment = Payment.create({
                "sage_application_id": values["sage_application_id"],
                "journal_id": values["journal_id"],
                "date": values["date"],
                "amount": values["amount"],
                "payment_type": values["payment_type"],
                "partner_type": values["partner_type"],
                "partner_id": move.partner_id.id,
                "memo": memo or False,
                "company_id": company_id,
            })
            payment.action_post()
            # Carried on the move too, so the reversing entry finds every
            # imported line with one search rather than joining to payments.
            payment.move_id.sage_application_id = values[
                "sage_application_id"
            ]
            payment.move_id.sage_gl_entry_id = values["sage_gl_entry_id"]
            created += 1
            ctx.report.success()

            if self._reconcile(payment, move, values["control_type"]):
                reconciled += 1
            else:
                ctx.report.warning(
                    f"Payment {payment.name} did not reconcile against "
                    f"{move.name}",
                    source_ref=values["reference"],
                )

        _logger.info(
            "Sage payments: %s posted, %s already present, %s reconciled.",
            created, skipped, reconciled,
        )

    def _reconcile(self, payment, move, control_type) -> bool:
        """Match the payment against the document it was applied to.

        Reconciling explicitly, rather than letting Odoo guess from the
        amount, because Sage says exactly which document each application
        belongs to — and several documents for one partner can share an
        amount.
        """
        lines = (payment.move_id.line_ids | move.line_ids).filtered(
            lambda line: (
                line.account_id.account_type == control_type
                and not line.reconciled
                and line.parent_state == "posted"
            )
        )
        if len(lines.account_id) != 1 or len(lines) < 2:
            return False
        lines.reconcile()
        return True
