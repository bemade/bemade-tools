"""QuickBooks Online Partner Account Linker ETL Pipeline

Sets property_account_receivable_id on customer partners and
property_account_payable_id on vendor partners by inspecting which AR/AP
account each partner's transactions use in QBO.

Must run after accounts, customers, and vendors are imported and before
invoices and bills are imported, so that Odoo's invoicing machinery
naturally uses the correct per-partner AR/AP account when action_post()
regenerates the counterpart line.
"""

import logging
from collections import Counter
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="res.partner",
    importer_name="qbo.partner.account.linker",
    sap_source="Invoice",
    depends_on=[
        "qbo.account.importer",
        "qbo.customer.importer",
        "qbo.vendor.importer",
    ],
)
class QboPartnerAccountLinker(models.AbstractModel):
    """ETL Pipeline for setting per-partner AR/AP accounts from QBO history."""

    _name = "qbo.partner.account.linker"
    _description = "QBO Partner Account Linker"

    @ETL.extract("Invoice")
    def extract_partner_accounts(self, ctx: ETLContext) -> Dict:
        """Extract AR/AP account usage per partner from QBO transaction history.

        QBO bills expose APAccountRef directly. QBO invoices do NOT expose
        ARAccountRef, so we infer the AR account from the invoice currency
        (each AR account in the CoA corresponds to a single currency).
        """
        api_client = get_api_client(ctx)

        _logger.info("Querying QBO invoices for per-customer currency usage...")
        invoices = api_client.query_all(entity="Invoice", order_by="Id")

        # Map customer → most-used invoice currency (to resolve AR account)
        customer_currency: Dict[str, Counter] = {}
        for inv in invoices:
            customer_id = inv.get("CustomerRef", {}).get("value")
            currency = inv.get("CurrencyRef", {}).get("value")
            if customer_id and currency:
                customer_currency.setdefault(customer_id, Counter())[currency] += 1

        _logger.info("Querying QBO bills for per-vendor AP account usage...")
        bills = api_client.query_all(entity="Bill", order_by="Id")

        vendor_ap: Dict[str, Counter] = {}
        for bill in bills:
            vendor_id = bill.get("VendorRef", {}).get("value")
            ap_id = bill.get("APAccountRef", {}).get("value")
            if vendor_id and ap_id:
                vendor_ap.setdefault(vendor_id, Counter())[ap_id] += 1

        _logger.info(
            f"Currency usage found for {len(customer_currency)} customers, "
            f"AP usage found for {len(vendor_ap)} vendors"
        )
        return {"customer_currency": customer_currency, "vendor_ap": vendor_ap}

    @ETL.transform()
    def transform_partner_accounts(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Resolve QBO IDs to Odoo IDs and build the update list."""
        data = extracted.get("extract_partner_accounts", {})
        customer_currency = data.get("customer_currency", {})
        vendor_ap = data.get("vendor_ap", {})

        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner "
            "WHERE qbo_customer_id IS NOT NULL"
        )
        customer_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_vendor_id, id FROM res_partner "
            "WHERE qbo_vendor_id IS NOT NULL"
        )
        vendor_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        # Build currency code → AR account mapping from the currency_id
        # set on each receivable account during account import.
        # Sorted by code so the primary account (lowest code) wins when
        # multiple AR accounts share the same currency (e.g. AR CAD vs AR FX).
        ar_accounts = ctx.env["account.account"].search(
            [
                ("account_type", "=", "asset_receivable"),
                ("active", "=", True),
                ("currency_id", "!=", False),
            ],
            order="code",
        )
        currency_ar_map = {}
        for ar in ar_accounts:
            currency_ar_map.setdefault(ar.currency_id.name, ar.id)
        # Also map the company currency to any AR account without a currency_id
        company_currency = ctx.env.company.currency_id.name
        if company_currency not in currency_ar_map:
            default_ar = ctx.env["account.account"].search(
                [
                    ("account_type", "=", "asset_receivable"),
                    ("active", "=", True),
                    ("currency_id", "=", False),
                ],
                order="code",
                limit=1,
            )
            if default_ar:
                currency_ar_map[company_currency] = default_ar.id
        _logger.info(f"Currency → AR account map: {currency_ar_map}")

        updates = []

        for qbo_customer_id, counts in customer_currency.items():
            partner_id = customer_map.get(qbo_customer_id)
            if not partner_id:
                continue
            best_currency = counts.most_common(1)[0][0]
            ar_account_id = currency_ar_map.get(best_currency)
            if not ar_account_id:
                _logger.warning(
                    f"No AR account found for currency {best_currency}, "
                    f"customer QBO#{qbo_customer_id}"
                )
                continue
            updates.append(
                {
                    "partner_id": partner_id,
                    "property_account_receivable_id": ar_account_id,
                }
            )

        for qbo_vendor_id, counts in vendor_ap.items():
            partner_id = vendor_map.get(qbo_vendor_id)
            if not partner_id:
                continue
            best_ap_qbo_id = counts.most_common(1)[0][0]
            ap_account_id = account_map.get(best_ap_qbo_id)
            if not ap_account_id:
                _logger.warning(
                    f"AP account QBO#{best_ap_qbo_id} not found for vendor "
                    f"QBO#{qbo_vendor_id}"
                )
                continue
            updates.append(
                {
                    "partner_id": partner_id,
                    "property_account_payable_id": ap_account_id,
                }
            )

        _logger.info(f"Prepared {len(updates)} partner account updates")
        return updates

    @ETL.load()
    def load_partner_accounts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Write AR/AP accounts directly onto partner records."""
        updates = transformed.get("transform_partner_accounts", [])

        if not updates:
            _logger.info("No partner account updates to apply")
            return

        ar_count = 0
        ap_count = 0

        for update in updates:
            partner_id = update["partner_id"]
            partner = ctx.env["res.partner"].browse(partner_id)
            if "property_account_receivable_id" in update:
                partner.property_account_receivable_id = update[
                    "property_account_receivable_id"
                ]
                ar_count += 1
            if "property_account_payable_id" in update:
                partner.property_account_payable_id = update[
                    "property_account_payable_id"
                ]
                ap_count += 1

        _logger.info(
            f"Set AR account on {ar_count} customer partners, "
            f"AP account on {ap_count} vendor partners"
        )
