"""Inline exchange rate helper for QBO ETL pipelines.

Ensures that res.currency.rate records exist for all foreign-currency
transactions being imported, without requiring a separate full-scan
exchange rate pipeline.

Uses INSERT ... ON CONFLICT DO NOTHING to safely handle concurrent
workers inserting rates for the same (currency, date) pair.
"""

import logging
from typing import Dict, List, Tuple

_logger = logging.getLogger(__name__)


class ExchangeRateEnsurer:
    """Creates missing Odoo exchange rates from QBO transaction data.

    Instead of running a dedicated exchange rate pipeline that queries every
    transaction type from the API, this helper extracts rates from records
    already fetched by the calling pipeline and upserts any that are missing.

    Safe under concurrency: uses ON CONFLICT DO NOTHING so parallel workers
    inserting the same (currency, date) pair won't conflict.

    Usage::

        ExchangeRateEnsurer(ctx.env).ensure_rates(qbo_records)
    """

    def __init__(self, env):
        self.env = env
        self._company = env.company
        self._company_currency_id = self._company.currency_id.id

        # Build currency lookup: {code: id}
        currencies = env["res.currency"].search([("active", "in", [True, False])])
        self._currency_map: Dict[str, int] = {c.name: c.id for c in currencies}

    def ensure_rates(
        self,
        records: List[Dict],
        date_field: str = "TxnDate",
    ) -> int:
        """Create missing exchange rates extracted from QBO records.

        Args:
            records: Raw QBO API records containing CurrencyRef,
                ExchangeRate, and a date field.
            date_field: Name of the date field on the QBO records.

        Returns:
            Number of rows inserted (excludes conflicts).
        """
        # Collect unique (currency_code, date) -> qbo_rate
        needed: Dict[Tuple[str, str], float] = {}

        for record in records:
            currency_ref = record.get("CurrencyRef", {})
            currency_code = currency_ref.get("value") if currency_ref else None
            exchange_rate = record.get("ExchangeRate")
            txn_date = record.get(date_field)

            if not currency_code or not exchange_rate or not txn_date:
                continue

            rate = float(exchange_rate)
            if rate == 1.0:
                continue

            key = (currency_code, txn_date)
            if key not in needed:
                needed[key] = rate

        if not needed:
            return 0

        # Build rows for bulk insert
        rows = []
        for (currency_code, date_str), qbo_rate in needed.items():
            currency_id = self._currency_map.get(currency_code)
            if not currency_id or currency_id == self._company_currency_id:
                continue

            # QBO: home units per 1 foreign unit (e.g. 1.4 = 1 USD -> 1.4 CAD)
            # Odoo: foreign units per 1 home unit (e.g. 0.714 = 1 CAD -> 0.714 USD)
            odoo_rate = 1.0 / qbo_rate if qbo_rate else 1.0
            rows.append((currency_id, date_str, odoo_rate, self._company.id))

        if not rows:
            return 0

        # res.currency.rate has: UNIQUE (name, currency_id, company_id)
        cr = self.env.cr
        query = """
            INSERT INTO res_currency_rate (currency_id, name, rate, company_id,
                                           create_uid, create_date, write_uid, write_date)
            VALUES %s
            ON CONFLICT (name, currency_id, company_id) DO NOTHING
        """
        # Build VALUES clause with proper placeholders
        values_template = "(%%s, %%s, %%s, %%s, %s, NOW() AT TIME ZONE 'UTC', %s, NOW() AT TIME ZONE 'UTC')"
        uid = self.env.uid
        values_template = values_template % (uid, uid)
        values_list = [
            cr.mogrify(values_template, row).decode()
            for row in rows
        ]
        cr.execute(query % ", ".join(values_list))
        inserted = cr.rowcount

        if inserted:
            # Invalidate ORM cache so subsequent reads see the new rates
            self.env.registry.clear_cache()
            _logger.info(
                f"Inserted {inserted} exchange rates "
                f"({len(rows) - inserted} already existed)"
            )

        return inserted
