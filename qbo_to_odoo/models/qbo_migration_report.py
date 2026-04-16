"""Post-migration validation: compare QBO Journal export GL vs Odoo.

Stores results in persistent models viewable in the Odoo UI.
Can be triggered from the QBO connection form or created manually.
"""

import logging
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

_logger = logging.getLogger(__name__)

# QBO transaction type labels for display
_QBO_TYPE_LABELS = {
    "Invoice": "Invoice",
    "Bill": "Bill",
    "Credit Memo": "Credit Memo",
    "Vendor Credit": "Vendor Credit",
    "Payment": "Payment",
    "Bill Payment (Cheque)": "Bill Payment",
    "Bill Payment (Credit Card)": "Bill Payment (CC)",
    "Cheque": "Cheque",
    "Expense": "Expense",
    "Transfer": "Transfer",
    "Deposit": "Deposit",
    "Journal Entry": "Journal Entry",
    "Sales Receipt": "Sales Receipt",
    "Refund Receipt": "Refund Receipt",
    "Sales Tax Payment": "Tax Payment",
    "Tax Payment": "Tax Payment",
    "Credit Card Payment": "CC Payment",
    "Credit Card Credit": "CC Credit",
    "Credit Card Expense": "CC Expense",
    "Payroll Cheque": "Payroll Cheque",
    "Supplier Credit": "Supplier Credit",
    "Inventory Starting Value": "Inventory Starting Value",
}


class QboMigrationReport(models.Model):
    _name = "qbo.migration.report"
    _description = "QBO Migration Validation Report"
    _order = "create_date desc"

    name = fields.Char(compute="_compute_name", store=True)
    qbo_connection_id = fields.Many2one("qbo.connection", required=True)
    tolerance = fields.Float(default=0.01)
    opening_balance_account_id = fields.Many2one(
        "account.account",
        string="Opening Balance Offset Account",
        help="Account for the offsetting entry when creating opening balances "
        "from QBO-only accounts (typically retained earnings / equity).",
    )
    tb_line_ids = fields.One2many(
        "qbo.migration.report.tb.line",
        "report_id",
        string="Trial Balance",
    )
    txn_line_ids = fields.One2many(
        "qbo.migration.report.txn.line",
        "report_id",
        string="Transactions",
    )
    match_count = fields.Integer(readonly=True)
    drift_count = fields.Integer(readonly=True)
    qbo_only_count = fields.Integer(readonly=True)
    odoo_only_count = fields.Integer(readonly=True)

    @api.depends("create_date")
    def _compute_name(self):
        for rec in self:
            ts = (
                rec.create_date.strftime("%Y-%m-%d %H:%M")
                if rec.create_date
                else "draft"
            )
            rec.name = f"QBO Migration Report ({ts})"

    def action_run(self):
        """Run the full validation and populate lines."""
        self.ensure_one()

        self.tb_line_ids.unlink()
        self.txn_line_ids.unlink()

        qbo_tb, qbo_txns = self._get_qbo_trial_balance()
        odoo_tb, odoo_txns = self._get_odoo_trial_balance()

        all_codes = sorted(set(qbo_tb) | set(odoo_tb))
        tb_vals = []
        stats = defaultdict(int)

        for code in all_codes:
            qbo = qbo_tb.get(code, {})
            odoo = odoo_tb.get(code, {})
            qbo_bal = qbo.get("debit", 0) - qbo.get("credit", 0)
            odoo_bal = odoo.get("debit", 0) - odoo.get("credit", 0)
            bal_diff = round(qbo_bal - odoo_bal, 2)

            if qbo and not odoo:
                status = "qbo_only"
            elif odoo and not qbo:
                status = "odoo_only"
            elif abs(bal_diff) <= self.tolerance:
                status = "match"
            else:
                status = "drift"

            stats[status] += 1
            tb_vals.append(
                Command.create(
                    {
                        "account_code": code,
                        "qbo_acct_name": qbo.get("name", ""),
                        "odoo_acct_name": odoo.get("name", ""),
                        "qbo_debit": round(qbo.get("debit", 0), 2),
                        "qbo_credit": round(qbo.get("credit", 0), 2),
                        "qbo_balance": round(qbo_bal, 2),
                        "odoo_debit": round(odoo.get("debit", 0), 2),
                        "odoo_credit": round(odoo.get("credit", 0), 2),
                        "odoo_balance": round(odoo_bal, 2),
                        "balance_diff": bal_diff,
                        "status": status,
                    }
                )
            )

        # Transaction-level detail for drifted / qbo-only accounts
        drift_codes = [
            code
            for code in all_codes
            if code in qbo_tb
            and (
                (code not in odoo_tb)
                or abs(
                    round(
                        (qbo_tb[code].get("debit", 0) - qbo_tb[code].get("credit", 0))
                        - (
                            odoo_tb.get(code, {}).get("debit", 0)
                            - odoo_tb.get(code, {}).get("credit", 0)
                        ),
                        2,
                    )
                )
                > self.tolerance
            )
        ]
        txn_vals = self._build_transaction_lines(drift_codes, qbo_txns, odoo_txns)

        self.write(
            {
                "tb_line_ids": tb_vals,
                "txn_line_ids": txn_vals,
                "match_count": stats.get("match", 0),
                "drift_count": stats.get("drift", 0),
                "qbo_only_count": stats.get("qbo_only", 0),
                "odoo_only_count": stats.get("odoo_only", 0),
            }
        )
        _logger.info(
            "QBO migration report %s: %d match, %d drift, %d qbo_only, %d odoo_only",
            self.name,
            stats["match"],
            stats["drift"],
            stats["qbo_only"],
            stats["odoo_only"],
        )

    def action_create_opening_balance(self):
        """Create a journal entry for QBO-only account balances."""
        self.ensure_one()
        qbo_only_lines = self.tb_line_ids.filtered(
            lambda l: l.status == "qbo_only" and l.qbo_balance != 0
        )
        if not qbo_only_lines:
            return

        Account = self.env["account.account"]
        line_commands = []
        total_offset = 0.0

        for tb_line in qbo_only_lines:
            account = Account.search(
                [("qbo_id", "=", tb_line.account_code)],
                limit=1,
            )
            if not account:
                # Try matching by code
                account = Account.search(
                    [("code", "=", tb_line.account_code)],
                    limit=1,
                )
            if not account:
                _logger.warning(
                    "Opening balance: no Odoo account for QBO %s, skipping.",
                    tb_line.account_code,
                )
                continue

            balance = tb_line.qbo_balance
            debit = balance if balance > 0 else 0.0
            credit = -balance if balance < 0 else 0.0
            total_offset += balance

            line_commands.append(
                Command.create(
                    {
                        "account_id": account.id,
                        "name": f"Opening balance: {tb_line.qbo_acct_name}",
                        "debit": debit,
                        "credit": credit,
                    }
                )
            )

        if not line_commands:
            return

        offset_account = self.opening_balance_account_id
        if not offset_account:
            from odoo.exceptions import UserError

            raise UserError(
                _("Set an Opening Balance Offset Account before creating entries.")
            )

        offset_debit = -total_offset if total_offset < 0 else 0.0
        offset_credit = total_offset if total_offset > 0 else 0.0
        line_commands.append(
            Command.create(
                {
                    "account_id": offset_account.id,
                    "name": "Opening balance offset (pre-migration equity)",
                    "debit": offset_debit,
                    "credit": offset_credit,
                }
            )
        )

        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("code", "=", "MISC")],
            limit=1,
        )
        if not journal:
            journal = self.env["account.journal"].search(
                [("type", "=", "general")],
                limit=1,
            )

        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "journal_id": journal.id,
                "date": fields.Date.today(),
                "ref": f"QBO pre-migration opening balances ({self.name})",
                "line_ids": line_commands,
            }
        )
        move.action_post()

        _logger.info(
            "Created opening balance JE %s with %d lines (offset %.2f on %s).",
            move.name,
            len(line_commands),
            total_offset,
            offset_account.code,
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": move.id,
            "views": [(False, "form")],
        }

    # -- QBO Journal export queries --

    def _get_qbo_trial_balance(self):
        """Return (tb_dict, txn_by_account) from the JournalReport cache.

        Delegates to ``qbo.journal.cache.get_trial_balance()``.  The
        cache is auto-created and refreshed on first access.

        tb_dict: {account_code: {name, debit, credit}}
        txn_dict: {account_code: [list of transaction dicts]}
        """
        conn = self.qbo_connection_id
        cache = conn._ensure_journal_cache()
        return cache.get_trial_balance()

    def _get_odoo_trial_balance(self):
        """Return (tb_dict, txn_dict) from all posted Odoo moves.

        After a clean import (wiped DB), every posted move originates from
        QBO so no source filter is needed.  Using all posted moves avoids
        false negatives from enriched payments whose ``qbo_*_id`` lives on
        ``account.payment`` rather than ``account.move``.
        """
        cr = self.env.cr
        cr.execute(
            """
            SELECT aa.id as account_id,
                   aa.code_store::jsonb->>jsonb_object_keys(aa.code_store::jsonb) as code,
                   aa.name ->> 'en_US' AS acct_name,
                   COALESCE(SUM(aml.debit), 0) AS debit,
                   COALESCE(SUM(aml.credit), 0) AS credit
              FROM account_move_line aml
              JOIN account_account aa ON aml.account_id = aa.id
              JOIN account_move am ON aml.move_id = am.id
             WHERE am.state = 'posted'
             GROUP BY aa.id, code, acct_name
             ORDER BY code
        """
        )
        tb = {}
        for row in cr.dictfetchall():
            code = row["code"]
            if not code:
                continue
            tb[code] = {
                "name": row["acct_name"] or "",
                "debit": float(row["debit"]),
                "credit": float(row["credit"]),
            }

        # Transaction-level detail
        cr.execute(
            """
            SELECT aa.code_store::jsonb->>jsonb_object_keys(aa.code_store::jsonb) as code,
                   am.id as move_id,
                   am.name as move_name,
                   am.ref,
                   am.date,
                   aml.debit,
                   aml.credit,
                   aml.name as line_label
              FROM account_move_line aml
              JOIN account_account aa ON aml.account_id = aa.id
              JOIN account_move am ON aml.move_id = am.id
             WHERE am.state = 'posted'
             ORDER BY am.date, am.id
        """
        )
        txn_by_account = defaultdict(list)
        for row in cr.dictfetchall():
            code = row["code"]
            if not code:
                continue
            txn_by_account[code].append(
                {
                    "move_name": row["move_name"] or "",
                    "ref": row["ref"] or "",
                    "date": str(row["date"])[:10] if row["date"] else "",
                    "debit": float(row["debit"]),
                    "credit": float(row["credit"]),
                    "label": row["line_label"] or "",
                }
            )

        return tb, txn_by_account

    # -- Transaction comparison --

    def _build_transaction_lines(self, drift_codes, qbo_txns, odoo_txns):
        """Build transaction detail lines for drifted accounts."""
        txn_vals = []

        for code in drift_codes:
            qbo_lines = qbo_txns.get(code, [])
            odoo_lines = odoo_txns.get(code, [])

            # Index Odoo lines by ref (which contains QBO ID info)
            odoo_by_ref = defaultdict(list)
            for txn in odoo_lines:
                if txn["ref"]:
                    odoo_by_ref[txn["ref"]].append(txn)

            for txn in qbo_lines:
                txn_vals.append(
                    Command.create(
                        {
                            "account_code": code,
                            "source": "qbo",
                            "qbo_id": str(txn.get("qbo_id", "")),
                            "qbo_type": _QBO_TYPE_LABELS.get(
                                txn.get("type", ""),
                                txn.get("type", ""),
                            ),
                            "date": txn.get("date") or False,
                            "qbo_debit": txn.get("debit", 0),
                            "qbo_credit": txn.get("credit", 0),
                            "memo": txn.get("memo", ""),
                            "partner_name": txn.get("name", ""),
                        }
                    )
                )

            for txn in odoo_lines:
                txn_vals.append(
                    Command.create(
                        {
                            "account_code": code,
                            "source": "odoo",
                            "odoo_move_name": txn.get("move_name", ""),
                            "odoo_ref": txn.get("ref", ""),
                            "date": txn.get("date") or False,
                            "odoo_debit": txn.get("debit", 0),
                            "odoo_credit": txn.get("credit", 0),
                            "memo": txn.get("label", ""),
                        }
                    )
                )

        return txn_vals


class QboMigrationReportTBLine(models.Model):
    _name = "qbo.migration.report.tb.line"
    _description = "QBO Migration Report — Trial Balance Line"
    _order = "status desc, balance_diff desc"

    report_id = fields.Many2one(
        "qbo.migration.report",
        required=True,
        ondelete="cascade",
    )
    account_code = fields.Char("Account Code")
    qbo_acct_name = fields.Char("QBO Name")
    odoo_acct_name = fields.Char("Odoo Name")
    qbo_debit = fields.Float("QBO Debit", digits=(16, 2))
    qbo_credit = fields.Float("QBO Credit", digits=(16, 2))
    qbo_balance = fields.Float("QBO Balance", digits=(16, 2))
    odoo_debit = fields.Float("Odoo Debit", digits=(16, 2))
    odoo_credit = fields.Float("Odoo Credit", digits=(16, 2))
    odoo_balance = fields.Float("Odoo Balance", digits=(16, 2))
    balance_diff = fields.Float("Difference", digits=(16, 2))
    status = fields.Selection(
        [
            ("match", "Match"),
            ("drift", "Drift"),
            ("qbo_only", "QBO Only"),
            ("odoo_only", "Odoo Only"),
        ]
    )


class QboMigrationReportTxnLine(models.Model):
    _name = "qbo.migration.report.txn.line"
    _description = "QBO Migration Report — Transaction Line"
    _order = "account_code, date, source"

    report_id = fields.Many2one(
        "qbo.migration.report",
        required=True,
        ondelete="cascade",
    )
    account_code = fields.Char("Account")
    source = fields.Selection(
        [
            ("qbo", "QBO Export"),
            ("odoo", "Odoo"),
        ]
    )
    date = fields.Date()
    memo = fields.Char()
    partner_name = fields.Char("Partner")
    # QBO side
    qbo_id = fields.Char("QBO Txn ID")
    qbo_type = fields.Char("QBO Type")
    qbo_debit = fields.Float("QBO Debit", digits=(16, 2))
    qbo_credit = fields.Float("QBO Credit", digits=(16, 2))
    # Odoo side
    odoo_move_name = fields.Char("Odoo Move")
    odoo_ref = fields.Char("Odoo Ref")
    odoo_debit = fields.Float("Odoo Debit", digits=(16, 2))
    odoo_credit = fields.Float("Odoo Credit", digits=(16, 2))
