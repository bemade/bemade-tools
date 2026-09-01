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

from odoo.addons.sage50_to_odoo import tools

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
                "sage_gl_entry_ref": application["sage_gl_entry_ref"],
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

    def _wholly_ours(self, ctx: ETLContext, values: list) -> list:
        """Drop payments whose Sage receipt also settled other invoices.

        One receipt settles many invoices, and Sage records it as ONE GL
        entry. Only the slice applied to a document we imported can become
        an `account.payment` — the rest settled invoices that closed long
        ago and are not in Odoo as documents at all.

        That matters because the general-ledger replay skips the entry
        behind any payment we create. Skip an entry worth 64,837.65 and
        replace it with a payment worth 245.35 and the ledger loses the
        difference: the bank comes up short and the receivable stays high by
        the same amount, which is exactly the shape of a mirror-image pair
        in the per-account check.

        So a receipt is only taken as a payment when the applications we
        hold account for the whole of it. Otherwise the payments are
        dropped, nothing carries that entry's reference, and the replay
        posts the receipt in full — which is where it belongs, since most of
        what it settled is history rather than an open document.
        """
        if not ctx.env["sage.opening.balance.importer"].history_start(ctx):
            # Only the general-ledger replay makes a partial payment a
            # problem: it skips the whole entry behind any payment we
            # create. With no replay, nothing else will ever post that
            # receipt, so dropping it loses the money outright and leaves
            # the partner-less control balance short by the difference —
            # which is the check a balances-only take-on is verified by.
            return values

        by_entry = {}
        for values_row in values:
            by_entry.setdefault(
                values_row["sage_gl_entry_ref"], []
            ).append(values_row)

        kept = []
        for entry_ref, rows in by_entry.items():
            if not entry_ref:
                # No GL entry of its own — an application settled by a credit
                # note rather than by money. Nothing is excluded on its
                # behalf, so there is nothing to reconcile against.
                kept.extend(rows)
                continue
            generation, _, entry_id = entry_ref.partition(":")
            control = tools.query(
                ctx.cr,
                f"""select round(sum(l.dAmount), 2) as amount
                      from {generation} j
                      join {tools.LINES_FOR[generation]} l
                        on l.lJEntId = j.lId
                     where j.lId = %s and l.lAcctId in %s""",
                (int(entry_id), tuple(self._control_accounts(ctx))),
            )[0]["amount"] or 0.0
            ours = round(sum(row["amount"] for row in rows), 2)
            if abs(abs(control) - ours) < 0.02:
                kept.extend(rows)
            else:
                ctx.report.warning(
                    f"Sage entry {entry_ref} settles {abs(control):,.2f} but "
                    f"only {ours:,.2f} of it belongs to an imported "
                    f"document. Left to the general-ledger replay rather "
                    f"than split into a partial payment.",
                    source_ref=entry_ref,
                )
        return kept

    def _control_accounts(self, ctx: ETLContext) -> list:
        """The Sage receivable and payable control account numbers."""
        linked = tools.linked_accounts(ctx.cr)
        return [
            account for account in
            (linked.get("lAcNaccRec"), linked.get("lAcNaccPay"))
            if account
        ]

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
        for values in self._wholly_ours(ctx, transformed["transform_payments"]):
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
            payment.move_id.sage_gl_entry_ref = values["sage_gl_entry_ref"]
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
