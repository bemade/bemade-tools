"""Connection to an offline Sage 50 company file, and the take-on it drives.

Sage's MySQL server is started by `scripts/setup_sage_db.sh` with
`skip-grant-tables`, which implies `--skip-networking`, so in practice the
connection is a unix socket. Host and port are supported anyway for the case
where the file has been loaded into a MySQL of your own.

This model also carries the handful of destination settings the take-on
needs — the journal the opening entries go to, the cutover date, the
transition account, and Sage's own known trial-balance imbalance — because
they belong to a migration run and not to the company's permanent
configuration.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.etl_framework import (
    ETL,
    ETLContext,
    ETLExecutor,
    PipelineOrchestrator,
)

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)


class SageCursor:
    """A PyMySQL cursor that answers to `dictfetchall()`.

    The ETL framework types `ctx.cr` as `Any` and only ever calls `execute()`
    and `dictfetchall()` on it, and PyMySQL takes the same `%s` paramstyle as
    psycopg2, so the pipelines here read exactly like the Postgres-sourced
    ones in this repo. This thin wrapper is the whole of the difference.
    """

    def __init__(self, connection):
        self._connection = connection
        self._cursor = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def execute(self, sql, args=None):
        return self._cursor.execute(sql, args)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    def dictfetchall(self):
        return list(self._cursor.fetchall())

    def close(self):
        try:
            self._cursor.close()
        finally:
            self._connection.close()


class SageDatabase(models.Model):
    _name = "sage.database"
    _description = "Sage 50 Company File"

    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )

    # -- connection ---------------------------------------------------
    socket_path = fields.Char(
        string="Unix socket",
        help="Path to the socket `setup_sage_db.sh` leaves behind, normally "
             "<work dir>/sagedb/run/mysql.sock. Takes precedence over the "
             "host and port.",
    )
    database_host = fields.Char(default="127.0.0.1")
    database_port = fields.Integer(default=3306)
    database_name = fields.Char(required=True, default="simply")
    database_username = fields.Char(required=True, default="root")
    database_password = fields.Char()

    # -- source-file properties --------------------------------------
    language = fields.Selection(
        [("en", "English"), ("fr", "French")],
        default="fr",
        required=True,
        help="Sage 50 CA files carry two names for accounts, items and "
             "pricelists. This picks which one the import uses.",
    )

    # -- take-on settings --------------------------------------------
    history_start_date = fields.Date(
        string="Import history from",
        help="Leave empty for a balances-only take-on: Odoo opens at the "
             "cutover date with no history behind it. Set it to the first "
             "day of a fiscal year Sage still holds to replay every journal "
             "entry from that day forward, which is what lets a year be "
             "closed in Odoo rather than merely opened there. It must be a "
             "fiscal year start — anything else would leave retained "
             "earnings carrying a part-year roll Sage never performed.",
    )
    cutover_date = fields.Date(
        help="Balances are taken as at the end of this day. It must fall "
             "inside the fiscal year the company file is open on — an older "
             "file simply cannot describe a later date.",
    )
    journal_id = fields.Many2one(
        "account.journal",
        domain="[('type', '=', 'general')]",
        help="Journal for the counter-entry and the opening entry. A "
             "dedicated one keeps the whole take-on isolable afterwards.",
    )
    transition_account_id = fields.Many2one(
        "account.account",
        help="The opening entry balances against this account. Once the open "
             "documents, the counter-entry and the opening entry are all "
             "posted it must read the known imbalance below and nothing "
             "else.",
    )
    known_imbalance = fields.Float(
        digits="Account",
        help="The balance the transition account is expected to carry once "
             "everything is posted. Sage trial balances that have never "
             "balanced are common, and the difference usually predates the "
             "oldest generation in the file, so it cannot be made to "
             "disappear. Record it here so the verification checks it rather "
             "than rediscovering it.",
    )

    @api.depends("database_host", "database_name", "socket_path")
    def _compute_display_name(self):
        for record in self:
            where = record.socket_path or (
                f"{record.database_host}:{record.database_port}"
            )
            record.display_name = f"{where}/{record.database_name}"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def get_cursor(self) -> SageCursor:
        """Open a read-only cursor onto the Sage company file.

        PyMySQL is imported here rather than at module scope so the module
        remains importable — and therefore uninstallable — on a server where
        the driver was never installed. That is not a hypothetical: this
        module is meant to be uninstalled before the migrated database is
        promoted, and an uninstall still loads the registry.
        """
        self.ensure_one()
        try:
            import pymysql
        except ImportError as error:
            raise UserError(
                _("PyMySQL is not installed. It is only needed on the machine "
                  "running the migration; add `PyMySQL` to requirements.txt "
                  "there.")
            ) from error

        kwargs = {
            "user": self.database_username,
            "database": self.database_name,
            "charset": "utf8mb4",
            "cursorclass": pymysql.cursors.DictCursor,
        }
        if self.database_password:
            kwargs["password"] = self.database_password
        if self.socket_path:
            kwargs["unix_socket"] = self.socket_path
        else:
            kwargs["host"] = self.database_host
            kwargs["port"] = self.database_port or 3306
        return SageCursor(pymysql.connect(**kwargs))

    def action_test_connection(self):
        self.ensure_one()
        with self.get_cursor() as cr:
            cr.execute("select count(*) as n from taccount")
            accounts = cr.fetchone()["n"]
            cr.execute("select dtSDate, dtFDate from tcompany")
            fiscal = cr.fetchone()
        return self._notification(
            _("Connected. %(n)s accounts; the file is open on the fiscal year "
              "%(start)s to %(end)s.",
              n=accounts,
              start=fiscal["dtSDate"].strftime("%Y-%m-%d"),
              end=fiscal["dtFDate"].strftime("%Y-%m-%d")),
        )

    # ------------------------------------------------------------------
    # Hooks for client layers
    # ------------------------------------------------------------------
    def sage_name(self, row, primary, alternate):
        """Pick the English or French name off a Sage row.

        Sage 50 CA carries both, in differently-named columns per table
        (`sName`/`sNameAlt` on accounts, `sName`/`sNameF` on items,
        `sDesc`/`sDescF` on pricelists), which is why the columns are passed
        in rather than guessed.
        """
        self.ensure_one()
        first, second = (
            (alternate, primary) if self.language == "fr" else (primary, alternate)
        )
        return ((row.get(first) or row.get(second) or "")).strip()

    def _get_source_config(self) -> dict:
        self.ensure_one()
        return {
            "source_id": self.id,
            "source_model": self._name,
            "company_id": self.company_id.id,
            "language": self.language,
            "cutover_date": (
                self.cutover_date.strftime("%Y-%m-%d")
                if self.cutover_date else None
            ),
            "history_start_date": (
                self.history_start_date.strftime("%Y-%m-%d")
                if self.history_start_date else None
            ),
            "journal_id": self.journal_id.id,
            "transition_account_id": self.transition_account_id.id,
            "known_imbalance": self.known_imbalance,
        }

    @api.model
    def _notification(self, message, kind="success") -> dict:
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sage 50 take-on"),
                "message": message,
                "sticky": kind != "success",
                "type": kind,
            },
        }

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------
    def _execute_pipelines(self, pipeline_names: list) -> dict:
        self.ensure_one()
        env = self.with_company(self.company_id).env
        with self.get_cursor() as cr:
            orchestrator = PipelineOrchestrator(
                env, source_config=self._get_source_config()
            )
            orchestrator.execute_pipelines(cr, pipeline_names)
        return self._notification(
            _("Imported: %s.", ", ".join(pipeline_names))
        )

    def _execute_pipeline(self, pipeline_name: str) -> dict:
        self.ensure_one()
        pipeline = ETL.get_pipeline(pipeline_name)
        if not pipeline:
            raise UserError(_("No ETL pipeline named %s.", pipeline_name))
        env = self.with_company(self.company_id).env
        with self.get_cursor() as cr:
            ctx = ETLContext(
                cr=cr, env=env, source_config=self._get_source_config()
            )
            ETLExecutor(pipeline, ctx, env[pipeline_name]).execute()
            self.env.cr.commit()
        return self._notification(_("Imported: %s.", pipeline_name))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_import_master_data(self) -> dict:
        """Chart of accounts, partners, categories, products, pricelists."""
        return self._execute_pipelines([
            "sage.account.importer",
            "sage.partner.importer",
            "sage.product.category.importer",
            "sage.product.importer",
            "sage.pricelist.item.importer",
        ])

    def action_import_open_items(self) -> dict:
        """The open receivables and payables carried over at cutover."""
        return self._execute_pipelines([
            "sage.open.item.importer",
            "sage.bank.journal.importer",
            "sage.payment.importer",
        ])

    def action_import_opening_entries(self) -> dict:
        """The counter-entry and the opening trial balance, in that order."""
        return self._execute_pipelines([
            "sage.counter.entry.importer",
            "sage.opening.balance.importer",
        ])

    def action_import_all(self) -> dict:
        self.ensure_one()
        env = self.with_company(self.company_id).env
        with self.get_cursor() as cr:
            orchestrator = PipelineOrchestrator(
                env,
                source_config=self._get_source_config(),
                module_filter="sage50_to_odoo",
            )
            orchestrator.execute_all(cr)
        return self._notification(_("The Sage 50 take-on is loaded."))

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    def action_check(self) -> dict:
        """The checks that actually catch a bad take-on.

        Which check is the strong one depends on the shape of the take-on.

        **Balances only.** A trial balance that ties proves less than it
        looks: an invoice missed at the open-items step is reversed away by
        the counter-entry and never shows up in the trial balance at all.
        What exposes it is the partner-less balance on the control accounts,
        which the opening entry and the counter-entry are constructed to
        leave at exactly zero.

        **With history.** That check no longer holds, and saying so is the
        point — the opening entry carries the control balances as they stood
        at the start of the replay, with no partner, and they decay toward
        zero only as the replayed payments settle the documents that were
        open back then. The strong check here is the other one: every
        account's balance in Odoo against the same account in Sage, at the
        last date the file describes. It is reported per account, because a
        total that ties while two accounts are wrong in opposite directions
        is the failure this is meant to catch.
        """
        self.ensure_one()
        lines, ok = [], True
        history = bool(self.history_start_date)
        if history:
            tied, tb_lines = self._check_trial_balance()
            ok = ok and tied
            lines.extend(tb_lines)
        for account, label in (
            (self.env["account.account"].search([
                ("company_ids", "in", self.company_id.id),
                ("account_type", "=", "asset_receivable"),
                ("sage_account_id", "!=", 0),
            ], limit=1), _("Receivable")),
            (self.env["account.account"].search([
                ("company_ids", "in", self.company_id.id),
                ("account_type", "=", "liability_payable"),
                ("sage_account_id", "!=", 0),
            ], limit=1), _("Payable")),
        ):
            if not account:
                continue
            orphan = self._posted_balance(account, partner_less=True)
            if history:
                # Information, not a verdict: see the docstring.
                lines.append(
                    f"{label}: {orphan:,.2f} with no partner (the opening "
                    f"balance, less whatever the replay has since settled)"
                )
                continue
            verdict, good = self._verdict(orphan)
            ok = ok and good
            lines.append(f"{label}: {orphan:,.2f} with no partner  {verdict}")

        if self.transition_account_id:
            balance = self._posted_balance(self.transition_account_id)
            good = abs(balance - self.known_imbalance) < 0.01
            ok = ok and good
            lines.append(
                f"Transition account: {balance:,.2f}  "
                + ("= the recorded Sage imbalance, as expected" if good else
                   f"expected {self.known_imbalance:,.2f} — the take-on does "
                   f"not tie")
            )

        threshold = self.company_id.invoicing_switch_threshold
        if threshold:
            ok = False
            lines.append(
                f"Invoicing Switch Threshold is {threshold} — it MUST be "
                f"empty. It cancels posted entries before its date with a raw "
                f"SQL sweep and no chatter."
            )
        else:
            lines.append("Invoicing Switch Threshold: empty  OK")

        return self._notification(
            "\n".join(lines), kind="success" if ok else "danger"
        )

    #: A control account this far from zero is source rounding, not a missing
    #: document. Sage's own line detail can sum a cent under the receivable
    #: record it belongs to, and a tax recomputed from the base can land a cent
    #: off the amount Sage stored. Neither is recoverable from the data, and a
    #: check that calls a cent a failure trains people to ignore it — which is
    #: the real cost, because the next failure is a whole missing invoice.
    ROUNDING_BAND = 0.05

    def _verdict(self, balance) -> tuple:
        if abs(balance) < 0.005:
            return "OK", True
        if abs(balance) < self.ROUNDING_BAND:
            return _("within source rounding — see the import report"), True
        return _("NOT ZERO — a document failed to import"), False

    def _check_trial_balance(self) -> tuple:
        """Odoo against Sage, per account, over every replayed year.

        Comparing cumulative balances alone would be wrong for half the
        chart. A profit and loss account restarts at zero every year, so
        Sage's figure for it describes ONE year while Odoo's running total
        describes all of them — on a three-year replay that reads as a
        threefold discrepancy on every P&L account and hides whatever the
        real errors are. The comparison is therefore per generation: each
        year's movement in Odoo against the same year's movement in Sage,
        which is the right test for both halves of the chart. The closing
        balance sheet is then checked once on top, because a year's movement
        can be right in every year while the opening balance underneath it
        is wrong.

        Sage's own `dYtc` summary field is deliberately not used as the
        expected figure: it describes the file's "as at" moment, which is
        not necessarily its last entry, and it disagrees with the ledger on
        a real file.
        """
        self.ensure_one()
        start = self.history_start_date.strftime("%Y-%m-%d")
        report, ok = [], True

        with self.get_cursor() as cr:
            spans = [
                span for span in tools.generation_spans(cr)
                if span["start"] >= start
            ]
            movements = {
                span["header"]: (
                    span, tools.generation_movement(cr, span)
                )
                for span in spans
            }
            opening = {
                row["lId"]: round(row["dYts"], 2)
                for row in tools.query(
                    cr,
                    "select lId, dYts from taccount where cFunc in %s",
                    (tools.POSTABLE_FUNCS,),
                )
            }
            as_of = spans[0]["end"] if spans else start

        for header, (span, expected) in movements.items():
            actual = self._movement(span["start"], span["end"])
            drift = self._drift(expected, actual)
            if drift:
                ok = False
                report.append(
                    f"{span['start']}..{span['end']}: {len(drift)} accounts "
                    f"do not tie"
                )
                report += self._drift_lines(drift)
            else:
                report.append(
                    f"{span['start']}..{span['end']}: every account ties  OK"
                )

        # The closing balance sheet. Profit and loss is excluded: it is
        # covered year by year above, and its cumulative total across
        # several years is not a figure Sage holds anywhere.
        expected = {
            sage_id: natural for sage_id, natural in opening.items()
            if sage_id // 10_000_000 < 4
        }
        closing = dict(expected)
        current = movements.get(tools.GENERATIONS[0][0])
        if current:
            for sage_id, amount in current[1].items():
                if sage_id // 10_000_000 < 4:
                    closing[sage_id] = closing.get(sage_id, 0.0) + amount
        actual = self._movement(None, as_of, balance_sheet_only=True)
        drift = self._drift(closing, actual)
        if drift:
            ok = False
            report.append(
                f"Balance sheet at {as_of}: {len(drift)} accounts do not tie"
            )
            report += self._drift_lines(drift)
        else:
            report.append(f"Balance sheet at {as_of}: every account ties  OK")
        return ok, report

    def _movement(self, start, end, balance_sheet_only=False) -> dict:
        """Sage account number -> Odoo movement over a date range."""
        query = """select a.sage_account_id, coalesce(sum(l.balance), 0)
                     from account_move_line l
                     join account_move m on m.id = l.move_id
                     join account_account a on a.id = l.account_id
                    where m.state = 'posted' and m.company_id = %s
                      and a.sage_account_id != 0 and m.date <= %s"""
        args = [self.company_id.id, end]
        if start:
            query += " and m.date >= %s"
            args.append(start)
        if balance_sheet_only:
            query += " and a.sage_account_id < 40000000"
        query += " group by a.sage_account_id"
        self.env.cr.execute(query, args)
        return {row[0]: round(row[1], 2) for row in self.env.cr.fetchall()}

    def _drift(self, expected_natural: dict, actual: dict) -> list:
        """Accounts where Odoo and Sage disagree, debit-positive."""
        drift = []
        seen = set(expected_natural) | set(actual)
        for sage_id in seen:
            want = round(tools.signed_amount(
                sage_id, round(expected_natural.get(sage_id, 0.0), 2)
            ), 2)
            got = actual.get(sage_id, 0.0)
            if abs(want - got) > 0.01:
                drift.append((sage_id, want, got))
        drift.sort(key=lambda row: -abs(row[1] - row[2]))
        return drift

    def _drift_lines(self, drift: list, limit: int = 10) -> list:
        lines = [
            f"  {sage_id}: Sage {want:,.2f}, Odoo {got:,.2f} "
            f"(off by {got - want:,.2f})"
            for sage_id, want, got in drift[:limit]
        ]
        if len(drift) > limit:
            lines.append(f"  … and {len(drift) - limit} more")
        return lines

    def _posted_balance(self, account, partner_less=False) -> float:
        self.env.cr.execute(
            """select coalesce(sum(l.balance), 0)
                 from account_move_line l
                 join account_move m on m.id = l.move_id
                where l.account_id = %s and m.state = 'posted'"""
            + (" and l.partner_id is null" if partner_less else ""),
            (account.id,),
        )
        return self.env.cr.fetchone()[0]
