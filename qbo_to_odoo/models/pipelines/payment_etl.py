"""QuickBooks Online Payment ETL Pipeline

This module handles the migration of Payments and BillPayments from QBO to Odoo
using the ETL framework. Payments are created as account.payment objects, which
automatically generate journal entries when posted. Reconciliation with the
original invoice/bill is attempted as a second pass.
"""

import logging
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData, post_lock

from .exchange_rate_helper import ExchangeRateEnsurer
from .extractor import QBOExtractor
from .move_builder import QBOMoveBuilder
from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.payment",
    importer_name="qbo.payment.importer",
    sap_source="Payment",
    depends_on=[
        "qbo.invoice.importer",
        "qbo.bill.importer",
        "qbo.account.importer",
        "qbo.customer.importer",
        "qbo.vendor.importer",
        "qbo.credit.memo.importer",
        "qbo.vendor.credit.importer",
    ],
    chunk_size=50,
    multiprocessing_threshold=50,
)
class QboPaymentImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Payments as account.payment objects."""

    _name = "qbo.payment.importer"
    _description = "QBO Payment Importer"

    @ETL.extract("Payment")
    def extract_payments(self, ctx: ETLContext) -> ChunkableData:
        """Extract payments from QBO API and preload all required data."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO payment IDs from account.payment
        existing_payment_ids = extractor.existing_qbo_ids(
            "account_payment", "qbo_payment_id"
        )
        existing_bill_payment_ids = extractor.existing_qbo_ids(
            "account_payment", "qbo_bill_payment_id"
        )

        _logger.info(
            f"Found {len(existing_payment_ids)} existing customer payments, "
            f"{len(existing_bill_payment_ids)} existing bill payments in Odoo"
        )

        # Fetch customer payments from QBO
        payments = api_client.query_all(entity="Payment", order_by="Id")
        new_payments = [
            {"type": "customer", "data": p}
            for p in payments
            if str(p.get("Id")) not in existing_payment_ids
        ]

        # Fetch bill payments from QBO
        bill_payments = api_client.query_all(entity="BillPayment", order_by="Id")
        new_bill_payments = [
            {"type": "vendor", "data": bp}
            for bp in bill_payments
            if str(bp.get("Id")) not in existing_bill_payment_ids
        ]

        _logger.info(
            f"Extracted {len(payments)} customer payments, {len(new_payments)} new; "
            f"{len(bill_payments)} bill payments, {len(new_bill_payments)} new"
        )

        # Ensure exchange rates exist for all foreign-currency payments
        all_raw_records = [p["data"] for p in new_payments + new_bill_payments]
        ExchangeRateEnsurer(ctx.env).ensure_rates(all_raw_records)

        # Preload maps for transform
        extractor.preload("account", "customer", "vendor", "currency")
        extractor.preload_account_journal_map()
        extractor.preload_journals("general")
        extractor.preload_undeposited_funds()

        # Journal -> default bank account for direct-to-bank outstanding account
        ctx.env.cr.execute(
            "SELECT id, default_account_id FROM account_journal "
            "WHERE default_account_id IS NOT NULL AND company_id = %s",
            [extractor._company_id],
        )
        extractor.extra["journal_bank_account_map"] = {
            row[0]: row[1] for row in ctx.env.cr.fetchall()
        }

        # Pipeline-specific: invoice/bill/credit memo maps for reconciliation
        extractor.extra["invoice_map"] = extractor.qbo_id_map(
            "account_move", "qbo_invoice_id", where="state = 'posted'"
        )
        extractor.extra["bill_map"] = extractor.qbo_id_map(
            "account_move", "qbo_bill_id", where="state = 'posted'"
        )
        extractor.extra["credit_memo_map"] = extractor.qbo_id_map(
            "account_move", "qbo_credit_memo_id", where="state = 'posted'"
        )
        extractor.extra["vendor_credit_map"] = extractor.qbo_id_map(
            "account_move", "qbo_vendor_credit_id", where="state = 'posted'"
        )

        # Pre-fetch receivable/payable accounts for destination_account_id
        extractor.extra["invoice_receivable_map"] = extractor.invoice_receivable_map()
        extractor.extra["bill_payable_map"] = extractor.bill_payable_map()
        extractor.extra["partner_receivable_map"] = extractor.partner_receivable_map()
        extractor.extra["partner_payable_map"] = extractor.partner_payable_map()

        all_payments = new_payments + new_bill_payments

        return ChunkableData(
            records=all_payments,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO payments into account.payment values."""
        data = extracted.get("extract_payments")
        if not data:
            return []
        all_payments = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])
        invoice_map = builder.get_extra("invoice_map") or {}
        bill_map = builder.get_extra("bill_map") or {}
        credit_memo_map = builder.get_extra("credit_memo_map") or {}
        vendor_credit_map = builder.get_extra("vendor_credit_map") or {}
        journal_bank_account_map = builder.get_extra("journal_bank_account_map") or {}

        payment_data = []
        credit_applications = []
        skipped = 0

        for pmt in all_payments:
            pmt_type = pmt["type"]
            pmt_data = pmt["data"]
            total_amt = float(pmt_data.get("TotalAmt", 0) or 0)

            # Zero-amount payments are credit/debit note applications:
            # Lines link CreditMemos→Invoices or VendorCredits→Bills
            # without any cash movement.
            if total_amt <= 0:
                if pmt_type == "customer":
                    apps = self._transform_credit_application(
                        pmt_data, invoice_map, credit_memo_map,
                    )
                else:
                    apps = self._transform_vendor_credit_application(
                        pmt_data, bill_map, vendor_credit_map,
                    )
                credit_applications.extend(apps)
                continue

            if pmt_type == "customer":
                result = self._transform_customer_payment(
                    pmt_data, builder, invoice_map, journal_bank_account_map,
                )
            else:
                result = self._transform_bill_payment(
                    pmt_data, builder, bill_map, journal_bank_account_map,
                )

            if result:
                payment_data.append(result)
            else:
                skipped += 1

        with_links = sum(1 for p in payment_data if p.get("linked_moves"))
        _logger.info(
            f"Transformed {len(payment_data)} payments, skipped {skipped}, "
            f"{with_links} linked to invoices/bills; "
            f"{len(credit_applications)} credit/debit note applications"
        )
        return {
            "payments": payment_data,
            "credit_applications": credit_applications,
        }

    def _transform_customer_payment(
        self,
        payment: Dict,
        builder: QBOMoveBuilder,
        invoice_map: Dict,
        journal_bank_account_map: Dict,
    ) -> Optional[Dict]:
        """Transform a customer payment into account.payment vals."""
        partner_id = builder.resolve_partner(payment, "customer")
        if not partner_id:
            _logger.warning(
                f"Customer not found for QBO ID "
                f"{payment.get('CustomerRef', {}).get('value')} "
                f"in payment {payment.get('Id')}"
            )
            return None

        txn_date = payment.get("TxnDate")
        total_amt = float(payment.get("TotalAmt", 0) or 0)
        if total_amt <= 0:
            _logger.debug(
                f"Customer payment {payment.get('Id')} has TotalAmt={total_amt}, skipping"
            )
            return None

        qbo_payment_id = int(payment.get("Id", 0))
        payment_ref = payment.get("PaymentRefNum", "") or f"QBO-{qbo_payment_id}"

        # Get bank journal
        result = self._get_bank_journal(payment, builder)
        if not result:
            _logger.warning(
                f"No valid bank journal found for payment {qbo_payment_id}, skipping"
            )
            return None
        journal_id = result

        # Get receivable account: prefer linked invoice, fall back to partner default
        recv_account_id = None
        linked_moves = []  # (odoo_move_id, qbo_line_amount)
        invoice_recv_map = builder.get_extra("invoice_receivable_map") or {}
        partner_recv_map = builder.get_extra("partner_receivable_map") or {}

        for line in payment.get("Line", []):
            line_amount = float(line.get("Amount", 0) or 0)
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                if linked.get("TxnType") == "Invoice" and txn_id in invoice_map:
                    linked_moves.append((invoice_map[txn_id], line_amount))
                    if not recv_account_id:
                        recv_account_id = invoice_recv_map.get(txn_id)

        if not recv_account_id:
            recv_account_id = partner_recv_map.get(partner_id)

        if not recv_account_id:
            _logger.warning(
                f"No receivable account for payment {qbo_payment_id}, skipping"
            )
            return None

        # Resolve currency
        currency_id, is_foreign, _exchange_rate = builder.resolve_currency(payment)

        # Use journal's bank account as outstanding account (direct-to-bank,
        # no transit account) so the JE is: DR Bank / CR Receivable.
        outstanding_account_id = journal_bank_account_map.get(journal_id)

        payment_vals = {
            "date": txn_date,
            "journal_id": journal_id,
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner_id,
            "amount": total_amt,
            "memo": payment_ref,
            "payment_reference": payment_ref,
            "qbo_payment_id": qbo_payment_id,
            "destination_account_id": recv_account_id,
            "outstanding_account_id": outstanding_account_id,
        }
        if is_foreign:
            payment_vals["currency_id"] = currency_id

        return {
            "payment_vals": payment_vals,
            "linked_moves": linked_moves,
            "is_customer": True,
        }

    @staticmethod
    def _transform_credit_application(
        payment: Dict,
        invoice_map: Dict,
        credit_memo_map: Dict,
    ) -> List[Dict]:
        """Transform a zero-amount payment (credit memo application).

        In QBO, applying credit memos to invoices creates a Payment with
        TotalAmt=0.  The Lines link CreditMemos (credit side) to Invoices
        (debit side).  In Odoo we don't create an account.payment; we just
        reconcile the credit note's AR line against the invoice's AR line.

        Returns a list of dicts with ``invoice_move_id`` and
        ``credit_memo_move_id`` for each pair to reconcile.
        """
        qbo_id = payment.get("Id")
        pairs = []
        invoice_ids = []
        credit_memo_ids = []

        for line in payment.get("Line", []):
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType")
                if txn_type == "Invoice" and txn_id in invoice_map:
                    invoice_ids.append(invoice_map[txn_id])
                elif txn_type == "CreditMemo" and txn_id in credit_memo_map:
                    credit_memo_ids.append(credit_memo_map[txn_id])

        if not invoice_ids or not credit_memo_ids:
            if invoice_ids or credit_memo_ids:
                _logger.debug(
                    f"Credit application QBO#{qbo_id}: only partial links "
                    f"(invoices={len(invoice_ids)}, memos={len(credit_memo_ids)})"
                )
            return []

        # Each invoice gets paired with all credit memos in this application.
        for inv_id in invoice_ids:
            for cm_id in credit_memo_ids:
                pairs.append({
                    "invoice_move_id": inv_id,
                    "credit_memo_move_id": cm_id,
                    "qbo_payment_id": qbo_id,
                })

        return pairs

    @staticmethod
    def _transform_vendor_credit_application(
        bill_payment: Dict,
        bill_map: Dict,
        vendor_credit_map: Dict,
    ) -> List[Dict]:
        """Transform a zero-amount bill payment (vendor credit application).

        In QBO, applying vendor credits to bills creates a BillPayment with
        TotalAmt=0.  The Lines link VendorCredits to Bills.  In Odoo we
        reconcile the vendor credit's AP line against the bill's AP line.

        Returns a list of dicts with ``invoice_move_id`` (the bill) and
        ``credit_memo_move_id`` (the vendor credit) for each pair.
        """
        qbo_id = bill_payment.get("Id")
        bill_ids = []
        vendor_credit_ids = []

        for line in bill_payment.get("Line", []):
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType")
                if txn_type == "Bill" and txn_id in bill_map:
                    bill_ids.append(bill_map[txn_id])
                elif txn_type == "VendorCredit" and txn_id in vendor_credit_map:
                    vendor_credit_ids.append(vendor_credit_map[txn_id])

        if not bill_ids or not vendor_credit_ids:
            if bill_ids or vendor_credit_ids:
                _logger.debug(
                    f"Vendor credit application QBO-BP#{qbo_id}: only partial "
                    f"links (bills={len(bill_ids)}, "
                    f"credits={len(vendor_credit_ids)})"
                )
            return []

        pairs = []
        for bill_id in bill_ids:
            for vc_id in vendor_credit_ids:
                pairs.append({
                    "invoice_move_id": bill_id,
                    "credit_memo_move_id": vc_id,
                    "qbo_payment_id": f"BP-{qbo_id}",
                })
        return pairs

    def _get_bank_journal(
        self, payment: Dict, builder: QBOMoveBuilder
    ) -> Optional[int]:
        """Resolve the bank/cash journal ID from QBO payment data."""
        # Try DepositToAccountRef (customer payments)
        account_ref = payment.get("DepositToAccountRef", {})

        # Try CheckPayment.BankAccountRef (bill payments by cheque)
        if not account_ref or not account_ref.get("value"):
            check_payment = payment.get("CheckPayment", {})
            if check_payment:
                account_ref = check_payment.get("BankAccountRef", {})

        # Try CreditCardPayment.CCAccountRef (bill payments by credit card)
        if not account_ref or not account_ref.get("value"):
            cc_payment = payment.get("CreditCardPayment", {})
            if cc_payment:
                account_ref = cc_payment.get("CCAccountRef", {})

        if not account_ref or not account_ref.get("value"):
            # Fall back to Undeposited Funds account → its journal
            account_id = builder.undeposited_funds_id
            if not account_id:
                _logger.warning(
                    f"No account reference found in payment {payment.get('Id')} "
                    f"and no 'Undeposited Funds' account in Odoo"
                )
                return None
        else:
            qbo_account_id = account_ref.get("value")
            try:
                account_id = builder.account_map.get(int(qbo_account_id))
            except (ValueError, TypeError):
                account_id = None
            if not account_id:
                _logger.warning(
                    f"Account with QBO ID {qbo_account_id} not found in Odoo "
                    f"for payment {payment.get('Id')}"
                )
                return None

        # Never fall back to general journal — payments need bank/cash journals
        # with payment method lines.
        journal_id = builder.get_journal_id_for_account(
            account_id, fallback_type=None
        )
        if not journal_id:
            _logger.warning(
                f"No bank/cash journal found for account {account_id} "
                f"in payment {payment.get('Id')}"
            )
            return None

        return journal_id

    def _transform_bill_payment(
        self,
        bp: Dict,
        builder: QBOMoveBuilder,
        bill_map: Dict,
        journal_bank_account_map: Dict,
    ) -> Optional[Dict]:
        """Transform a bill payment into account.payment vals."""
        partner_id = builder.resolve_partner(bp, "vendor")
        if not partner_id:
            _logger.warning(
                f"Vendor not found for QBO ID "
                f"{bp.get('VendorRef', {}).get('value')} "
                f"in bill payment {bp.get('Id')}"
            )
            return None

        txn_date = bp.get("TxnDate")
        total_amt = float(bp.get("TotalAmt", 0) or 0)
        if total_amt <= 0:
            _logger.debug(
                f"Bill payment {bp.get('Id')} has TotalAmt={total_amt}, skipping"
            )
            return None

        qbo_bill_payment_id = int(bp.get("Id", 0))
        payment_ref = bp.get("DocNumber", "") or f"QBO-BP-{qbo_bill_payment_id}"

        result = self._get_bank_journal(bp, builder)
        if not result:
            _logger.warning(
                f"No valid bank journal for bill payment {qbo_bill_payment_id}, skipping"
            )
            return None
        journal_id = result

        # Get payable account: prefer linked bill, fall back to partner default
        payable_account_id = None
        linked_moves = []  # (odoo_move_id, qbo_line_amount)
        bill_payable_map = builder.get_extra("bill_payable_map") or {}
        partner_payable_map = builder.get_extra("partner_payable_map") or {}

        for line in bp.get("Line", []):
            line_amount = float(line.get("Amount", 0) or 0)
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                if linked.get("TxnType") == "Bill" and txn_id in bill_map:
                    linked_moves.append((bill_map[txn_id], line_amount))
                    if not payable_account_id:
                        payable_account_id = bill_payable_map.get(txn_id)

        if not payable_account_id:
            payable_account_id = partner_payable_map.get(partner_id)

        if not payable_account_id:
            _logger.warning(
                f"No payable account for bill payment {qbo_bill_payment_id}, skipping"
            )
            return None

        # Resolve currency
        currency_id, is_foreign, _exchange_rate = builder.resolve_currency(bp)

        outstanding_account_id = journal_bank_account_map.get(journal_id)

        payment_vals = {
            "date": txn_date,
            "journal_id": journal_id,
            "payment_type": "outbound",
            "partner_type": "supplier",
            "partner_id": partner_id,
            "amount": total_amt,
            "memo": payment_ref,
            "payment_reference": payment_ref,
            "qbo_bill_payment_id": qbo_bill_payment_id,
            "destination_account_id": payable_account_id,
            "outstanding_account_id": outstanding_account_id,
        }
        if is_foreign:
            payment_vals["currency_id"] = currency_id

        return {
            "payment_vals": payment_vals,
            "linked_moves": linked_moves,
            "is_customer": False,
        }

    @staticmethod
    def _ensure_payment_method_lines(ctx: ETLContext, payment_data: List[Dict]):
        """Ensure every target journal has manual inbound/outbound method lines.

        Bank/cash journals normally get these on creation, but journals created
        by the ETL (or via raw SQL) may be missing them.  Without method lines
        the ``account.payment`` constraint ``_check_payment_method_line_id``
        raises a ``ValidationError``.
        """
        journal_ids = {
            pmt["payment_vals"]["journal_id"]
            for pmt in payment_data
            if pmt["payment_vals"].get("journal_id")
        }
        if not journal_ids:
            return

        journals = ctx.env["account.journal"].browse(list(journal_ids))
        manual_in = ctx.env.ref(
            "account.account_payment_method_manual_in",
            raise_if_not_found=False,
        )
        manual_out = ctx.env.ref(
            "account.account_payment_method_manual_out",
            raise_if_not_found=False,
        )
        MethodLine = ctx.env["account.payment.method.line"]
        for journal in journals:
            if manual_in and not journal.inbound_payment_method_line_ids.filtered(
                lambda l, m=manual_in: l.payment_method_id == m
            ):
                MethodLine.create({
                    "payment_method_id": manual_in.id,
                    "journal_id": journal.id,
                })
                _logger.info(
                    f"Added manual inbound payment method to journal "
                    f"{journal.name} (id={journal.id})"
                )
            if manual_out and not journal.outbound_payment_method_line_ids.filtered(
                lambda l, m=manual_out: l.payment_method_id == m
            ):
                MethodLine.create({
                    "payment_method_id": manual_out.id,
                    "journal_id": journal.id,
                })
                _logger.info(
                    f"Added manual outbound payment method to journal "
                    f"{journal.name} (id={journal.id})"
                )

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create account.payment records and attempt reconciliation."""
        transform_result = transformed.get("transform_payments", {})
        # Backwards compat: if transform returned a plain list (old code path)
        if isinstance(transform_result, list):
            payment_data = transform_result
            credit_applications = []
        else:
            payment_data = transform_result.get("payments", [])
            credit_applications = transform_result.get("credit_applications", [])

        if not payment_data and not credit_applications:
            _logger.info("No payments to process")
            return

        ctx.env.invalidate_all()

        # Phase 0: Ensure all target journals have manual payment method lines.
        # Journals of type bank/cash/credit normally get these on creation, but
        # they may be missing if the journal was created outside the normal flow
        # (e.g. by the ETL account pipeline).
        self._ensure_payment_method_lines(ctx, payment_data)

        # Phase 1: Create all payments (no lock needed)
        # outstanding_account_id is set to the journal's bank account in the
        # transform (direct-to-bank), so the JE goes straight to the bank
        # account without a transit/outstanding account.
        payments = []  # (payment_record, linked_moves, is_customer)
        for pmt in payment_data:
            pmt_vals = pmt["payment_vals"]
            qbo_id = (
                pmt_vals.get("qbo_payment_id")
                or pmt_vals.get("qbo_bill_payment_id")
                or "?"
            )
            with ctx.skippable(f"create payment QBO#{qbo_id}"):
                outstanding_id = pmt_vals.pop("outstanding_account_id", None)
                payment = ctx.env["account.payment"].create(pmt_vals)
                if outstanding_id:
                    payment.outstanding_account_id = outstanding_id
                payments.append(
                    (payment, pmt["linked_moves"], pmt["is_customer"])
                )

        _logger.info(f"Created {len(payments)} payments")

        # Phase 2: Post payments, grouped by journal to minimize lock acquisitions
        by_journal = {}
        for payment, linked_moves, is_customer in payments:
            jid = payment.journal_id.id
            by_journal.setdefault(jid, []).append(
                (payment, linked_moves, is_customer)
            )

        posted = 0
        reconciliation_queue = []
        for journal_id, group in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for payment, linked_moves, is_customer in group:
                    qbo_id = payment.qbo_payment_id or payment.qbo_bill_payment_id or "?"
                    with ctx.skippable(f"post payment QBO#{qbo_id}"):
                        payment.action_post()
                        posted += 1
                        if linked_moves:
                            reconciliation_queue.append(
                                (payment, linked_moves, is_customer)
                            )

        _logger.info(f"Posted {posted} payments")

        # Phase 3: Reconcile, grouped by account to minimize lock acquisitions.
        # linked_moves is a list of (odoo_move_id, qbo_line_amount) tuples.
        # The QBO line amount tells us exactly how much to apply to each invoice/bill.
        # When both the payment's remaining balance and the invoice's open balance
        # exceed the QBO amount, we create account.partial.reconcile directly to
        # avoid greedily over-applying to the first invoice and leaving later ones
        # with no credit.
        with_links = sum(1 for _, lm, _ in reconciliation_queue if lm)
        _logger.info(
            f"Reconciliation queue: {len(reconciliation_queue)} payments, "
            f"{with_links} with linked invoices/bills"
        )

        by_account = {}  # account_id -> [(payment, payment_line, linked_moves)]
        no_move = 0
        no_line = 0
        for payment, linked_moves, is_customer in reconciliation_queue:
            if not payment.move_id:
                no_move += 1
                _logger.warning(
                    f"Payment {payment.name} has no move_id after posting"
                )
                continue
            account_type = "asset_receivable" if is_customer else "liability_payable"
            payment_line = payment.move_id.line_ids.filtered(
                lambda l, at=account_type: l.account_id.account_type == at
            )
            if not payment_line:
                no_line += 1
                line_types = [
                    (l.account_id.name, l.account_id.account_type)
                    for l in payment.move_id.line_ids
                ]
                _logger.warning(
                    f"Payment {payment.name}: no {account_type} line found. "
                    f"Move lines: {line_types}"
                )
                continue
            pay_account_id = payment_line[0].account_id.id
            by_account.setdefault(pay_account_id, []).append(
                (payment, payment_line, linked_moves)
            )

        if no_move or no_line:
            _logger.warning(
                f"Reconciliation prep: {no_move} without move_id, "
                f"{no_line} without matching line"
            )

        reconciled = 0
        for account_id, group in sorted(by_account.items()):
            with post_lock(ctx.env.cr, account_id):
                for payment, payment_line, linked_moves in group:
                    for linked_id, qbo_amount in linked_moves:
                        with ctx.skippable(
                            f"reconcile {payment.name} <-> move#{linked_id}"
                        ):
                            pay_line_open = payment_line.filtered(
                                lambda l: not l.reconciled
                            )
                            if not pay_line_open:
                                break
                            original_move = ctx.env["account.move"].browse(linked_id)
                            inv_line = original_move.line_ids.filtered(
                                lambda l, aid=account_id: (
                                    l.account_id.id == aid and not l.reconciled
                                )
                            )
                            if not inv_line:
                                _logger.debug(
                                    f"No unreconciled {account_id} line on "
                                    f"move#{linked_id} for {payment.name}"
                                )
                                continue
                            pay_line = pay_line_open[0]
                            inv_line = inv_line[0]
                            # Residual amounts in the payment's currency
                            # (amount_residual_currency for multi-currency,
                            # amount_residual for same-currency)
                            pay_curr = pay_line.currency_id
                            inv_curr = inv_line.currency_id
                            pay_open = abs(
                                pay_line.amount_residual_currency
                                if pay_curr and pay_curr != pay_line.company_currency_id
                                else pay_line.amount_residual
                            )
                            inv_open = abs(
                                inv_line.amount_residual_currency
                                if inv_curr and inv_curr != inv_line.company_currency_id
                                else inv_line.amount_residual
                            )
                            # If both sides have more open than the QBO amount,
                            # we must limit to avoid over-applying.
                            tol = 0.01
                            if pay_open > qbo_amount + tol and inv_open > qbo_amount + tol:
                                # Compute the CAD equivalent using the payment line's rate
                                pay_cad = abs(pay_line.amount_residual)
                                pay_foreign = abs(pay_line.amount_residual_currency) if pay_curr else pay_cad
                                rate = pay_cad / pay_foreign if pay_foreign else 1.0
                                cad_amount = qbo_amount * rate
                                ctx.env["account.partial.reconcile"].create({
                                    "debit_move_id": inv_line.id,
                                    "credit_move_id": pay_line.id,
                                    "amount": cad_amount,
                                    "debit_amount_currency": qbo_amount,
                                    "credit_amount_currency": qbo_amount,
                                    "company_id": inv_line.company_id.id,
                                })
                                # Force recompute of residuals so the next
                                # iteration sees the updated open balances.
                                (inv_line + pay_line).invalidate_recordset(
                                    ["amount_residual", "amount_residual_currency", "reconciled"]
                                )
                            else:
                                (inv_line + pay_line).reconcile()
                            reconciled += 1

        _logger.info(f"Reconciled {reconciled} payment/invoice pairs")

        # Phase 4: Apply credit/debit notes to invoices/bills.
        # These come from zero-amount QBO Payments (CreditMemos → Invoices)
        # and zero-amount BillPayments (VendorCredits → Bills).
        # We reconcile the credit note's receivable/payable line against the
        # invoice/bill's receivable/payable line.
        if not credit_applications:
            return

        _logger.info(
            f"Processing {len(credit_applications)} credit/debit note applications"
        )
        applied = 0
        for app in credit_applications:
            qbo_id = app["qbo_payment_id"]
            inv_id = app["invoice_move_id"]
            cm_id = app["credit_memo_move_id"]
            with ctx.skippable(
                f"credit apply QBO#{qbo_id}: move#{inv_id} <-> move#{cm_id}"
            ):
                invoice = ctx.env["account.move"].browse(inv_id)
                credit_memo = ctx.env["account.move"].browse(cm_id)
                # Determine account type from the invoice/bill move type
                if invoice.move_type in ("out_invoice", "out_refund"):
                    account_type = "asset_receivable"
                else:
                    account_type = "liability_payable"
                inv_line = invoice.line_ids.filtered(
                    lambda l, at=account_type: (
                        l.account_id.account_type == at
                        and not l.reconciled
                    )
                )
                cm_line = credit_memo.line_ids.filtered(
                    lambda l, at=account_type: (
                        l.account_id.account_type == at
                        and not l.reconciled
                    )
                )
                if not inv_line or not cm_line:
                    _logger.debug(
                        f"Credit apply QBO#{qbo_id}: no unreconciled "
                        f"{account_type} lines "
                        f"(inv={len(inv_line)}, cm={len(cm_line)})"
                    )
                    continue
                (inv_line + cm_line).reconcile()
                applied += 1

        _logger.info(f"Applied {applied} credit/debit note applications")
