"""Unified GL import pipeline: OJDT/JDT1 as single source of truth.

Imports all SAP journal entries as account.move records. For enrichable
transaction types (invoices, bills, credit memos), builds proper typed
moves with product lines, taxes, COGS, and currency handling. All other
types (payments, manual JEs, inventory, period closes) are imported as
generic journal entries from JDT1 lines.
"""

import logging

from odoo import api, models
from odoo.fields import Command

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData
from odoo.addons.etl_framework.utils import post_lock
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

_logger = logging.getLogger(__name__)

# OJDT.transtype (text in PG dump) -> config
_TRANSTYPE_CONFIG = {
    "13": {
        "sap_table": "oinv",
        "line_table": "inv1",
        "move_type": "out_invoice",
        "refund_type": "out_refund",
    },
    "14": {
        "sap_table": "orin",
        "line_table": "rin1",
        "move_type": "out_refund",
        "refund_type": "out_invoice",
    },
    "18": {
        "sap_table": "opch",
        "line_table": "pch1",
        "move_type": "in_invoice",
        "refund_type": "in_refund",
    },
    "19": {
        "sap_table": "orpc",
        "line_table": "rpc1",
        "move_type": "in_refund",
        "refund_type": "in_invoice",
    },
    "24": {"sap_table": "orct"},
    "46": {"sap_table": "ovpm"},
}

_ENRICHABLE_TYPES = {"13", "14", "18", "19"}


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
        "product.product.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=50,
    max_workers=8,
)
class AccountMoveJDT1Importer(models.AbstractModel):
    _name = "account.move.jdt1.importer"
    _description = "SAP Unified Journal Entry Importer (OJDT/JDT1)"
    _inherit = "sap.account.move.importer.mixin"

    def _get_order_line_link_config(self):
        return None

    def _get_order_line_link_vals(self, order_line_id):
        return {}

    @api.model
    def _get_cogs_line_vals(self, row, lookups):
        """Skip COGS lines — JDT1 already includes inventory/COGS entries
        as separate transactions (delivery/goods receipt JEs)."""
        return []

    # ----------------------------------------------------------------
    # Extract
    # ----------------------------------------------------------------

    @ETL.extract("ojdt")
    def extract_journal_entries(self, ctx: ETLContext) -> ChunkableData:
        """Extract OJDT headers with embedded JDT1 lines and enrichment data.

        For enrichable types, source doc header and product lines are
        embedded directly into each record — only per-record data
        travels with each chunk.
        """
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

        # Fetch all JDT1 lines
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

        # Embed JDT1 lines into headers
        lines_by_transid = {}
        for line in all_lines:
            lines_by_transid.setdefault(line["transid"], []).append(line)
        for header in headers:
            header["_lines"] = lines_by_transid.get(header["transid"], [])

        # Embed enrichment data per record
        enrichable = [
            h for h in headers if h["transtype"] in _ENRICHABLE_TYPES
        ]
        if enrichable:
            self._embed_enrichment(ctx.cr, enrichable)

        # Sort by first partner to reduce multiprocessing conflicts
        headers.sort(
            key=lambda h: (h["_lines"][0]["shortname"] or "")
            if h["_lines"]
            else ""
        )

        _logger.info(
            "Extracted %d journal entries (%d lines, %d enrichable), "
            "skipped %d already imported.",
            len(headers), len(all_lines), len(enrichable),
            len(already_imported),
        )
        return ChunkableData(records=headers, context={})

    @ETL.extract("oact")
    def extract_lookups(self, ctx: ETLContext):
        """Extract lightweight lookup dicts for transform."""
        partners = ctx.env["res.partner"].search_read(
            [("sap_card_code", "!=", False), ("active", "in", [True, False])],
            ["id", "sap_card_code"],
        )
        partners_dict = {p["sap_card_code"]: p["id"] for p in partners}

        lookups = self._build_lookups()

        misc_journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("code", "=", "MISC")], limit=1,
        )
        if not misc_journal:
            misc_journal = ctx.env["account.journal"].search(
                [("type", "=", "general")], limit=1,
            )

        return {
            "partners": partners_dict,
            "lookups": lookups,
            "misc_journal_id": misc_journal.id if misc_journal else False,
        }

    def _embed_enrichment(self, sap_cr, enrichable_headers):
        """Fetch source doc headers + lines and embed into each OJDT header."""
        by_type = {}
        for h in enrichable_headers:
            by_type.setdefault(h["transtype"], []).append(h)

        for transtype, type_headers in by_type.items():
            config = _TRANSTYPE_CONFIG[transtype]
            header_table = config["sap_table"]
            line_table = config["line_table"]

            docentries = tuple(h["createdby"] for h in type_headers)
            sap_cr.execute(
                f"SELECT * FROM {header_table} WHERE docentry IN %s",
                (docentries,),
            )
            doc_headers = {
                row["docentry"]: row for row in sap_cr.dictfetchall()
            }

            lines_by_doc = {}
            if doc_headers:
                docs_for_lines = [{"docentry": de} for de in doc_headers]
                lines = self._get_lines(sap_cr, line_table, docs_for_lines)
                for line in lines:
                    lines_by_doc.setdefault(
                        line["docentry"], [],
                    ).append(line)

            for h in type_headers:
                de = h["createdby"]
                h["_doc"] = doc_headers.get(de)
                h["_doc_lines"] = lines_by_doc.get(de, [])

            _logger.info(
                "Embedded %d/%d %s docs for enrichment.",
                len(doc_headers), len(type_headers), header_table,
            )

    # ----------------------------------------------------------------
    # Transform
    # ----------------------------------------------------------------

    @ETL.transform()
    def transform_journal_entries(self, ctx: ETLContext, extracted):
        """Transform OJDT headers into account.move create vals.

        Enrichable types get typed moves with product lines.
        Falls back to generic JDT1 lines otherwise.
        """
        data = extracted["extract_journal_entries"]
        headers = (
            data.records if hasattr(data, "records")
            else data.get("records", [])
        )

        if not headers:
            return {"move_vals": [], "lookups": {}}

        meta = extracted.get("extract_lookups") or {}
        if not meta:
            _logger.error("No lookups data — extract_lookups may have failed.")
            return {"move_vals": [], "lookups": {}}
        partners_dict = meta["partners"]
        lookups = meta["lookups"]
        misc_journal_id = meta["misc_journal_id"]

        accounts_dict = lookups["accounts"]
        currencies_dict = lookups["currencies"]
        company_currency_id = lookups["company_currency_id"]

        move_vals_list = []
        enriched_count = 0
        generic_count = 0

        for header in headers:
            jdt1_lines = header.pop("_lines", [])
            doc = header.pop("_doc", None)
            doc_lines = header.pop("_doc_lines", [])

            if not jdt1_lines:
                continue

            transtype = header["transtype"]
            config = _TRANSTYPE_CONFIG.get(transtype)

            # Try enrichment
            move_vals = None
            if transtype in _ENRICHABLE_TYPES and config and doc:
                move_vals = self._build_enriched_vals(
                    header, doc, doc_lines, config, partners_dict, lookups,
                )
                if move_vals:
                    # Carry JDT1 account expectations for post-posting fix
                    move_vals["_jdt1_accounts"] = [
                        {
                            "acct_formatcode": (
                                jl.get("acct_formatcode") or ""
                            ).strip(),
                            "debit": float(jl.get("debit") or 0),
                            "credit": float(jl.get("credit") or 0),
                        }
                        for jl in jdt1_lines
                        if float(jl.get("debit") or 0)
                        or float(jl.get("credit") or 0)
                    ]
                    enriched_count += 1

            # Fall back to generic JDT1 entry
            if not move_vals:
                move_vals = self._build_generic_entry_vals(
                    header, jdt1_lines, accounts_dict, partners_dict,
                    currencies_dict, company_currency_id, misc_journal_id,
                )
                if move_vals:
                    generic_count += 1

            if move_vals:
                move_vals_list.append(move_vals)

        _logger.info(
            "Transformed %d journal entries (%d enriched, %d generic).",
            len(move_vals_list), enriched_count, generic_count,
        )
        return {"move_vals": move_vals_list, "lookups": lookups}

    # ----------------------------------------------------------------
    # Load
    # ----------------------------------------------------------------

    @ETL.load()
    def load_journal_entries(self, ctx: ETLContext, transformed):
        """Create and post account.move records."""
        data = transformed.get("transform_journal_entries", {})
        move_vals_list = data.get("move_vals", [])
        lookups = data.get("lookups", {})

        if not move_vals_list:
            return

        self._create_pending_currency_rates(lookups)

        moves = ctx.env["account.move"]
        for vals in move_vals_list:
            ref = (
                f"{vals.get('sap_table', 'ojdt')}#"
                f"{vals.get('sap_docentry', '?')}"
            )
            with ctx.skippable(ref):
                moves |= ctx.env["account.move"].create(vals)

        if not moves:
            return

        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, ctx.env["account.move"])
            by_journal[move.journal_id.id] |= move

        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(
                        f"post {move.sap_table}#{move.sap_docentry}"
                    ):
                        move.with_context(
                            skip_cogs_generation=True,
                        ).action_post()

        _logger.info("Created and posted %d journal entries.", len(moves))

    # ----------------------------------------------------------------
    # Enrichment builder
    # ----------------------------------------------------------------

    def _build_enriched_vals(self, header, doc, doc_lines, config,
                             partners_dict, lookups):
        """Build enriched move vals from embedded source doc data."""
        partner_id = partners_dict.get(doc.get("cardcode"))
        if not partner_id:
            return None

        lines_dict = {doc["docentry"]: doc_lines}

        vals = self._get_move_vals(
            doc,
            partner_id,
            lines_dict,
            config["sap_table"],
            config["line_table"],
            {},  # order_lines_dict — later phase
            lookups,
        )

        if not vals.get("line_ids"):
            return None

        self._normalize_move_type(
            vals, config["move_type"], config["refund_type"],
        )
        return vals

    # ----------------------------------------------------------------
    # Generic JDT1 builder
    # ----------------------------------------------------------------

    @staticmethod
    def _build_generic_entry_vals(header, jdt1_lines, accounts_dict,
                                  partners_dict, currencies_dict,
                                  company_currency_id, misc_journal_id):
        """Build a generic move_type='entry' from JDT1 lines."""
        line_commands = []
        partner_id = False

        for jdt1 in jdt1_lines:
            line_vals = AccountMoveJDT1Importer._build_jdt1_line_vals(
                jdt1, accounts_dict, partners_dict,
                currencies_dict, company_currency_id,
            )
            if line_vals:
                line_commands.append(Command.create(line_vals))
                if not partner_id and line_vals.get("partner_id"):
                    partner_id = line_vals["partner_id"]

        if not line_commands:
            return None

        # Fix rounding imbalance
        total_debit = sum(c[2].get("debit", 0) for c in line_commands)
        total_credit = sum(c[2].get("credit", 0) for c in line_commands)
        diff = round(total_debit - total_credit, 2)
        if diff != 0 and abs(diff) <= 0.05:
            if diff > 0:
                target = max(
                    line_commands, key=lambda c: c[2].get("debit", 0),
                )
                target[2]["debit"] = round(target[2]["debit"] - diff, 2)
            else:
                target = max(
                    line_commands, key=lambda c: c[2].get("credit", 0),
                )
                target[2]["credit"] = round(
                    target[2]["credit"] + diff, 2,
                )

        move_vals = {
            "move_type": "entry",
            "date": (
                fix_tz(header["refdate"]) if header["refdate"] else False
            ),
            "ref": header.get("memo") or "",
            "journal_id": misc_journal_id,
            "line_ids": line_commands,
            "sap_docentry": header["transid"],
            "sap_docnum": header.get("docnum") or 0,
            "sap_table": "ojdt",
        }
        if partner_id:
            move_vals["partner_id"] = partner_id

        return move_vals

    # ----------------------------------------------------------------
    # JDT1 line builder
    # ----------------------------------------------------------------

    @staticmethod
    def _build_jdt1_line_vals(jdt1, accounts_dict, partners_dict,
                              currencies_dict, company_currency_id):
        """Build account.move.line vals from a single JDT1 row."""
        debit = float(jdt1.get("debit") or 0)
        credit = float(jdt1.get("credit") or 0)

        if debit == 0 and credit == 0:
            return None

        acct_formatcode = (jdt1.get("acct_formatcode") or "").strip()
        account_info = accounts_dict.get(acct_formatcode)
        if not account_info:
            _logger.warning(
                "Account not found for SAP code '%s' (transid=%s, line=%s)",
                acct_formatcode, jdt1.get("transid"), jdt1.get("line_id"),
            )
            return None
        account_id, _account_type = account_info

        shortname = (jdt1.get("shortname") or "").strip()
        partner_id = partners_dict.get(shortname) if shortname else False

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

    # ----------------------------------------------------------------
    # Already-imported detection
    # ----------------------------------------------------------------

    @staticmethod
    def _get_already_imported(ctx):
        """Get set of OJDT transids already imported."""
        ctx.env.cr.execute(
            """
            SELECT sap_docentry FROM account_move
             WHERE sap_table = 'ojdt'
               AND sap_docentry IS NOT NULL
               AND sap_docentry != 0
            """
        )
        ojdt_transids = {row[0] for row in ctx.env.cr.fetchall()}

        # Enriched moves: sap_table = oinv/opch/etc.
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
                        "SELECT transid FROM ojdt"
                        " WHERE createdby = %s AND transtype = %s",
                        (sap_docentry, transtype),
                    )
                    for row in ctx.cr.fetchall():
                        ojdt_transids.add(row[0])

        return ojdt_transids
