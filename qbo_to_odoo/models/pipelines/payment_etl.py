"""QuickBooks Online Payment ETL Pipeline

This module handles the migration of Payments and BillPayments from QBO to Odoo
using the ETL framework. Payments are created as account.payment objects, which
automatically generate journal entries when posted. Reconciliation is NOT done
here — the account finalizer replays every QBO payment application in one pass
once all transaction pipelines have committed (see reconcile_pass.py).
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
    # Posting upserts per-date currency rates (ExchangeRateEnsurer) that
    # later payments in the same run depend on — keep single-process,
    # in-transaction execution rather than racing chunk-workers on
    # res.currency.rate. (Reconciliation itself now happens once, in the
    # account finalizer's reconcile_pass, after all pipelines commit.)
    allow_multiprocessing=False,
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

        # Pipeline-specific: invoice/bill maps — used to resolve each
        # payment's destination AR/AP account from its linked documents.
        extractor.extra["invoice_map"] = extractor.qbo_id_map(
            "account_move", "qbo_invoice_id", where="state = 'posted'"
        )
        extractor.extra["bill_map"] = extractor.qbo_id_map(
            "account_move", "qbo_bill_id", where="state = 'posted'"
        )

        # Pre-fetch receivable/payable accounts for destination_account_id
        extractor.extra["invoice_receivable_map"] = extractor.invoice_receivable_map()
        extractor.extra["bill_payable_map"] = extractor.bill_payable_map()
        extractor.extra["partner_receivable_map"] = extractor.partner_receivable_map()
        extractor.extra["partner_payable_map"] = extractor.partner_payable_map()

        # Ensure the cached QBO JournalReport exists before the load phase.
        # load_payments reads it (per chunk) to recover the true bank rate for
        # foreign payments QBO booked at par (see _build_gl_bank_home). The
        # cache is idempotent and later reused by the fallback pipeline.
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        connection._ensure_journal_cache()

        all_payments = new_payments + new_bill_payments

        return ChunkableData(
            records=all_payments,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted: Dict) -> Dict:
        """Transform QBO payments into account.payment values."""
        data = extracted.get("extract_payments")
        if not data:
            return {"payments": []}
        all_payments = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])
        invoice_map = builder.get_extra("invoice_map") or {}
        bill_map = builder.get_extra("bill_map") or {}
        journal_bank_account_map = builder.get_extra("journal_bank_account_map") or {}

        payment_data = []
        skipped = 0

        for pmt in all_payments:
            pmt_type = pmt["type"]
            pmt_data = pmt["data"]
            total_amt = float(pmt_data.get("TotalAmt", 0) or 0)

            # Zero-amount payments are pure credit/debit note applications —
            # no cash moves, so there is no account.payment to create. The
            # finalizer's reconcile_pass replays their application links.
            if total_amt <= 0:
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

        _logger.info(
            f"Transformed {len(payment_data)} payments, skipped {skipped}"
        )
        return {"payments": payment_data}

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

        # Get receivable account: prefer linked invoice, fall back to partner
        # default. (The links themselves are replayed by the finalizer's
        # reconcile_pass — here they only pin the destination account.)
        recv_account_id = None
        invoice_recv_map = builder.get_extra("invoice_receivable_map") or {}
        partner_recv_map = builder.get_extra("partner_receivable_map") or {}

        for line in payment.get("Line", []):
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType")
                if txn_type == "Invoice" and txn_id in invoice_map:
                    if not recv_account_id:
                        recv_account_id = invoice_recv_map.get(txn_id)
                elif txn_type == "Invoice":
                    _logger.warning(
                        "Payment %s: Invoice %s not found in Odoo",
                        qbo_payment_id, txn_id,
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

        return {
            "payment_vals": payment_vals,
            "is_customer": True,
            "currency_code": (
                payment.get("CurrencyRef", {}).get("value")
                if is_foreign else None
            ),
            "exchange_rate": exchange_rate if is_foreign else None,
        }

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

        # Get payable account: prefer linked bill, fall back to partner
        # default. (The links themselves are replayed by the finalizer's
        # reconcile_pass — here they only pin the destination account.)
        payable_account_id = None
        bill_payable_map = builder.get_extra("bill_payable_map") or {}
        partner_payable_map = builder.get_extra("partner_payable_map") or {}

        for line in bp.get("Line", []):
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType")
                if txn_type == "Bill" and txn_id in bill_map:
                    if not payable_account_id:
                        payable_account_id = bill_payable_map.get(txn_id)
                elif txn_type == "Bill":
                    _logger.warning(
                        "BillPayment %s: Bill %s not found in Odoo",
                        qbo_bill_payment_id, txn_id,
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

        return {
            "payment_vals": payment_vals,
            "is_customer": False,
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

    @staticmethod
    def _build_gl_bank_home(ctx: ETLContext) -> Dict[str, Dict[str, float]]:
        """Return ``{qbo_txn_id: {account_code: home_net}}`` for payments.

        Reads the cached QBO JournalReport (home-currency amounts) for the
        payment transaction types.  Used to recover the exact bank rate for
        foreign payments QBO booked at par — see the posting loop.
        """
        ctx.env.cr.execute(
            """
            SELECT t.qbo_txn_id, l.account_code,
                   sum(l.debit - l.credit)
            FROM qbo_journal_cache_transaction t
            JOIN qbo_journal_cache_line l ON l.transaction_id = t.id
            WHERE t.txn_type IN (
                'Payment',
                'Bill Payment (Cheque)',
                'Bill Payment (Credit Card)'
            )
            GROUP BY t.qbo_txn_id, l.account_code
            """
        )
        result: Dict[str, Dict[str, float]] = {}
        for txn_id, code, home in ctx.env.cr.fetchall():
            if not txn_id or not code:
                continue
            result.setdefault(txn_id, {})[code] = home
        return result

    @staticmethod
    def _gl_bank_rate(payment, gl_bank_home: Dict[str, Dict[str, float]]):
        """Derive QBO's true bank rate (home per foreign) for *payment*.

        ``rate = |bank home CAD| / |foreign paid|`` from the cached
        JournalReport bank line on the payment's own journal account.
        Returns ``None`` when the cache has no usable amount.
        """
        txn_id = payment.qbo_bill_payment_id or payment.qbo_payment_id
        if not txn_id:
            return None
        # qbo_bill_payment_id / qbo_payment_id are Integer fields; the cache
        # keys on the Char qbo_txn_id — match on the string form.
        txn_id = str(txn_id)
        bank_account = payment.journal_id.default_account_id
        bank_code = bank_account.code if bank_account else None
        if not bank_code:
            return None
        bank_home = (gl_bank_home.get(txn_id) or {}).get(bank_code)
        if not bank_home or not payment.amount:
            return None
        return abs(bank_home) / abs(payment.amount)

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create and post account.payment records.

        Reconciliation happens later, in the account finalizer's
        reconcile_pass, once every transaction pipeline has committed.
        """
        transform_result = transformed.get("transform_payments", {})
        # Backwards compat: if transform returned a plain list (old code path)
        if isinstance(transform_result, list):
            payment_data = transform_result
        else:
            payment_data = transform_result.get("payments", [])

        if not payment_data:
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
        payments = []  # (payment_record, fx_info)
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
                payments.append((payment, fx_info))

        _logger.info(f"Created {len(payments)} payments")

        # Phase 2: Post payments, grouped by journal to minimize lock acquisitions
        # For foreign-currency payments, upsert the QBO per-transaction rate
        # into res.currency.rate immediately before posting so that Odoo's
        # line computation picks up the exact rate.
        rate_ensurer = ExchangeRateEnsurer(ctx.env)
        # GL-truth bank rates, keyed by QBO transaction id, for the par-booking
        # recovery in the posting loop below.
        gl_bank_home = self._build_gl_bank_home(ctx)
        by_journal = {}
        for payment, fx_info in payments:
            jid = payment.journal_id.id
            by_journal.setdefault(jid, []).append((payment, fx_info))

        posted = 0
        for journal_id, group in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for payment, fx_info in group:
                    qbo_id = payment.qbo_payment_id or payment.qbo_bill_payment_id or "?"
                    with ctx.skippable(f"post payment QBO#{qbo_id}"):
                        fx_code, fx_rate = fx_info
                        if fx_code:
                            if fx_rate and fx_rate != 1.0:
                                # QBO gave an explicit conversion rate — trust it.
                                rate_ensurer.set_rate(
                                    fx_code, str(payment.date), fx_rate,
                                )
                            else:
                                # QBO booked this foreign payment at par
                                # (ExchangeRate 1.0/absent). Recover the true
                                # bank rate from the cached JournalReport:
                                # rate = |bank home CAD| / |foreign paid|.
                                # Without this Odoo falls back to the prevailing
                                # daily rate and fabricates FX (drift on 5900 +
                                # the USD banks).
                                gl_rate = self._gl_bank_rate(
                                    payment, gl_bank_home,
                                )
                                if gl_rate:
                                    rate_ensurer.set_rate(
                                        fx_code, str(payment.date), gl_rate,
                                        force=True,
                                    )
                        payment.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} payments")

