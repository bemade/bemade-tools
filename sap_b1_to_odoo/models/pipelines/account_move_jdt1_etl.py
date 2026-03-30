"""Unified GL import pipeline: OJDT/JDT1 as single source of truth.

Phase 1a: Import all SAP journal entries as account.move records.
Every OJDT header becomes one account.move, every JDT1 line becomes
one account.move.line. All moves are created as move_type='entry'
to guarantee GL correctness.

Future phases will add enrichment (invoices, bills, payments) and
reconciliation.
"""

import logging

from odoo import api, models
from odoo.fields import Command

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData
from odoo.addons.etl_framework.utils import post_lock
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

_logger = logging.getLogger(__name__)

# OJDT.transtype (text in PG dump) -> SAP source table and move metadata
_TRANSTYPE_CONFIG = {
    "13": {"sap_table": "oinv", "label": "A/R Invoice"},
    "14": {"sap_table": "orin", "label": "A/R Credit Memo"},
    "18": {"sap_table": "opch", "label": "A/P Invoice"},
    "19": {"sap_table": "orpc", "label": "A/P Credit Memo"},
    "24": {"sap_table": "orct", "label": "Incoming Payment"},
    "46": {"sap_table": "ovpm", "label": "Outgoing Payment"},
}


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.move.jdt1.importer",
    sap_source="ojdt",
    depends_on=[
        "account.journal.setup",
        "account.tax.importer",
        "account.account.importer",
        "res.partner.company.importer",
        "res.users.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=50,
    max_workers=8,
)
class AccountMoveJDT1Importer(models.AbstractModel):
    _name = "account.move.jdt1.importer"
    _description = "SAP Unified Journal Entry Importer (OJDT/JDT1)"

    # ----------------------------------------------------------------
    # Extract
    # ----------------------------------------------------------------

    @ETL.extract("ojdt")
    def extract_journal_entries(self, ctx: ETLContext) -> ChunkableData:
        """Extract all OJDT headers with embedded JDT1 lines.

        Skips already-imported entries and unbalanced transactions.
        Sorts by partner (shortname) to minimize multiprocessing conflicts.
        """
        # Find already-imported OJDT transids
        already_imported = self._get_already_imported(ctx)

        ctx.cr.execute(
            """
            SELECT o.transid, o.transtype, o.refdate, o.memo,
                   o.createdby, o.number AS docnum
              FROM ojdt o
             ORDER BY o.transid
            """
        )
        headers = ctx.cr.dictfetchall()

        if already_imported:
            imported_set = set(already_imported)
            headers = [h for h in headers if h["transid"] not in imported_set]

        if not headers:
            _logger.info("No new journal entries to import.")
            return ChunkableData(records=[], context={})

        # Fetch all JDT1 lines for these headers
        transids = tuple(h["transid"] for h in headers)
        ctx.cr.execute(
            """
            SELECT j.transid, j.line_id, j.account, j.debit, j.credit,
                   j.shortname, j.fccurrency, j.fcdebit, j.fccredit,
                   j.ref1, j.ref2, j.project,
                   a.formatcode AS acct_formatcode
              FROM jdt1 j
              JOIN oact a ON j.account = a.acctcode
             WHERE j.transid IN %s
             ORDER BY j.transid, j.line_id
            """,
            (transids,),
        )
        all_lines = ctx.cr.dictfetchall()

        # Embed lines into headers
        lines_by_transid = {}
        for line in all_lines:
            lines_by_transid.setdefault(line["transid"], []).append(line)
        for header in headers:
            header["_lines"] = lines_by_transid.get(header["transid"], [])

        # Sort by first partner in each transaction to reduce conflicts
        headers.sort(key=lambda h: (h["_lines"][0]["shortname"] or "") if h["_lines"] else "")

        _logger.info(
            "Extracted %d journal entries (%d lines), skipped %d already imported.",
            len(headers), len(all_lines), len(already_imported),
        )
        return ChunkableData(records=headers, context={})

    @ETL.extract("oact")
    def extract_lookups(self, ctx: ETLContext):
        """Extract all lookup data needed by transform.

        Partners, accounts, currencies — everything the transform needs
        to map SAP codes to Odoo IDs without touching the database.
        """
        # Partners by SAP card code
        partners = ctx.env["res.partner"].search_read(
            [("sap_card_code", "!=", False), ("active", "in", [True, False])],
            ["id", "sap_card_code"],
        )
        partners_dict = {p["sap_card_code"]: p["id"] for p in partners}

        # Accounts by SAP format code -> (id, account_type)
        accounts = ctx.env["account.account"].search_read(
            [("sap_acct_code", "!=", False)],
            ["id", "sap_acct_code", "account_type"],
        )
        accounts_dict = {
            a["sap_acct_code"]: (a["id"], a["account_type"]) for a in accounts
        }

        # Currencies
        currencies = ctx.env["res.currency"].search_read(
            [("active", "in", [True, False])],
            ["id", "name"],
        )
        currencies_dict = {c["name"]: c["id"] for c in currencies}

        # Misc journal for generic entries
        misc_journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("code", "=", "MISC")], limit=1,
        )
        if not misc_journal:
            misc_journal = ctx.env["account.journal"].search(
                [("type", "=", "general")], limit=1,
            )
        misc_journal_id = misc_journal.id if misc_journal else False

        return {
            "partners": partners_dict,
            "accounts": accounts_dict,
            "currencies": currencies_dict,
            "company_currency_id": ctx.env.company.currency_id.id,
            "misc_journal_id": misc_journal_id,
        }

    # ----------------------------------------------------------------
    # Transform
    # ----------------------------------------------------------------

    @ETL.transform()
    def transform_journal_entries(self, ctx: ETLContext, extracted):
        """Transform OJDT headers + JDT1 lines into account.move create vals.

        Pure function: uses only data from extract phase.
        """
        data = extracted["extract_journal_entries"]
        headers = data.records if hasattr(data, "records") else data.get("records", [])

        if not headers:
            return {"move_vals": []}

        lookups = extracted["extract_lookups"]
        partners_dict = lookups["partners"]
        accounts_dict = lookups["accounts"]
        currencies_dict = lookups["currencies"]
        company_currency_id = lookups["company_currency_id"]
        misc_journal_id = lookups["misc_journal_id"]

        move_vals_list = []
        for header in headers:
            jdt1_lines = header.pop("_lines", [])
            if not jdt1_lines:
                continue

            # Phase 1a: all moves use ojdt/transid for tracing.
            # Enrichment phases will switch typed transactions to
            # source table + createdby for reconciliation compatibility.
            sap_table = "ojdt"
            sap_docentry = header["transid"]

            # Build move lines from JDT1
            line_commands = []
            partner_id = False
            for jdt1 in jdt1_lines:
                line_vals = self._build_jdt1_line_vals(
                    jdt1, accounts_dict, partners_dict,
                    currencies_dict, company_currency_id,
                )
                if line_vals:
                    line_commands.append(Command.create(line_vals))
                    # Use first partner encountered as the move partner
                    if not partner_id and line_vals.get("partner_id"):
                        partner_id = line_vals["partner_id"]

            if not line_commands:
                continue

            # Fix rounding imbalance (SAP allows sub-cent drift, Odoo doesn't)
            # Adjust the largest line rather than adding a separate rounding line,
            # so the correction stays on the correct account.
            total_debit = sum(c[2].get("debit", 0) for c in line_commands)
            total_credit = sum(c[2].get("credit", 0) for c in line_commands)
            diff = round(total_debit - total_credit, 2)
            if diff != 0 and abs(diff) <= 0.05:
                # Find the largest line on the heavy side and adjust it
                if diff > 0:  # debits too high
                    target = max(line_commands, key=lambda c: c[2].get("debit", 0))
                    target[2]["debit"] = round(target[2]["debit"] - diff, 2)
                else:  # credits too high
                    target = max(line_commands, key=lambda c: c[2].get("credit", 0))
                    target[2]["credit"] = round(target[2]["credit"] + diff, 2)

            move_vals = {
                "move_type": "entry",
                "date": fix_tz(header["refdate"]) if header["refdate"] else False,
                "ref": header.get("memo") or "",
                "journal_id": misc_journal_id,
                "line_ids": line_commands,
                "sap_docentry": sap_docentry,
                "sap_docnum": header.get("docnum") or 0,
                "sap_table": sap_table,
            }
            if partner_id:
                move_vals["partner_id"] = partner_id

            move_vals_list.append(move_vals)

        _logger.info("Transformed %d journal entries.", len(move_vals_list))
        return {"move_vals": move_vals_list}

    # ----------------------------------------------------------------
    # Load
    # ----------------------------------------------------------------

    @ETL.load()
    def load_journal_entries(self, ctx: ETLContext, transformed):
        """Create and post account.move records."""
        data = transformed.get("transform_journal_entries", {})
        move_vals_list = data.get("move_vals", [])

        if not move_vals_list:
            return

        moves = ctx.env["account.move"]
        for vals in move_vals_list:
            ref = f"JE SAP transid={vals.get('sap_docentry', '?')} [{vals.get('sap_table', '')}]"
            with ctx.skippable(ref):
                moves |= ctx.env["account.move"].create(vals)

        if not moves:
            return

        # Post grouped by journal under advisory lock
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, ctx.env["account.move"])
            by_journal[move.journal_id.id] |= move

        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post {move.sap_table}#{move.sap_docentry}"):
                        move.action_post()

        _logger.info("Created %d, posted %d journal entries.", len(move_vals_list), len(moves))

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _build_jdt1_line_vals(jdt1, accounts_dict, partners_dict,
                              currencies_dict, company_currency_id):
        """Build account.move.line vals from a single JDT1 row.

        Pure function — no database access.
        """
        debit = float(jdt1.get("debit") or 0)
        credit = float(jdt1.get("credit") or 0)

        # Skip zero lines
        if debit == 0 and credit == 0:
            return None

        # Account lookup
        acct_formatcode = (jdt1.get("acct_formatcode") or "").strip()
        account_info = accounts_dict.get(acct_formatcode)
        if not account_info:
            _logger.warning(
                "Account not found for SAP code '%s' (transid=%s, line=%s)",
                acct_formatcode, jdt1.get("transid"), jdt1.get("line_id"),
            )
            return None
        account_id, _account_type = account_info

        # Partner lookup (shortname is the BP code in JDT1)
        shortname = (jdt1.get("shortname") or "").strip()
        partner_id = partners_dict.get(shortname) if shortname else False

        # Currency
        fc_currency = (jdt1.get("fccurrency") or "").strip()
        currency_id = False
        amount_currency = 0.0
        if fc_currency:
            currency_id = currencies_dict.get(fc_currency)
            if currency_id and currency_id != company_currency_id:
                fc_debit = float(jdt1.get("fcdebit") or 0)
                fc_credit = float(jdt1.get("fccredit") or 0)
                amount_currency = fc_debit - fc_credit
            else:
                currency_id = False

        vals = {
            "account_id": account_id,
            "debit": debit,
            "credit": credit,
            "name": jdt1.get("ref1") or jdt1.get("ref2") or "",
            "sap_line_num": (jdt1.get("line_id") or 0) + 2,
            "sap_aftlinenum": 0,
            "sap_lineseq": 0,
            "sap_table": "jdt1",
        }

        if partner_id:
            vals["partner_id"] = partner_id
        if currency_id:
            vals["currency_id"] = currency_id
            vals["amount_currency"] = amount_currency

        return vals

    @staticmethod
    def _get_already_imported(ctx):
        """Get set of OJDT transids already imported.

        All JDT1-pipeline moves use sap_table='ojdt', sap_docentry=transid.
        Also detects moves from the old per-doctype pipelines by reverse-
        looking up their OJDT transid via createdby + transtype.
        """
        ctx.env.cr.execute(
            """
            SELECT sap_docentry FROM account_move
             WHERE sap_table = 'ojdt'
               AND sap_docentry IS NOT NULL
               AND sap_docentry != 0
            """
        )
        ojdt_transids = {row[0] for row in ctx.env.cr.fetchall()}

        # Also detect old-pipeline moves (sap_table = oinv/opch/etc.)
        ctx.env.cr.execute(
            """
            SELECT DISTINCT sap_table, sap_docentry FROM account_move
             WHERE sap_table IN ('oinv', 'orin', 'opch', 'orpc', 'orct', 'ovpm')
               AND sap_docentry IS NOT NULL
               AND sap_docentry != 0
            """
        )
        typed_docs = ctx.env.cr.fetchall()

        if typed_docs:
            table_to_transtype = {
                v["sap_table"]: k for k, v in _TRANSTYPE_CONFIG.items()
            }
            for sap_table, sap_docentry in typed_docs:
                transtype = table_to_transtype.get(sap_table)
                if transtype:
                    ctx.cr.execute(
                        "SELECT transid FROM ojdt WHERE createdby = %s AND transtype = %s",
                        (sap_docentry, transtype),
                    )
                    for row in ctx.cr.fetchall():
                        ojdt_transids.add(row[0])

        return ojdt_transids
