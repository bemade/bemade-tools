"""Unified GL import pipeline: OJDT/JDT1 as single source of truth.

Pass 1: Import all SAP journal entries as move_type='entry' from JDT1
        lines. Guarantees GL correctness — debits/credits match SAP exactly.

Pass 2: Post-enrich typed transactions (invoices, bills, credit memos)
        by updating move_type, journal_id, and partner on receivable/payable
        lines via SQL. No financial recomputation — just metadata.
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
    "13": {"sap_table": "oinv", "move_type": "out_invoice", "journal_type": "sale"},
    "14": {"sap_table": "orin", "move_type": "out_refund", "journal_type": "sale"},
    "18": {"sap_table": "opch", "move_type": "in_invoice", "journal_type": "purchase"},
    "19": {"sap_table": "orpc", "move_type": "in_refund", "journal_type": "purchase"},
    "24": {"sap_table": "orct"},
    "46": {"sap_table": "ovpm"},
}


# =====================================================================
# Pass 1 — Import all JEs as generic entries
# =====================================================================

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

    # ----------------------------------------------------------------
    # Extract
    # ----------------------------------------------------------------

    @ETL.extract("ojdt")
    def extract_journal_entries(self, ctx: ETLContext) -> ChunkableData:
        """Extract all OJDT headers with embedded JDT1 lines."""
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

        # Embed lines into headers
        lines_by_transid = {}
        for line in all_lines:
            lines_by_transid.setdefault(line["transid"], []).append(line)
        for header in headers:
            header["_lines"] = lines_by_transid.get(header["transid"], [])

        # Sort by first partner to reduce multiprocessing conflicts
        headers.sort(
            key=lambda h: (h["_lines"][0]["shortname"] or "")
            if h["_lines"]
            else ""
        )

        _logger.info(
            "Extracted %d journal entries (%d lines), "
            "skipped %d already imported.",
            len(headers), len(all_lines), len(already_imported),
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

        accounts = ctx.env["account.account"].search_read(
            [("sap_acct_code", "!=", False)],
            ["id", "sap_acct_code", "account_type"],
        )
        accounts_dict = {
            a["sap_acct_code"]: (a["id"], a["account_type"]) for a in accounts
        }

        currencies = ctx.env["res.currency"].search_read(
            [("active", "in", [True, False])],
            ["id", "name"],
        )
        currencies_dict = {c["name"]: c["id"] for c in currencies}

        misc_journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("code", "=", "MISC")], limit=1,
        )
        if not misc_journal:
            misc_journal = ctx.env["account.journal"].search(
                [("type", "=", "general")], limit=1,
            )

        return {
            "partners": partners_dict,
            "accounts": accounts_dict,
            "currencies": currencies_dict,
            "company_currency_id": ctx.env.company.currency_id.id,
            "misc_journal_id": misc_journal.id if misc_journal else False,
        }

    # ----------------------------------------------------------------
    # Transform
    # ----------------------------------------------------------------

    @ETL.transform()
    def transform_journal_entries(self, ctx: ETLContext, extracted):
        """Transform OJDT headers + JDT1 lines into account.move create vals.

        All moves are created as move_type='entry'. Post-enrichment
        updates move_type for typed transactions after posting.
        """
        data = extracted["extract_journal_entries"]
        headers = (
            data.records if hasattr(data, "records")
            else data.get("records", [])
        )

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

            line_commands = []
            partner_id = False
            for jdt1 in jdt1_lines:
                line_vals = self._build_jdt1_line_vals(
                    jdt1, accounts_dict, partners_dict,
                    currencies_dict, company_currency_id,
                )
                if line_vals:
                    line_commands.append(Command.create(line_vals))
                    if not partner_id and line_vals.get("partner_id"):
                        partner_id = line_vals["partner_id"]

            if not line_commands:
                continue

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
            ref = f"ojdt#{vals.get('sap_docentry', '?')}"
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
                    with ctx.skippable(f"post ojdt#{move.sap_docentry}"):
                        move.action_post()

        _logger.info(
            "Created and posted %d journal entries.", len(moves),
        )

    # ----------------------------------------------------------------
    # Helpers
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
        return {row[0] for row in ctx.env.cr.fetchall()}


# =====================================================================
# Pass 2 — Post-enrich: update move_type, journal, partner on lines
# =====================================================================

@ETL.pipeline(
    target_model="account.move",
    importer_name="account.move.jdt1.enricher",
    sap_source="ojdt",
    depends_on=["account.internal.reconciliation"],
    allow_multiprocessing=False,
)
class AccountMoveJDT1Enricher(models.AbstractModel):
    _name = "account.move.jdt1.enricher"
    _description = "Post-enrich JDT1 moves: set move_type, journal, partner"

    @ETL.extract("ojdt")
    def extract_enrichment_data(self, ctx: ETLContext):
        """Extract transtype→transid mapping and Odoo journal/partner lookups."""
        # Map OJDT transid → (transtype, createdby) for enrichable types
        ctx.cr.execute(
            """
            SELECT transid, transtype, createdby
              FROM ojdt
             WHERE transtype IN ('13', '14', '18', '19')
            """
        )
        ojdt_map = {}
        createdby_by_type = {}
        for row in ctx.cr.fetchall():
            transid, transtype, createdby = row
            ojdt_map[transid] = {
                "transtype": transtype,
                "createdby": createdby,
            }
            createdby_by_type.setdefault(transtype, {})[createdby] = transid

        # Get cardcode (partner) from source doc headers
        partner_by_transid = {}
        for transtype, config in _TRANSTYPE_CONFIG.items():
            if transtype not in ("13", "14", "18", "19"):
                continue
            header_table = config["sap_table"]
            entries = createdby_by_type.get(transtype, {})
            if not entries:
                continue
            docentries = tuple(entries.keys())
            ctx.cr.execute(
                f"SELECT docentry, cardcode FROM {header_table}"
                f" WHERE docentry IN %s",
                (docentries,),
            )
            for docentry, cardcode in ctx.cr.fetchall():
                transid = entries[docentry]
                partner_by_transid[transid] = cardcode

        # Odoo lookups
        journals = ctx.env["account.journal"].search_read(
            [("type", "in", ["sale", "purchase"])],
            ["id", "type"],
        )
        journal_by_type = {}
        for j in journals:
            journal_by_type.setdefault(j["type"], j["id"])

        partners = ctx.env["res.partner"].search_read(
            [("sap_card_code", "!=", False), ("active", "in", [True, False])],
            ["id", "sap_card_code"],
        )
        partners_dict = {p["sap_card_code"]: p["id"] for p in partners}

        return {
            "ojdt_map": ojdt_map,
            "partner_by_transid": partner_by_transid,
            "journal_by_type": journal_by_type,
            "partners_dict": partners_dict,
        }

    @ETL.transform()
    def transform_enrichment(self, ctx: ETLContext, extracted):
        """Build SQL update batches."""
        data = extracted["extract_enrichment_data"]
        ojdt_map = data["ojdt_map"]
        partner_by_transid = data["partner_by_transid"]
        journal_by_type = data["journal_by_type"]
        partners_dict = data["partners_dict"]

        return {
            "ojdt_map": ojdt_map,
            "partner_by_transid": partner_by_transid,
            "journal_by_type": journal_by_type,
            "partners_dict": partners_dict,
        }

    @ETL.load()
    def load_enrichment(self, ctx: ETLContext, transformed):
        """Update move_type, journal_id, and partner on lines via SQL."""
        data = transformed.get("transform_enrichment", {})
        ojdt_map = data.get("ojdt_map", {})
        partner_by_transid = data.get("partner_by_transid", {})
        journal_by_type = data.get("journal_by_type", {})
        partners_dict = data.get("partners_dict", {})

        if not ojdt_map:
            _logger.info("[Enricher] No enrichable moves found.")
            return

        # Get all enrichable moves from Odoo
        transids = list(ojdt_map.keys())
        ctx.env.cr.execute(
            """
            SELECT id, sap_docentry FROM account_move
             WHERE sap_table = 'ojdt'
               AND sap_docentry IN %s
               AND state = 'posted'
            """,
            (tuple(transids),),
        )
        move_rows = ctx.env.cr.fetchall()
        move_id_by_transid = {row[1]: row[0] for row in move_rows}

        _logger.info(
            "[Enricher] Found %d posted moves to enrich (of %d enrichable).",
            len(move_id_by_transid), len(ojdt_map),
        )

        # Batch update move_type and journal_id
        updated = 0
        for transid, move_id in move_id_by_transid.items():
            info = ojdt_map.get(transid)
            if not info:
                continue
            config = _TRANSTYPE_CONFIG.get(info["transtype"])
            if not config or "move_type" not in config:
                continue

            move_type = config["move_type"]
            journal_type = config["journal_type"]
            journal_id = journal_by_type.get(journal_type)
            if not journal_id:
                continue

            # Determine partner from source doc
            cardcode = partner_by_transid.get(transid)
            partner_id = partners_dict.get(cardcode) if cardcode else None

            # Update move header
            update_vals = {
                "move_type": move_type,
                "journal_id": journal_id,
            }
            if partner_id:
                update_vals["partner_id"] = partner_id

            set_clause = ", ".join(
                f"{k} = %s" for k in update_vals
            )
            ctx.env.cr.execute(
                f"UPDATE account_move SET {set_clause} WHERE id = %s",
                list(update_vals.values()) + [move_id],
            )

            # Set partner on receivable/payable lines
            if partner_id:
                ctx.env.cr.execute(
                    """
                    UPDATE account_move_line
                       SET partner_id = %s
                      FROM account_account aa
                     WHERE account_move_line.account_id = aa.id
                       AND account_move_line.move_id = %s
                       AND aa.account_type IN (
                           'asset_receivable', 'liability_payable'
                       )
                    """,
                    (partner_id, move_id),
                )

            updated += 1

        # Trigger recomputation of all stored fields that depend on
        # move_type, journal_id, partner_id — modified() cascades through
        # the full dependency graph.
        if move_id_by_transid:
            all_move_ids = list(move_id_by_transid.values())
            ctx.env.invalidate_all()
            moves = ctx.env["account.move"].browse(all_move_ids)
            _logger.info(
                "[Enricher] Triggering recomputation on %d moves...",
                len(moves),
            )
            moves.modified(["move_type", "journal_id", "partner_id"])
            ctx.env.flush_all()

        _logger.info(
            "[Enricher] Updated %d moves (move_type, journal, partner).",
            updated,
        )
