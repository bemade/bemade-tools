"""Exchange-rate helpers for QBO ETL pipelines.

Three mechanisms depending on the Odoo move type:

* **Invoices / bills / credit-memos / receipts** — set
  ``invoice_currency_rate`` directly on the ``account.move``.  Odoo uses
  this per-move field for every line-amount computation.  The global rate
  table is also seeded so that ``reconcile()`` can compute accurate
  exchange differences later.

* **Payments** (``account.payment``) — Odoo derives line amounts from the
  global ``res.currency.rate`` table.  We upsert the QBO per-transaction
  rate immediately before posting each payment.

* **Journal entries** built through ``move_builder`` — debit / credit are
  set explicitly at creation time, so no rate lookup is involved.
"""

import logging
from typing import Dict

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Convention helpers
# ---------------------------------------------------------------------------

def qbo_rate_to_odoo(qbo_rate: float) -> float:
    """Convert a QBO exchange rate to Odoo convention.

    QBO:  home (CAD) units per 1 foreign (USD) unit  → e.g. 1.40
    Odoo ``res.currency.rate``:  foreign per 1 home   → e.g. 0.714
    Odoo ``invoice_currency_rate``: same as res.currency.rate
    """
    return 1.0 / qbo_rate if qbo_rate else 1.0


def qbo_rate_from_record(record: dict) -> float | None:
    """Extract the QBO exchange rate from a raw API record.

    Returns the rate in **QBO convention** (home per 1 foreign), or *None*
    if the record has no foreign-currency info.
    """
    currency_ref = record.get("CurrencyRef", {})
    code = currency_ref.get("value") if currency_ref else None
    rate = record.get("ExchangeRate")
    if not code or not rate:
        return None
    rate = float(rate)
    return rate if rate != 1.0 else None


# ---------------------------------------------------------------------------
# Per-move rate setter  (invoices, bills, credit-memos, receipts)
# ---------------------------------------------------------------------------

def set_move_currency_rate(move, qbo_rate: float) -> None:
    """Set ``invoice_currency_rate`` on *move* from a QBO rate.

    Only effective for invoice-type moves (``is_invoice(True)``).  For
    journal entries this is a no-op because Odoo ignores the field.

    Call **after** ``create()`` and **before** ``action_post()``.
    """
    if not qbo_rate or qbo_rate == 1.0:
        return
    move.invoice_currency_rate = qbo_rate_to_odoo(qbo_rate)


# ---------------------------------------------------------------------------
# Global-rate upsert
# ---------------------------------------------------------------------------

class ExchangeRateEnsurer:
    """Upserts per-transaction exchange rates into ``res.currency.rate``.

    Call ``set_rate()`` immediately before posting each foreign-currency
    move so that Odoo's line computation and later ``reconcile()`` calls
    pick up the exact QBO per-transaction rate.
    """

    def __init__(self, env):
        self.env = env
        self._company = env.company
        self._company_currency_id = self._company.currency_id.id

        # Build currency lookup: {code: id}
        currencies = env["res.currency"].search([("active", "in", [True, False])])
        self._currency_map: Dict[str, int] = {c.name: c.id for c in currencies}

    def set_rate(
        self,
        currency_code: str,
        date: str,
        qbo_rate: float,
    ) -> None:
        """Upsert a single exchange rate, overwriting any existing value.

        Safe to call repeatedly for the same ``(currency, date)`` — each
        call overwrites the previous value so the *last* caller wins.

        Args:
            currency_code: ISO currency code (e.g. ``"USD"``).
            date: Transaction date as ``"YYYY-MM-DD"``.
            qbo_rate: QBO convention rate (home per 1 foreign).
        """
        currency_id = self._currency_map.get(currency_code)
        if not currency_id or currency_id == self._company_currency_id:
            return
        if not qbo_rate or qbo_rate == 1.0:
            return

        odoo_rate = qbo_rate_to_odoo(qbo_rate)
        uid = self.env.uid
        self.env.cr.execute(
            """
            INSERT INTO res_currency_rate
                   (currency_id, name, rate, company_id,
                    create_uid, create_date, write_uid, write_date)
            VALUES (%(cid)s, %(dt)s, %(rate)s, %(co)s,
                    %(uid)s, NOW() AT TIME ZONE 'UTC',
                    %(uid)s, NOW() AT TIME ZONE 'UTC')
            ON CONFLICT (name, currency_id, company_id)
            DO UPDATE SET rate      = EXCLUDED.rate,
                          write_uid = EXCLUDED.write_uid,
                          write_date = EXCLUDED.write_date
            """,
            {
                "cid": currency_id,
                "dt": date,
                "rate": odoo_rate,
                "co": self._company.id,
                "uid": uid,
            },
        )
        # Raw SQL bypasses the ORM, so we must manually invalidate the
        # computed ``inverse_rate`` field on res.currency — otherwise
        # ``currency._convert()`` will use a stale cached value.
        self.env["res.currency"].invalidate_model(["inverse_rate"])
        self.env.registry.clear_cache()
