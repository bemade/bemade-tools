import logging

from odoo import api, models
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.payment.reconciliation",
    sap_source="oinv",  # We'll query both OINV and OPCH
    depends_on=[
        "account.move.invoice.post.processor",
        "account.move.bill.importer",
    ],
    allow_multiprocessing=False,
)
class AccountPaymentReconciliation(models.AbstractModel):
    _name = "account.payment.reconciliation"
    _description = (
        "SAP Payment Reconciliation - Create Journal Entries for Paid Invoices/Bills"
    )

    @ETL.extract("oinv")
    def extract_payments(self, ctx: ETLContext):
        """Extract payments from SAP payment tables and pre-load Odoo invoices/bills."""
        _logger.info("Extracting payment data from SAP...")

        # Get incoming payments (customer payments) with invoice allocations
        ctx.cr.execute(
            """
            SELECT 
                p.docentry as payment_docentry,
                p.docnum as payment_docnum,
                p.docdate as payment_date,
                p.doctotal as payment_total,
                p.cashsum,
                p.trsfrsum,
                p.checksum,
                a.invoiceid,
                a.sumapplied,
                'customer' as payment_type
            FROM orct p
            LEFT JOIN rct2 a ON p.docentry = a.docentry
            WHERE a.invoiceid IS NOT NULL
            """
        )
        customer_payments = ctx.cr.dictfetchall()

        # Get outgoing payments (vendor payments) with bill allocations
        ctx.cr.execute(
            """
            SELECT 
                p.docentry as payment_docentry,
                p.docnum as payment_docnum,
                p.docdate as payment_date,
                p.doctotal as payment_total,
                p.cashsum,
                p.trsfrsum,
                p.checksum,
                a.docentry as invoiceid,
                a.sumapplied,
                'vendor' as payment_type
            FROM ovpm p
            LEFT JOIN vpm2 a ON p.docentry = a.docnum
            WHERE a.docentry IS NOT NULL
            """
        )
        vendor_payments = ctx.cr.dictfetchall()

        _logger.info(
            f"Found {len(customer_payments)} customer payment allocations and "
            f"{len(vendor_payments)} vendor payment allocations in SAP"
        )

        # Pre-load all invoices and bills that have payments
        invoice_docentries = [p["invoiceid"] for p in customer_payments]
        bill_docentries = [p["invoiceid"] for p in vendor_payments]

        invoices_dict = {}
        if invoice_docentries:
            invoices = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", invoice_docentries),
                    ("sap_table", "=", "oinv"),
                    ("state", "=", "posted"),
                ]
            )
            invoices_dict = {inv.sap_docentry: inv for inv in invoices}

        bills_dict = {}
        if bill_docentries:
            bills = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", bill_docentries),
                    ("sap_table", "=", "opch"),
                    ("state", "=", "posted"),
                ]
            )
            bills_dict = {bill.sap_docentry: bill for bill in bills}

        _logger.info(
            f"Pre-loaded {len(invoices_dict)} invoices and {len(bills_dict)} bills"
        )

        return {
            "customer_payments": customer_payments,
            "vendor_payments": vendor_payments,
            "invoices_dict": invoices_dict,
            "bills_dict": bills_dict,
        }

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted):
        """Match SAP payments to Odoo invoices/bills and prepare reconciliation data."""
        extract_data = extracted.get("extract_payments", {})
        customer_payments = extract_data.get("customer_payments", [])
        vendor_payments = extract_data.get("vendor_payments", [])
        invoices_dict = extract_data.get("invoices_dict", {})
        bills_dict = extract_data.get("bills_dict", {})

        payment_data = []

        # Process customer payments
        for payment in customer_payments:
            invoice = invoices_dict.get(payment["invoiceid"])
            if invoice:
                payment_data.append(
                    {
                        "move": invoice,
                        "payment_amount": payment["sumapplied"],
                        "payment_date": payment["payment_date"],
                        "payment_ref": f"SAP Payment {payment['payment_docnum']}",
                        "payment_type": "customer",
                    }
                )

        # Process vendor payments
        for payment in vendor_payments:
            bill = bills_dict.get(payment["invoiceid"])
            if bill:
                payment_data.append(
                    {
                        "move": bill,
                        "payment_amount": payment["sumapplied"],
                        "payment_date": payment["payment_date"],
                        "payment_ref": f"SAP Payment {payment['payment_docnum']}",
                        "payment_type": "vendor",
                    }
                )

        _logger.info(f"Prepared {len(payment_data)} payment reconciliations")
        return payment_data

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed):
        """Create simple journal entries to reconcile paid invoices/bills."""
        reconciliation_data = transformed.get("transform_payments", [])

        if not reconciliation_data:
            _logger.info("No payments to reconcile")
            return

        # Get or create a payment journal for SAP reconciliation
        payment_journal = ctx.env["account.journal"].search(
            [
                ("code", "=", "SAPREC"),
                ("type", "=", "general"),
            ],
            limit=1,
        )

        if not payment_journal:
            payment_journal = ctx.env["account.journal"].create(
                {
                    "name": "SAP Payment Reconciliation",
                    "code": "SAPREC",
                    "type": "general",
                }
            )

        # Get bank/cash account for payments (you may need to adjust this)
        bank_account = ctx.env["account.account"].search(
            [
                ("account_type", "=", "asset_cash"),
            ],
            limit=1,
        )

        if not bank_account:
            _logger.warning("No bank account found for payment reconciliation")
            return

        reconciled_count = 0
        for data in reconciliation_data:
            move = data["move"]
            payment_amount = data["payment_amount"]
            payment_type = data["payment_type"]
            payment_date = data["payment_date"]
            payment_ref = data["payment_ref"]

            # Skip if already reconciled
            if move.payment_state in ["paid", "in_payment"]:
                continue

            # Find the receivable/payable line to reconcile
            line_to_reconcile = move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ["asset_receivable", "liability_payable"]
                and not l.reconciled
            )

            if not line_to_reconcile:
                continue

            # Create a simple journal entry for the payment
            payment_vals = {
                "journal_id": payment_journal.id,
                "date": payment_date,
                "ref": payment_ref,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": line_to_reconcile[0].account_id.id,
                            "partner_id": move.partner_id.id,
                            "debit": payment_amount if payment_type == "vendor" else 0,
                            "credit": (
                                payment_amount if payment_type == "customer" else 0
                            ),
                            "name": payment_ref,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "account_id": bank_account.id,
                            "partner_id": move.partner_id.id,
                            "debit": (
                                payment_amount if payment_type == "customer" else 0
                            ),
                            "credit": payment_amount if payment_type == "vendor" else 0,
                            "name": payment_ref,
                        },
                    ),
                ],
            }

            payment_move = ctx.env["account.move"].create(payment_vals)
            payment_move.action_post()

            # Reconcile the payment with the invoice/bill
            payment_line = payment_move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ["asset_receivable", "liability_payable"]
            )

            if payment_line and line_to_reconcile:
                (line_to_reconcile + payment_line).reconcile()
                reconciled_count += 1

        _logger.info(f"Reconciled {reconciled_count} payments")
