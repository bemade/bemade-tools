"""QuickBooks Online Exchange Rate ETL Pipeline

This module syncs exchange rates from QBO transactions to Odoo before
other ETL pipelines run. It extracts rates from all transaction types
(Journal Entries, Purchases, Invoices, Bills, etc.) and creates/updates
res.currency.rate records in Odoo.

This pipeline should run FIRST, before any other transaction pipelines,
to ensure exchange rates are available when Odoo processes foreign
currency transactions.
"""

import logging
from datetime import datetime
from typing import Dict, List, Set, Tuple

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="res.currency.rate",
    importer_name="qbo.exchange.rate.importer",
    sap_source="ExchangeRate",
    depends_on=["qbo.account.importer"],  # Run early, after accounts
    allow_multiprocessing=False,  # Must run sequentially to avoid duplicates
)
class QboExchangeRateImporter(models.AbstractModel):
    """ETL Pipeline for syncing QBO exchange rates to Odoo."""

    _name = "qbo.exchange.rate.importer"
    _description = "QBO Exchange Rate Importer"

    @ETL.extract("ExchangeRate")
    def extract_exchange_rates(self, ctx: ETLContext) -> List[Dict]:
        """Extract exchange rates from all QBO transaction types.

        QBO doesn't have a dedicated exchange rate endpoint, so we extract
        rates from the ExchangeRate field on various transaction types.
        """
        api_client = get_api_client(ctx)

        # Collect unique (currency, date, rate) combinations
        rates: Dict[Tuple[str, str], float] = {}

        # Transaction types that may have exchange rates
        transaction_types = [
            ("JournalEntry", "TxnDate"),
            ("Purchase", "TxnDate"),
            ("Invoice", "TxnDate"),
            ("Bill", "TxnDate"),
            ("Payment", "TxnDate"),
            ("SalesReceipt", "TxnDate"),
            ("CreditMemo", "TxnDate"),
            ("VendorCredit", "TxnDate"),
            ("Deposit", "TxnDate"),
            ("Transfer", "TxnDate"),
        ]

        for entity_type, date_field in transaction_types:
            try:
                _logger.info(f"Extracting exchange rates from {entity_type}...")
                records = api_client.query_all(entity=entity_type, order_by="Id")

                for record in records:
                    currency_ref = record.get("CurrencyRef", {})
                    currency_code = currency_ref.get("value") if currency_ref else None
                    exchange_rate = record.get("ExchangeRate")
                    txn_date = record.get(date_field)

                    if currency_code and exchange_rate and txn_date:
                        exchange_rate = float(exchange_rate)
                        if exchange_rate != 1.0:  # Skip home currency
                            key = (currency_code, txn_date)
                            if key not in rates:
                                rates[key] = exchange_rate

            except Exception as e:
                _logger.warning(f"Could not extract rates from {entity_type}: {e}")
                continue

        # Convert to list of dicts for transform phase
        rate_list = [
            {
                "currency_code": currency_code,
                "date": txn_date,
                "rate": rate,
            }
            for (currency_code, txn_date), rate in rates.items()
        ]

        _logger.info(f"Extracted {len(rate_list)} unique exchange rates from QBO")
        return rate_list

    @ETL.transform()
    def transform_exchange_rates(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO rates into Odoo res.currency.rate values."""
        rates = extracted.get("extract_exchange_rates", [])

        company = ctx.env.company

        # Build currency lookup
        currencies = ctx.env["res.currency"].search([("active", "in", [True, False])])
        currency_map = {c.name: c.id for c in currencies}

        # Get existing rates to avoid duplicates
        ctx.env.cr.execute(
            """
            SELECT currency_id, name::text 
            FROM res_currency_rate 
            WHERE company_id = %s
        """,
            (company.id,),
        )
        existing_rates = {(row[0], row[1]) for row in ctx.env.cr.fetchall()}

        # Use dict to deduplicate by (currency_id, date) key
        rate_vals_dict: Dict[Tuple[int, str], Dict] = {}
        skipped_no_currency = 0

        for rate_data in rates:
            currency_code = rate_data["currency_code"]
            currency_id = currency_map.get(currency_code)

            if not currency_id:
                skipped_no_currency += 1
                continue

            # Parse date
            try:
                date = datetime.strptime(rate_data["date"], "%Y-%m-%d").date()
            except ValueError:
                continue

            key = (currency_id, str(date))

            # Skip if already exists in DB, otherwise set in dict (overwrites duplicates)
            if key not in existing_rates:
                # QBO ExchangeRate = home currency units per 1 foreign unit
                # e.g., 1.4 means 1 USD = 1.4 CAD
                # Odoo rate = foreign currency units per 1 home currency unit
                # e.g., 0.714 means 1 CAD = 0.714 USD
                # So: odoo_rate = 1 / qbo_exchange_rate
                qbo_rate = rate_data["rate"]
                odoo_rate = 1.0 / qbo_rate if qbo_rate else 1.0
                rate_vals_dict[key] = {
                    "name": date,
                    "rate": odoo_rate,
                    "currency_id": currency_id,
                    "company_id": company.id,
                }

        rate_vals = list(rate_vals_dict.values())

        _logger.info(
            f"Transformed {len(rate_vals)} new rates, "
            f"skipped {skipped_no_currency} with unknown currency"
        )
        return rate_vals

    @ETL.load()
    def load_exchange_rates(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load exchange rates into Odoo."""
        rate_vals = transformed.get("transform_exchange_rates", [])

        if not rate_vals:
            _logger.info("No new exchange rates to create")
            return

        # Batch create rates
        created = 0
        errors = 0

        for vals in rate_vals:
            try:
                ctx.env["res.currency.rate"].create(vals)
                created += 1
            except Exception as e:
                # Handle race condition duplicates gracefully
                if "unique constraint" in str(e).lower():
                    _logger.debug(f"Rate already exists for {vals['name']}")
                else:
                    _logger.warning(f"Failed to create rate: {e}")
                    errors += 1

        _logger.info(f"Created {created} exchange rates ({errors} errors)")
