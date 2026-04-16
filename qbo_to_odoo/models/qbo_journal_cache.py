"""QBO JournalReport Cache.

Fetches the QBO JournalReport once and caches it as Odoo records.
Two consumers read from the cache:

1. **Journal fallback pipeline** — imports transaction types without a
   dedicated QBO API endpoint (Payroll Cheque, Tax Payment, etc.).
2. **Migration validation report** — compares QBO vs Odoo trial balances.

Caching ensures both consumers see identical data and eliminates
reliance on potentially stale XLSX exports.
"""

import logging
from collections import defaultdict
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class QboJournalCache(models.Model):
    _name = "qbo.journal.cache"
    _description = "QBO JournalReport Cache"
    _order = "fetch_date desc"

    qbo_connection_id = fields.Many2one(
        "qbo.connection",
        string="QBO Connection",
        required=True,
        ondelete="cascade",
    )
    fetch_date = fields.Datetime(
        string="Fetched At",
        readonly=True,
    )
    date_from = fields.Date(
        string="Period Start",
        readonly=True,
    )
    date_to = fields.Date(
        string="Period End",
        readonly=True,
    )
    row_count = fields.Integer(
        string="Total Rows",
        readonly=True,
    )
    transaction_ids = fields.One2many(
        "qbo.journal.cache.transaction",
        "cache_id",
        string="Transactions",
    )

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def action_refresh(self):
        """Fetch the JournalReport from QBO and populate the cache."""
        self.ensure_one()
        api_client = self.qbo_connection_id.get_api_client()
        if not api_client:
            raise UserError(
                _("Could not connect to the QBO API. Check connection details.")
            )

        # Clear existing cached data
        self.transaction_ids.unlink()

        # Discover the date range that contains data
        date_from, date_to = self._discover_date_range(api_client)
        self.date_from = date_from
        self.date_to = date_to

        # Build 2-year chunks within the discovered range
        chunks = self._build_date_chunks(date_from, date_to)

        # Fetch and parse each chunk
        all_transactions = {}  # txn_key -> {vals dict with line list}
        total_rows = 0

        for start_date, end_date in chunks:
            data = api_client.get_report(
                "JournalReport",
                {
                    "start_date": str(start_date),
                    "end_date": str(end_date),
                    "minorversion": "75",
                },
            )
            rows = self._parse_report_chunk(data, all_transactions)
            total_rows += rows

        # Batch-create transaction and line records
        txn_vals_list = []
        for txn in all_transactions.values():
            txn_vals_list.append({
                "cache_id": self.id,
                "qbo_txn_id": txn["qbo_txn_id"],
                "txn_type": txn["txn_type"],
                "txn_date": txn["txn_date"],
                "txn_num": txn["txn_num"],
                "txn_name": txn["txn_name"],
                "line_ids": [(0, 0, line) for line in txn["lines"]],
            })

        if txn_vals_list:
            self.env["qbo.journal.cache.transaction"].create(txn_vals_list)

        self.fetch_date = fields.Datetime.now()
        self.row_count = total_rows

        _logger.info(
            "QBO JournalReport cache refreshed: %d transactions, %d rows, "
            "period %s to %s",
            len(txn_vals_list),
            total_rows,
            date_from,
            date_to,
        )

    def _discover_date_range(self, api_client):
        """Discover the earliest and latest years with QBO data.

        Fetches the TransactionList report grouped by year with
        ``date_macro=All``.  The grouped summary is tiny (one row per
        year) so there is no truncation risk.

        Returns:
            Tuple of (date_from, date_to) as ``date`` objects.
        """
        data = api_client.get_report(
            "TransactionList",
            {
                "date_macro": "All",
                "group_by": "Year",
            },
        )

        # Extract years from the response.  The grouped report has
        # Section rows whose Header contains the year label.
        years = set()
        for row in data.get("Rows", {}).get("Row", []):
            header = row.get("Header", {})
            col_data = header.get("ColData", [])
            if col_data:
                year_str = col_data[0].get("value", "")
                if year_str.isdigit():
                    years.add(int(year_str))

            # Also check nested rows for year labels
            summary = row.get("Summary", {})
            summary_cols = summary.get("ColData", [])
            if summary_cols:
                val = summary_cols[0].get("value", "")
                # Summary values like "Total for 2014"
                for word in val.split():
                    if word.isdigit() and len(word) == 4:
                        years.add(int(word))

        if not years:
            _logger.warning(
                "TransactionList returned no year groups — "
                "defaulting to 2014–today"
            )
            return date(2014, 1, 1), date.today()

        min_year = min(years)
        max_year = max(years)
        _logger.info(
            "QBO data spans %d–%d (%d years)",
            min_year,
            max_year,
            max_year - min_year + 1,
        )
        return date(min_year, 1, 1), date(max_year, 12, 31)

    @staticmethod
    def _build_date_chunks(date_from, date_to):
        """Build a list of 2-year (start, end) date pairs."""
        chunks = []
        year = date_from.year
        while year <= date_to.year:
            chunk_start = date(year, 1, 1)
            chunk_end = date(min(year + 1, date_to.year), 12, 31)
            if chunk_end > date_to:
                chunk_end = date_to
            chunks.append((chunk_start, chunk_end))
            year += 2
        return chunks

    def _parse_report_chunk(self, data, transactions):
        """Parse one JournalReport API response into ``transactions`` dict.

        Args:
            data: Raw JSON dict from ``api_client.get_report()``.
            transactions: Mutable dict (txn_key → txn dict) accumulating
                across chunks.  Each txn dict has keys: ``qbo_txn_id``,
                ``txn_type``, ``txn_date``, ``txn_num``, ``txn_name``,
                ``lines`` (list of line dicts).

        Returns:
            Number of data rows parsed in this chunk.
        """
        rows = data.get("Rows", {}).get("Row", [])

        # Detect column positions from header metadata
        columns = data.get("Columns", {}).get("Column", [])
        col_keys = [
            next(
                (
                    m["Value"]
                    for m in c.get("MetaData", [])
                    if m.get("Name") == "ColKey"
                ),
                "",
            )
            for c in columns
        ]

        def _idx(key, default):
            return col_keys.index(key) if key in col_keys else default

        idx_date = _idx("tx_date", 0)
        idx_type = _idx("txn_type", 1)
        idx_num = _idx("doc_num", 2)
        idx_name = _idx("name", 3)
        idx_memo = _idx("memo", 4)
        idx_code = _idx("acct_num_with_extn", 5)
        idx_acct = _idx("account_name", 6)
        idx_debit = _idx("debt_home_amt", 7)
        idx_credit = _idx("credit_home_amt", 8)

        # Current transaction header state
        cur_date = ""
        cur_type = ""
        cur_num = ""
        cur_name = ""
        cur_txn_id = ""
        row_count = 0

        for row in rows:
            if row.get("type") != "Data":
                continue
            col_data = row.get("ColData", [])
            vals = [c.get("value", "") for c in col_data]
            if not vals:
                continue

            # Header rows carry date/type/name; continuation rows have
            # empty date and inherit the prior header values.
            if vals[idx_date] and vals[idx_date] != "0-00-00":
                cur_date = vals[idx_date]
                cur_type = vals[idx_type]
                cur_num = vals[idx_num]
                cur_name = vals[idx_name]
                # Extract QBO transaction ID from the txn_type ColData
                if idx_type < len(col_data):
                    cur_txn_id = str(
                        col_data[idx_type].get("id", "")
                    )

            raw_code = vals[idx_code] if idx_code < len(vals) else ""
            if not raw_code:
                continue

            # Normalize account code (strip trailing zeros after decimal)
            code = (
                raw_code.rstrip("0").rstrip(".")
                if "." in raw_code
                else raw_code
            )

            acct_name = vals[idx_acct] if idx_acct < len(vals) else ""
            debit_str = vals[idx_debit] if idx_debit < len(vals) else ""
            credit_str = vals[idx_credit] if idx_credit < len(vals) else ""
            d = float(debit_str) if debit_str else 0.0
            c = float(credit_str) if credit_str else 0.0

            if d == 0 and c == 0:
                continue

            memo = vals[idx_memo] if idx_memo < len(vals) else ""

            # Group by transaction ID (fall back to composite key)
            txn_key = cur_txn_id or f"{cur_date}_{cur_type}_{cur_num}"

            if txn_key not in transactions:
                transactions[txn_key] = {
                    "qbo_txn_id": cur_txn_id,
                    "txn_type": cur_type,
                    "txn_date": cur_date,
                    "txn_num": cur_num,
                    "txn_name": cur_name,
                    "lines": [],
                }

            transactions[txn_key]["lines"].append({
                "account_code": code,
                "account_name": acct_name,
                "memo": memo,
                "name": cur_name,
                "debit": d,
                "credit": c,
            })
            row_count += 1

        return row_count

    # ------------------------------------------------------------------
    # Query helpers (consumed by fallback pipeline + migration report)
    # ------------------------------------------------------------------

    def get_trial_balance(self):
        """Return ``(tb_dict, txn_by_account)`` from cached data.

        Matches the return shape of the former
        ``qbo_migration_report._get_qbo_trial_balance()``:

        - ``tb_dict``: ``{account_code: {name, debit, credit}}``
        - ``txn_by_account``: ``{account_code: [{qbo_id, type, num,
          date, name, memo, debit, credit}, ...]}``
        """
        self.ensure_one()
        tb = {}
        txn_by_account = defaultdict(list)

        for txn in self.transaction_ids:
            for line in txn.line_ids:
                code = line.account_code
                if code not in tb:
                    tb[code] = {
                        "name": line.account_name,
                        "debit": 0.0,
                        "credit": 0.0,
                    }
                tb[code]["debit"] += line.debit
                tb[code]["credit"] += line.credit

                txn_by_account[code].append({
                    "qbo_id": txn.qbo_txn_id or "",
                    "type": txn.txn_type or "",
                    "num": txn.txn_num or "",
                    "date": str(txn.txn_date) if txn.txn_date else "",
                    "name": txn.txn_name or "",
                    "memo": line.memo or "",
                    "debit": line.debit,
                    "credit": line.credit,
                })

        return tb, dict(txn_by_account)

    def get_transactions_for_import(self, allowed_types, exclude_ids):
        """Return transactions matching *allowed_types*, excluding IDs
        already imported.

        Returns a list of dicts matching the shape expected by
        ``journal_entry_vals_from_export()``::

            [
                {
                    "id": "12345",
                    "lines": [
                        {
                            "date": "2025-02-15",
                            "type": "Payroll Cheque",
                            "num": "1234",
                            "name": "Employee Name",
                            "memo": "...",
                            "account_code": "2100",
                            "account_name": "...",
                            "debit": 1500.00,
                            "credit": 0.00,
                        },
                        ...
                    ],
                },
                ...
            ]
        """
        self.ensure_one()
        domain = [
            ("cache_id", "=", self.id),
            ("txn_type", "in", list(allowed_types)),
        ]
        if exclude_ids:
            domain.append(("qbo_txn_id", "not in", list(exclude_ids)))

        result = []
        for txn in self.env["qbo.journal.cache.transaction"].search(domain):
            lines = []
            for line in txn.line_ids:
                lines.append({
                    "date": str(txn.txn_date) if txn.txn_date else "",
                    "type": txn.txn_type or "",
                    "num": txn.txn_num or "",
                    "name": txn.txn_name or "",
                    "memo": line.memo or "",
                    "account_code": line.account_code or "",
                    "account_name": line.account_name or "",
                    "debit": line.debit,
                    "credit": line.credit,
                })
            result.append({
                "id": txn.qbo_txn_id or str(txn.id),
                "lines": lines,
            })
        return result


class QboJournalCacheTransaction(models.Model):
    _name = "qbo.journal.cache.transaction"
    _description = "QBO JournalReport Cached Transaction"
    _order = "txn_date, qbo_txn_id"

    cache_id = fields.Many2one(
        "qbo.journal.cache",
        string="Cache",
        required=True,
        ondelete="cascade",
        index=True,
    )
    qbo_txn_id = fields.Char(
        string="QBO Transaction ID",
        index=True,
    )
    txn_type = fields.Char(
        string="Transaction Type",
        index=True,
    )
    txn_date = fields.Date(string="Date")
    txn_num = fields.Char(string="Doc Number")
    txn_name = fields.Char(string="Name/Payee")
    line_ids = fields.One2many(
        "qbo.journal.cache.line",
        "transaction_id",
        string="Lines",
    )


class QboJournalCacheLine(models.Model):
    _name = "qbo.journal.cache.line"
    _description = "QBO JournalReport Cached Line"
    _order = "id"

    transaction_id = fields.Many2one(
        "qbo.journal.cache.transaction",
        string="Transaction",
        required=True,
        ondelete="cascade",
        index=True,
    )
    account_code = fields.Char(string="Account Code", index=True)
    account_name = fields.Char(string="Account Name")
    memo = fields.Char(string="Memo")
    name = fields.Char(string="Name")
    debit = fields.Float(string="Debit", digits=(16, 2))
    credit = fields.Float(string="Credit", digits=(16, 2))
