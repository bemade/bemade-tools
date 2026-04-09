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
from .move_posting_helpers import reconcile_at_amount
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
        extractor.extra["journal_entry_map"] = extractor.qbo_id_map(
            "account_move", "qbo_journal_entry_id", where="state = 'posted'"
        )
        extractor.extra["expense_map"] = extractor.qbo_id_map(
            "account_move", "qbo_expense_id", where="state = 'posted'"
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
        journal_entry_map = builder.get_extra("journal_entry_map") or {}
        expense_map = builder.get_extra("expense_map") or {}
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
                        journal_entry_map,
                    )
                else:
                    apps = self._transform_vendor_credit_application(
                        pmt_data, bill_map, vendor_credit_map,
                        journal_entry_map, expense_map,
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
                # Collect embedded credit/vendor credit applications
                # from regular (non-zero) payments
                credit_applications.extend(
                    result.get("embedded_credit_apps", [])
                )
            else:
                skipped += 1

        with_links = sum(1 for p in payment_data if p.get("linked_moves"))
        embedded = sum(
            len(p.get("embedded_credit_apps", []))
            for p in payment_data
        )
        _logger.info(
            f"Transformed {len(payment_data)} payments, skipped {skipped}, "
            f"{with_links} linked to invoices/bills; "
            f"{len(credit_applications)} credit/debit note applications "
            f"({embedded} embedded on regular payments)"
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

        # Also collect embedded credit memo applications on this payment.
        # QBO can apply CMs as lines on a regular (non-zero) payment.
        credit_memo_map = builder.get_extra("credit_memo_map") or {}
        journal_entry_map = builder.get_extra("journal_entry_map") or {}
        embedded_cm_links = []  # (odoo_cm_move_id, qbo_amount)

        for line in payment.get("Line", []):
            line_amount = float(line.get("Amount", 0) or 0)
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType")
                if txn_type == "Invoice" and txn_id in invoice_map:
                    linked_moves.append((invoice_map[txn_id], line_amount))
                    if not recv_account_id:
                        recv_account_id = invoice_recv_map.get(txn_id)
                elif txn_type == "Invoice":
                    _logger.warning(
                        "Payment %s: Invoice %s not found in Odoo",
                        qbo_payment_id, txn_id,
                    )
                elif txn_type == "CreditMemo" and txn_id in credit_memo_map:
                    embedded_cm_links.append(
                        (credit_memo_map[txn_id], line_amount)
                    )
                elif txn_type == "CreditMemo":
                    _logger.warning(
                        "Payment %s: CreditMemo %s not found in Odoo",
                        qbo_payment_id, txn_id,
                    )
                elif txn_type == "JournalEntry" and txn_id in journal_entry_map:
                    embedded_cm_links.append(
                        (journal_entry_map[txn_id], line_amount)
                    )

        if not recv_account_id:
            recv_account_id = partner_recv_map.get(partner_id)

        if not recv_account_id:
            _logger.warning(
                f"No receivable account for payment {qbo_payment_id}, skipping"
            )
            return None

        # Resolve currency
        currency_id, is_foreign, exchange_rate = builder.resolve_currency(payment)

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

        # Build credit application pairs for embedded CMs
        embedded_apps = []
        if embedded_cm_links and linked_moves:
            for cm_id, cm_amount in embedded_cm_links:
                for inv_id, _inv_amount in linked_moves:
                    embedded_apps.append({
                        "invoice_move_id": inv_id,
                        "credit_memo_move_id": cm_id,
                        "amount": cm_amount,
                        "qbo_payment_id": qbo_payment_id,
                    })

        return {
            "payment_vals": payment_vals,
            "linked_moves": linked_moves,
            "is_customer": True,
            "embedded_credit_apps": embedded_apps,
            "currency_code": (
                payment.get("CurrencyRef", {}).get("value")
                if is_foreign else None
            ),
            "exchange_rate": exchange_rate if is_foreign else None,
        }

    @staticmethod
    def _transform_credit_application(
        payment: Dict,
        invoice_map: Dict,
        credit_memo_map: Dict,
        journal_entry_map: Dict,
    ) -> List[Dict]:
        """Transform a zero-amount payment (credit memo application).

        In QBO, applying credit memos to invoices creates a Payment with
        TotalAmt=0.  Each Line carries an Amount and a LinkedTxn pointing
        to either an Invoice, CreditMemo, or JournalEntry.

        We pair each credit line with each invoice line, carrying the QBO
        line Amount so the load phase can create exact partial reconciles.

        Returns a list of dicts with ``invoice_move_id``,
        ``credit_memo_move_id``, ``amount``, and ``qbo_payment_id``.
        """
        qbo_id = payment.get("Id")
        invoices = []   # (odoo_move_id, qbo_amount)
        credits = []    # (odoo_move_id, qbo_amount)

        for line in payment.get("Line", []):
            amount = float(line.get("Amount", 0) or 0)
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType")
                if txn_type == "Invoice" and txn_id in invoice_map:
                    invoices.append((invoice_map[txn_id], amount))
                elif txn_type == "CreditMemo" and txn_id in credit_memo_map:
                    credits.append((credit_memo_map[txn_id], amount))
                elif txn_type == "JournalEntry" and txn_id in journal_entry_map:
                    credits.append((journal_entry_map[txn_id], amount))

        if not invoices or not credits:
            if invoices or credits:
                _logger.debug(
                    f"Credit application QBO#{qbo_id}: only partial links "
                    f"(invoices={len(invoices)}, credits={len(credits)})"
                )
            return []

        # Pair credits to invoices using the credit's QBO amount.
        # Each credit is applied for its stated amount.
        pairs = []
        for credit_id, credit_amount in credits:
            for inv_id, _inv_amount in invoices:
                pairs.append({
                    "invoice_move_id": inv_id,
                    "credit_memo_move_id": credit_id,
                    "amount": credit_amount,
                    "qbo_payment_id": qbo_id,
                })

        return pairs

    @staticmethod
    def _transform_vendor_credit_application(
        bill_payment: Dict,
        bill_map: Dict,
        vendor_credit_map: Dict,
        journal_entry_map: Dict,
        expense_map: Optional[Dict] = None,
    ) -> List[Dict]:
        """Transform a zero-amount bill payment (vendor credit application).

        In QBO, applying vendor credits to bills creates a BillPayment with
        TotalAmt=0.  Each Line carries an Amount and a LinkedTxn pointing
        to a Bill, VendorCredit, JournalEntry, or Purchase (expense used
        as vendor credit).

        Returns a list of dicts with ``invoice_move_id`` (the bill),
        ``credit_memo_move_id`` (the vendor credit / JE / expense),
        ``amount``, and ``qbo_payment_id``.
        """
        expense_map = expense_map or {}
        qbo_id = bill_payment.get("Id")
        bills = []    # (odoo_move_id, qbo_amount)
        credits = []  # (odoo_move_id, qbo_amount)

        for line in bill_payment.get("Line", []):
            amount = float(line.get("Amount", 0) or 0)
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType")
                if txn_type == "Bill" and txn_id in bill_map:
                    bills.append((bill_map[txn_id], amount))
                elif txn_type == "VendorCredit" and txn_id in vendor_credit_map:
                    credits.append((vendor_credit_map[txn_id], amount))
                elif txn_type == "JournalEntry" and txn_id in journal_entry_map:
                    credits.append((journal_entry_map[txn_id], amount))
                elif txn_type == "Purchase" and txn_id in expense_map:
                    credits.append((expense_map[txn_id], amount))

        if not bills or not credits:
            if bills or credits:
                _logger.debug(
                    f"Vendor credit application QBO-BP#{qbo_id}: only partial "
                    f"links (bills={len(bills)}, credits={len(credits)})"
                )
            return []

        pairs = []
        for credit_id, credit_amount in credits:
            for bill_id, _bill_amount in bills:
                pairs.append({
                    "invoice_move_id": bill_id,
                    "credit_memo_move_id": credit_id,
                    "amount": credit_amount,
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

        # Also collect embedded vendor credit applications on this payment.
        vendor_credit_map = builder.get_extra("vendor_credit_map") or {}
        journal_entry_map = builder.get_extra("journal_entry_map") or {}
        embedded_vc_links = []  # (odoo_vc_move_id, qbo_amount)

        for line in bp.get("Line", []):
            line_amount = float(line.get("Amount", 0) or 0)
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType")
                if txn_type == "Bill" and txn_id in bill_map:
                    linked_moves.append((bill_map[txn_id], line_amount))
                    if not payable_account_id:
                        payable_account_id = bill_payable_map.get(txn_id)
                elif txn_type == "Bill":
                    _logger.warning(
                        "BillPayment %s: Bill %s not found in Odoo",
                        qbo_bill_payment_id, txn_id,
                    )
                elif txn_type == "VendorCredit" and txn_id in vendor_credit_map:
                    embedded_vc_links.append(
                        (vendor_credit_map[txn_id], line_amount)
                    )
                elif txn_type == "VendorCredit":
                    _logger.warning(
                        "BillPayment %s: VendorCredit %s not found in Odoo",
                        qbo_bill_payment_id, txn_id,
                    )
                elif txn_type == "JournalEntry" and txn_id in journal_entry_map:
                    embedded_vc_links.append(
                        (journal_entry_map[txn_id], line_amount)
                    )

        if not payable_account_id:
            payable_account_id = partner_payable_map.get(partner_id)

        if not payable_account_id:
            _logger.warning(
                f"No payable account for bill payment {qbo_bill_payment_id}, skipping"
            )
            return None

        # Resolve currency
        currency_id, is_foreign, exchange_rate = builder.resolve_currency(bp)

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

        # Build credit application pairs for embedded VCs
        embedded_apps = []
        if embedded_vc_links and linked_moves:
            for vc_id, vc_amount in embedded_vc_links:
                for bill_id, _bill_amount in linked_moves:
                    embedded_apps.append({
                        "invoice_move_id": bill_id,
                        "credit_memo_move_id": vc_id,
                        "amount": vc_amount,
                        "qbo_payment_id": f"BP-{qbo_bill_payment_id}",
                    })

        return {
            "payment_vals": payment_vals,
            "linked_moves": linked_moves,
            "is_customer": False,
            "embedded_credit_apps": embedded_apps,
            "currency_code": (
                bp.get("CurrencyRef", {}).get("value")
                if is_foreign else None
            ),
            "exchange_rate": exchange_rate if is_foreign else None,
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
        payments = []  # (payment_record, linked_moves, is_customer, fx_info)
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
                fx_info = (pmt.get("currency_code"), pmt.get("exchange_rate"))
                payments.append(
                    (payment, pmt["linked_moves"], pmt["is_customer"], fx_info)
                )

        _logger.info(f"Created {len(payments)} payments")

        # Phase 2: Post payments, grouped by journal to minimize lock acquisitions
        # For foreign-currency payments, upsert the QBO per-transaction rate
        # into res.currency.rate immediately before posting so that Odoo's
        # line computation picks up the exact rate.
        rate_ensurer = ExchangeRateEnsurer(ctx.env)
        by_journal = {}
        for payment, linked_moves, is_customer, fx_info in payments:
            jid = payment.journal_id.id
            by_journal.setdefault(jid, []).append(
                (payment, linked_moves, is_customer, fx_info)
            )

        posted = 0
        reconciliation_queue = []
        for journal_id, group in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for payment, linked_moves, is_customer, fx_info in group:
                    qbo_id = payment.qbo_payment_id or payment.qbo_bill_payment_id or "?"
                    with ctx.skippable(f"post payment QBO#{qbo_id}"):
                        fx_code, fx_rate = fx_info
                        if fx_code and fx_rate:
                            rate_ensurer.set_rate(
                                fx_code, str(payment.date), fx_rate,
                            )
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
                        # Amount-constrained reconcile: apply exactly
                        # the QBO line amount so partial payments don't
                        # greedily consume the entire invoice.
                        reconcile_at_amount(
                            pay_line_open[0], inv_line[0], qbo_amount,
                        )
                        reconciled += 1

        _logger.info(f"Reconciled {reconciled} payment/invoice pairs")

        # Phase 4: Apply credit/debit notes to invoices/bills.
        # These come from zero-amount QBO Payments (CreditMemos → Invoices)
        # and zero-amount BillPayments (VendorCredits → Bills).
        if not credit_applications:
            return

        _logger.info(
            f"Processing {len(credit_applications)} credit/debit note applications"
        )
        applied = 0
        retry_queue = []
        for app in credit_applications:
            qbo_id = app["qbo_payment_id"]
            inv_id = app["invoice_move_id"]
            cm_id = app["credit_memo_move_id"]
            qbo_amount = float(app.get("amount", 0) or 0)
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
                    _logger.warning(
                        "Credit apply QBO#%s: no unreconciled %s lines "
                        "(inv=%d, cm=%d) — queued for retry",
                        qbo_id, account_type, len(inv_line), len(cm_line),
                    )
                    retry_queue.append(app)
                    continue
                reconcile_at_amount(cm_line[0], inv_line[0], qbo_amount)
                applied += 1

        _logger.info(f"Applied {applied} credit/debit note applications")

        # NOTE: a post-pipeline reconciliation retry would help here but
        # load_payments runs per-chunk with multiprocessing, so any retry
        # within this method races with other chunks. The retry must run
        # after all chunks complete — see account_etl.py finalizer.

