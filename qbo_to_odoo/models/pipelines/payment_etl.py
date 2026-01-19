"""QuickBooks Online Payment ETL Pipeline

This module handles the migration of Payments and BillPayments from QBO to Odoo
using the ETL framework.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.payment",
    importer_name="qbo.payment.importer",
    sap_source="Payment",
    depends_on=["qbo.invoice.importer", "qbo.bill.importer"],
)
class QboPaymentImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Payments."""

    _name = "qbo.payment.importer"
    _description = "QBO Payment Importer"

    @ETL.extract("Payment")
    def extract_payments(self, ctx: ETLContext) -> List[Dict]:
        """Extract payments from QBO API."""
        api_client = ctx.get_config("api_client")
        if not api_client:
            raise ValueError("API client not found in ETL context")

        # Get existing QBO payment IDs
        ctx.env.cr.execute(
            "SELECT qbo_payment_id FROM account_payment WHERE qbo_payment_id IS NOT NULL"
        )
        existing_payment_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_bill_payment_id FROM account_payment WHERE qbo_bill_payment_id IS NOT NULL"
        )
        existing_bill_payment_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}

        _logger.info(
            f"Found {len(existing_payment_ids)} existing customer payments in Odoo"
        )
        _logger.info(
            f"Found {len(existing_bill_payment_ids)} existing bill payments in Odoo"
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
            f"Extracted {len(payments)} customer payments, {len(new_payments)} are new"
        )
        _logger.info(
            f"Extracted {len(bill_payments)} bill payments, {len(new_bill_payments)} are new"
        )

        # Combine into single list for proper chunking
        all_payments = new_payments + new_bill_payments
        return all_payments

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO payments into Odoo account.payment values."""
        all_payments = extracted.get("extract_payments", [])

        # Build lookups
        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner WHERE qbo_customer_id IS NOT NULL"
        )
        customer_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_vendor_id, id FROM res_partner WHERE qbo_vendor_id IS NOT NULL"
        )
        vendor_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Split back into customer and bill payments
        payments = [p["data"] for p in all_payments if p.get("type") == "customer"]
        bill_payments = [p["data"] for p in all_payments if p.get("type") == "vendor"]

        company = ctx.env.company

        # Find bank journal (required for payments)
        bank_journal = ctx.env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not bank_journal:
            # Try cash journal as fallback
            bank_journal = ctx.env["account.journal"].search(
                [("type", "=", "cash"), ("company_id", "=", company.id)],
                limit=1,
            )
        if not bank_journal:
            raise ValueError("No bank or cash journal found for payments")

        payment_vals = []
        skipped = 0

        # Process customer payments
        for payment in payments:
            try:
                vals = self._transform_customer_payment(
                    payment, customer_map, bank_journal, company, ctx
                )
                if vals:
                    payment_vals.append(vals)
                else:
                    skipped += 1
            except Exception as e:
                _logger.error(f"Error transforming payment {payment.get('Id')}: {e}")
                skipped += 1

        # Process bill payments
        for bp in bill_payments:
            try:
                vals = self._transform_bill_payment(
                    bp, vendor_map, bank_journal, company, ctx
                )
                if vals:
                    payment_vals.append(vals)
                else:
                    skipped += 1
            except Exception as e:
                _logger.error(f"Error transforming bill payment {bp.get('Id')}: {e}")
                skipped += 1

        _logger.info(f"Transformed {len(payment_vals)} payments, skipped {skipped}")
        return payment_vals

    def _transform_customer_payment(
        self, payment: Dict, customer_map: Dict, bank_journal, company, ctx: ETLContext
    ) -> Optional[Dict]:
        """Transform a customer payment."""
        # Get customer
        customer_ref = payment.get("CustomerRef", {})
        qbo_customer_id = int(customer_ref.get("value", 0))
        partner_id = customer_map.get(qbo_customer_id)

        if not partner_id:
            _logger.warning(
                f"Customer not found for QBO ID {qbo_customer_id} "
                f"in payment {payment.get('Id')}"
            )
            return None

        # Parse date
        txn_date = payment.get("TxnDate")
        payment_date = None
        if txn_date:
            try:
                payment_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
            except ValueError:
                payment_date = datetime.now().date()

        # Get amount
        amount = float(payment.get("TotalAmt", 0) or 0)
        if amount <= 0:
            return None

        # Get currency
        currency_id = company.currency_id.id
        currency_ref = payment.get("CurrencyRef", {})
        if currency_ref:
            currency_code = currency_ref.get("value")
            if currency_code:
                currency = ctx.env["res.currency"].search(
                    [("name", "=", currency_code)], limit=1
                )
                if currency:
                    currency_id = currency.id

        vals = {
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner_id,
            "amount": amount,
            "date": payment_date,
            "currency_id": currency_id,
            "memo": payment.get("PaymentRefNum", "") or f"QBO-{payment.get('Id')}",
            "qbo_payment_id": int(payment.get("Id")),
        }

        vals["journal_id"] = bank_journal.id

        return vals

    def _transform_bill_payment(
        self, bp: Dict, vendor_map: Dict, bank_journal, company, ctx: ETLContext
    ) -> Optional[Dict]:
        """Transform a bill payment."""
        # Get vendor
        vendor_ref = bp.get("VendorRef", {})
        qbo_vendor_id = int(vendor_ref.get("value", 0))
        partner_id = vendor_map.get(qbo_vendor_id)

        if not partner_id:
            _logger.warning(
                f"Vendor not found for QBO ID {qbo_vendor_id} "
                f"in bill payment {bp.get('Id')}"
            )
            return None

        # Parse date
        txn_date = bp.get("TxnDate")
        payment_date = None
        if txn_date:
            try:
                payment_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
            except ValueError:
                payment_date = datetime.now().date()

        # Get amount
        amount = float(bp.get("TotalAmt", 0) or 0)
        if amount <= 0:
            return None

        # Get currency
        currency_id = company.currency_id.id
        currency_ref = bp.get("CurrencyRef", {})
        if currency_ref:
            currency_code = currency_ref.get("value")
            if currency_code:
                currency = ctx.env["res.currency"].search(
                    [("name", "=", currency_code)], limit=1
                )
                if currency:
                    currency_id = currency.id

        vals = {
            "payment_type": "outbound",
            "partner_type": "supplier",
            "partner_id": partner_id,
            "amount": amount,
            "date": payment_date,
            "currency_id": currency_id,
            "memo": bp.get("DocNumber", "") or f"QBO-BP-{bp.get('Id')}",
            "qbo_bill_payment_id": int(bp.get("Id")),
        }

        vals["journal_id"] = bank_journal.id

        return vals

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load payments into Odoo."""
        payment_vals = transformed.get("transform_payments", [])

        if not payment_vals:
            _logger.info("No new payments to create")
            return

        # Batch create all payments
        payments = ctx.env["account.payment"].create(payment_vals)
        _logger.info(f"Created {len(payments)} payments")

        # Post payments - action_post should work on recordset
        # but if it doesn't, we fall back to individual posting
        try:
            payments.action_post()
            _logger.info(f"Posted {len(payments)} payments")
        except Exception:
            _logger.info("Batch post failed, posting individually...")
            for payment in payments:
                payment.action_post()
            _logger.info(f"Posted {len(payments)} payments individually")
