"""Post-migration validation: compare SAP GL/TB with Odoo.

Stores results in persistent models viewable in the Odoo UI.
Populated as the final step of the ETL pipeline.
"""

import logging
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.fields import Command

_logger = logging.getLogger(__name__)

# SAP OJDT.transtype -> human-readable origin
_TRANSTYPE_LABELS = {
    13: "A/R Invoice",
    14: "A/R Credit Memo",
    15: "Delivery",
    18: "A/P Invoice",
    19: "A/P Credit Memo",
    20: "Goods Receipt PO",
    24: "Incoming Payment",
    25: "Deposit",
    30: "Journal Entry",
    46: "Outgoing Payment",
    59: "Goods Receipt",
    60: "Goods Issue",
    69: "Inventory Transfer",
    202: "Production Order",
    -2: "Exchange Rate Diff",
    -3: "Period Closing",
    -4: "Rounding",
}

# SAP transtype -> SAP document table (for matching to Odoo sap_table)
_TRANSTYPE_TO_TABLE = {
    13: "oinv",
    14: "orin",
    18: "opch",
    19: "orpc",
    24: "rct2",
    46: "rct2",
}


class SapMigrationReport(models.Model):
    _name = "sap.migration.report"
    _description = "SAP Migration Validation Report"
    _order = "create_date desc"

    name = fields.Char(compute="_compute_name", store=True)
    sap_database_id = fields.Many2one("sap.database", required=True)
    cutoff_date = fields.Date(required=True)
    tolerance = fields.Float(default=0.01)
    opening_balance_account_id = fields.Many2one(
        "account.account",
        string="Opening Balance Offset Account",
        help="Account for the offsetting entry when creating opening balances "
             "from SAP-only accounts (typically retained earnings / equity).",
    )
    tb_line_ids = fields.One2many(
        "sap.migration.report.tb.line", "report_id", string="Trial Balance",
    )
    txn_line_ids = fields.One2many(
        "sap.migration.report.txn.line", "report_id", string="Transactions",
    )
    match_count = fields.Integer(readonly=True)
    drift_count = fields.Integer(readonly=True)
    sap_only_count = fields.Integer(readonly=True)
    odoo_only_count = fields.Integer(readonly=True)

    @api.depends("cutoff_date", "create_date")
    def _compute_name(self):
        for rec in self:
            ts = rec.create_date.strftime("%Y-%m-%d %H:%M") if rec.create_date else "draft"
            rec.name = f"Migration Report — {rec.cutoff_date} ({ts})"

    def action_run(self):
        """Run the full validation and populate lines."""
        self.ensure_one()
        self.tb_line_ids.unlink()
        self.txn_line_ids.unlink()

        sap_tb = self._get_sap_trial_balance()
        odoo_tb = self._get_odoo_trial_balance()

        all_codes = sorted(set(sap_tb) | set(odoo_tb))
        tb_vals = []
        stats = defaultdict(int)

        for code in all_codes:
            sap = sap_tb.get(code, {})
            odoo = odoo_tb.get(code, {})
            sap_bal = sap.get("debit", 0) - sap.get("credit", 0)
            odoo_bal = odoo.get("debit", 0) - odoo.get("credit", 0)
            bal_diff = round(sap_bal - odoo_bal, 2)

            if sap and not odoo:
                status = "sap_only"
            elif odoo and not sap:
                status = "odoo_only"
            elif abs(bal_diff) <= self.tolerance:
                status = "match"
            else:
                status = "drift"

            stats[status] += 1
            tb_vals.append(Command.create({
                "sap_acct_code": code,
                "sap_acct_name": sap.get("name", ""),
                "odoo_code": odoo.get("code", ""),
                "odoo_acct_name": odoo.get("name", ""),
                "sap_debit": round(sap.get("debit", 0), 2),
                "sap_credit": round(sap.get("credit", 0), 2),
                "sap_balance": round(sap_bal, 2),
                "odoo_debit": round(odoo.get("debit", 0), 2),
                "odoo_credit": round(odoo.get("credit", 0), 2),
                "odoo_balance": round(odoo_bal, 2),
                "balance_diff": bal_diff,
                "status": status,
            }))

        # Transaction-level detail for drifted / sap-only accounts
        drift_codes = [
            code for code in all_codes
            if code in sap_tb and (
                (code not in odoo_tb)
                or abs(
                    round(
                        (sap_tb[code].get("debit", 0) - sap_tb[code].get("credit", 0))
                        - (odoo_tb.get(code, {}).get("debit", 0)
                           - odoo_tb.get(code, {}).get("credit", 0)),
                        2,
                    )
                ) > self.tolerance
            )
        ]
        txn_vals = self._build_transaction_lines(drift_codes)

        self.write({
            "tb_line_ids": tb_vals,
            "txn_line_ids": txn_vals,
            "match_count": stats.get("match", 0),
            "drift_count": stats.get("drift", 0),
            "sap_only_count": stats.get("sap_only", 0),
            "odoo_only_count": stats.get("odoo_only", 0),
        })
        _logger.info(
            "Migration report %s: %d match, %d drift, %d sap_only, %d odoo_only",
            self.name, stats["match"], stats["drift"],
            stats["sap_only"], stats["odoo_only"],
        )

    def action_create_opening_balance(self):
        """Create a journal entry for SAP-only account balances.

        These are accounts with balances in SAP that predate the GL
        history (no OJDT entries). Creates a single JE with one line
        per SAP-only account and an offsetting line on an equity account.
        """
        self.ensure_one()
        sap_only_lines = self.tb_line_ids.filtered(
            lambda l: l.status == "sap_only" and l.sap_balance != 0
        )
        if not sap_only_lines:
            return

        Account = self.env["account.account"]
        line_commands = []
        total_offset = 0.0

        for tb_line in sap_only_lines:
            account = Account.search(
                [("sap_acct_code", "=", tb_line.sap_acct_code)], limit=1,
            )
            if not account:
                _logger.warning(
                    "Opening balance: no Odoo account for SAP %s, skipping.",
                    tb_line.sap_acct_code,
                )
                continue

            balance = tb_line.sap_balance
            debit = balance if balance > 0 else 0.0
            credit = -balance if balance < 0 else 0.0
            total_offset += balance

            line_commands.append(Command.create({
                "account_id": account.id,
                "name": f"Opening balance: {tb_line.sap_acct_name}",
                "debit": debit,
                "credit": credit,
            }))

        if not line_commands:
            return

        offset_account = self.opening_balance_account_id
        if not offset_account:
            from odoo.exceptions import UserError
            raise UserError(_(
                "No unaffected earnings account found for the offsetting entry."
            ))

        offset_debit = -total_offset if total_offset < 0 else 0.0
        offset_credit = total_offset if total_offset > 0 else 0.0
        line_commands.append(Command.create({
            "account_id": offset_account.id,
            "name": "Opening balance offset (pre-migration equity)",
            "debit": offset_debit,
            "credit": offset_credit,
        }))

        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("code", "=", "MISC")], limit=1,
        )
        if not journal:
            journal = self.env["account.journal"].search(
                [("type", "=", "general")], limit=1,
            )

        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": journal.id,
            "date": self.cutoff_date,
            "ref": f"SAP pre-migration opening balances ({self.name})",
            "line_ids": line_commands,
        })
        move.action_post()

        _logger.info(
            "Created opening balance JE %s with %d lines (offset %.2f on %s).",
            move.name, len(line_commands), total_offset, offset_account.code,
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": move.id,
            "views": [(False, "form")],
        }

    # -- SAP queries --

    def _get_sap_trial_balance(self):
        # Mirror the import-time rewrite of SAP B1 Period-End-Closing
        # JEs (OJDT.transtype='-3'): P&L legs (OACT.acttype in I/E)
        # are redirected to Odoo account 999999 (equity_unaffected).
        # Exclude those legs from their original account and aggregate
        # them under a synthetic '999999' row so the comparison TB
        # represents the same accounting treatment as Odoo.
        sap_cr = self.sap_database_id.get_cursor()
        try:
            sap_cr.execute(
                """
                SELECT a.formatcode AS code,
                       a.acctname AS name,
                       COALESCE(SUM(j.debit), 0) AS debit,
                       COALESCE(SUM(j.credit), 0) AS credit
                  FROM jdt1 j
                  JOIN ojdt h ON h.transid = j.transid
                  JOIN oact a ON j.account = a.acctcode
                 WHERE h.refdate <= %s
                   AND a.postable = 'Y'
                   AND NOT (h.transtype = '-3' AND a.acttype IN ('I', 'E'))
                 GROUP BY a.formatcode, a.acctname

                 UNION ALL

                SELECT '999999' AS code,
                       'Unallocated Earnings' AS name,
                       COALESCE(SUM(j.debit), 0) AS debit,
                       COALESCE(SUM(j.credit), 0) AS credit
                  FROM jdt1 j
                  JOIN ojdt h ON h.transid = j.transid
                  JOIN oact a ON j.account = a.acctcode
                 WHERE h.refdate <= %s
                   AND a.postable = 'Y'
                   AND h.transtype = '-3'
                   AND a.acttype IN ('I', 'E')
                HAVING COALESCE(SUM(j.debit), 0) <> 0
                    OR COALESCE(SUM(j.credit), 0) <> 0
                """,
                (self.cutoff_date, self.cutoff_date),
            )
            result = {}
            for row in sap_cr.dictfetchall():
                code = row["code"].strip()
                result[code] = {
                    "name": row["name"].strip(),
                    "debit": float(row["debit"]),
                    "credit": float(row["credit"]),
                }
            return result
        finally:
            sap_cr.close()

    def _get_sap_transactions(self, sap_acct_code):
        # The synthetic '999999' code holds the redirected P&L legs of
        # SAP B1 Period-End-Closing JEs (transtype='-3', acttype I/E).
        # SAP itself has no 999999 account, so for that code we filter
        # by transtype/acttype instead of formatcode.
        sap_cr = self.sap_database_id.get_cursor()
        try:
            if sap_acct_code == "999999":
                sap_cr.execute(
                    """
                    SELECT h.transid,
                           h.refdate,
                           h.transtype,
                           h.memo,
                           h.createdby,
                           j.debit,
                           j.credit,
                           j.shortname,
                           j.line_id
                      FROM jdt1 j
                      JOIN ojdt h ON h.transid = j.transid
                      JOIN oact a ON j.account = a.acctcode
                     WHERE h.transtype = '-3'
                       AND a.acttype IN ('I', 'E')
                       AND h.refdate <= %s
                     ORDER BY h.refdate, h.transid, j.line_id
                    """,
                    (self.cutoff_date,),
                )
            else:
                sap_cr.execute(
                    """
                    SELECT h.transid,
                           h.refdate,
                           h.transtype,
                           h.memo,
                           h.createdby,
                           j.debit,
                           j.credit,
                           j.shortname,
                           j.line_id
                      FROM jdt1 j
                      JOIN ojdt h ON h.transid = j.transid
                      JOIN oact a ON j.account = a.acctcode
                     WHERE a.formatcode = %s
                       AND h.refdate <= %s
                       AND NOT (h.transtype = '-3' AND a.acttype IN ('I', 'E'))
                     ORDER BY h.refdate, h.transid, j.line_id
                    """,
                    (sap_acct_code, self.cutoff_date),
                )
            return sap_cr.dictfetchall()
        finally:
            sap_cr.close()

    # -- Odoo queries --

    def _get_odoo_trial_balance(self):
        # Include the unallocated-earnings clearing account (Odoo code
        # 999999) under key '999999' even if it has no sap_acct_code,
        # so it lines up with the synthetic '999999' row produced by
        # _get_sap_trial_balance for redirected closing-JE P&L legs.
        # `code_store` is jsonb (per-company code map), so we resolve
        # the 999999 account id via the ORM and match on id in SQL.
        unallocated_id = self.env["account.account"].search(
            [("code", "=", "999999")], limit=1,
        ).id or 0
        self.env.cr.execute(
            """
            SELECT CASE WHEN aa.id = %s THEN '999999'
                        ELSE aa.sap_acct_code END AS sap_acct_code,
                   aa.code_store,
                   aa.name ->> 'en_US' AS acct_name,
                   COALESCE(SUM(aml.debit), 0) AS debit,
                   COALESCE(SUM(aml.credit), 0) AS credit
              FROM account_move_line aml
              JOIN account_account aa ON aml.account_id = aa.id
              JOIN account_move am ON aml.move_id = am.id
             WHERE am.date <= %s
               AND am.state = 'posted'
               AND (aa.sap_acct_code IS NOT NULL OR aa.id = %s)
             GROUP BY aa.id, aa.sap_acct_code, aa.code_store, aa.name
             ORDER BY 1
            """,
            (unallocated_id, self.cutoff_date, unallocated_id),
        )
        result = {}
        for row in self.env.cr.dictfetchall():
            code = row["sap_acct_code"].strip()
            result[code] = {
                "code": row["code_store"],
                "name": row["acct_name"] or "",
                "debit": float(row["debit"]),
                "credit": float(row["credit"]),
            }
        return result

    def _get_odoo_transactions(self, sap_acct_code):
        self.env.cr.execute(
            """
            SELECT am.id AS move_id,
                   am.name AS move_name,
                   am.sap_docentry,
                   am.sap_table,
                   am.date,
                   aml.debit,
                   aml.credit,
                   aml.name AS line_label
              FROM account_move_line aml
              JOIN account_account aa ON aml.account_id = aa.id
              JOIN account_move am ON aml.move_id = am.id
             WHERE (aa.sap_acct_code = %s
                    OR (%s = '999999' AND aa.id = %s))
               AND am.date <= %s
               AND am.state = 'posted'
             ORDER BY am.date, am.id
            """,
            (
                sap_acct_code,
                sap_acct_code,
                self.env["account.account"].search(
                    [("code", "=", "999999")], limit=1,
                ).id or 0,
                self.cutoff_date,
            ),
        )
        return self.env.cr.dictfetchall()

    # -- Transaction comparison --

    def _build_transaction_lines(self, drift_codes):
        txn_vals = []
        for code in drift_codes:
            sap_txns = self._get_sap_transactions(code)
            odoo_txns = self._get_odoo_transactions(code)

            # Index Odoo by (sap_docentry, sap_table)
            odoo_by_key = defaultdict(list)
            for txn in odoo_txns:
                if txn["sap_docentry"] and txn["sap_table"]:
                    odoo_by_key[(txn["sap_docentry"], txn["sap_table"])].append(txn)

            matched_odoo_keys = set()

            for txn in sap_txns:
                # SAP PG dump stores transtype as text
                try:
                    transtype = int(txn["transtype"])
                except (ValueError, TypeError):
                    transtype = None
                table = _TRANSTYPE_TO_TABLE.get(transtype)
                docentry = txn["createdby"]
                key = (docentry, table) if table and docentry else None

                if key and key in odoo_by_key:
                    matched_odoo_keys.add(key)
                    source = "matched"
                else:
                    source = "sap_only"

                txn_vals.append(Command.create({
                    "sap_acct_code": code,
                    "source": source,
                    "sap_transid": txn["transid"],
                    "sap_transtype": _TRANSTYPE_LABELS.get(
                        transtype, str(txn["transtype"]),
                    ),
                    "date": str(txn["refdate"])[:10] if txn["refdate"] else False,
                    "sap_debit": float(txn["debit"]),
                    "sap_credit": float(txn["credit"]),
                    "memo": txn["memo"] or "",
                    "partner_code": txn["shortname"] or "",
                }))

            for txn in odoo_txns:
                key = (txn["sap_docentry"], txn["sap_table"])
                if (
                    not txn["sap_docentry"]
                    or not txn["sap_table"]
                    or key not in matched_odoo_keys
                ):
                    txn_vals.append(Command.create({
                        "sap_acct_code": code,
                        "source": "odoo_only",
                        "odoo_move_name": txn["move_name"] or "",
                        "odoo_sap_docentry": txn["sap_docentry"] or 0,
                        "odoo_sap_table": txn["sap_table"] or "",
                        "date": str(txn["date"])[:10] if txn["date"] else False,
                        "odoo_debit": float(txn["debit"]),
                        "odoo_credit": float(txn["credit"]),
                        "memo": txn["line_label"] or "",
                    }))

        return txn_vals


class SapMigrationReportTBLine(models.Model):
    _name = "sap.migration.report.tb.line"
    _description = "Migration Report — Trial Balance Line"
    _order = "status desc, balance_diff desc"

    report_id = fields.Many2one(
        "sap.migration.report", required=True, ondelete="cascade",
    )
    sap_acct_code = fields.Char("SAP Account")
    sap_acct_name = fields.Char("SAP Name")
    odoo_code = fields.Char("Odoo Code")
    odoo_acct_name = fields.Char("Odoo Name")
    sap_debit = fields.Float("SAP Debit", digits=(16, 2))
    sap_credit = fields.Float("SAP Credit", digits=(16, 2))
    sap_balance = fields.Float("SAP Balance", digits=(16, 2))
    odoo_debit = fields.Float("Odoo Debit", digits=(16, 2))
    odoo_credit = fields.Float("Odoo Credit", digits=(16, 2))
    odoo_balance = fields.Float("Odoo Balance", digits=(16, 2))
    balance_diff = fields.Float("Difference", digits=(16, 2))
    status = fields.Selection(
        [
            ("match", "Match"),
            ("drift", "Drift"),
            ("sap_only", "SAP Only"),
            ("odoo_only", "Odoo Only"),
        ],
    )


class SapMigrationReportTxnLine(models.Model):
    _name = "sap.migration.report.txn.line"
    _description = "Migration Report — Transaction Line"
    _order = "sap_acct_code, date, sap_transid"

    report_id = fields.Many2one(
        "sap.migration.report", required=True, ondelete="cascade",
    )
    sap_acct_code = fields.Char("Account")
    source = fields.Selection(
        [
            ("matched", "Matched"),
            ("sap_only", "SAP Only"),
            ("odoo_only", "Odoo Only"),
        ],
    )
    date = fields.Date()
    memo = fields.Char()
    partner_code = fields.Char("BP Code")
    # SAP side
    sap_transid = fields.Integer("SAP Trans ID")
    sap_transtype = fields.Char("SAP Type")
    sap_debit = fields.Float("SAP Debit", digits=(16, 2))
    sap_credit = fields.Float("SAP Credit", digits=(16, 2))
    # Odoo side
    odoo_move_name = fields.Char("Odoo Move")
    odoo_sap_docentry = fields.Integer("Odoo DocEntry")
    odoo_sap_table = fields.Char("Odoo Table")
    odoo_debit = fields.Float("Odoo Debit", digits=(16, 2))
    odoo_credit = fields.Float("Odoo Credit", digits=(16, 2))
