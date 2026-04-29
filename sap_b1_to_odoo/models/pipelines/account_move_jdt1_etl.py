"""Unified GL import pipeline: OJDT/JDT1 as single source of truth.

Imports all SAP journal entries as account.move records. For enrichable
transaction types (invoices, bills, credit memos), builds proper typed
moves with product lines, taxes, and currency handling. All other types
are imported as generic journal entries from JDT1 lines.

For enriched moves, three GL-correction mechanisms ensure accuracy:
1. Companion entries: move_type='entry' for JDT1 lines not representable
   on typed moves (COGS, inventory, freight, price variance).
2. Pre-posting tax fix: correct Odoo's percentage-computed tax amounts
   to match JDT1 exact amounts, rebalance payment_term.
3. Post-posting AR/AP fix: correct payment_term account from JDT1.
"""

import logging
from collections import defaultdict

from odoo import api, models
from odoo.fields import Command

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData
from odoo.addons.etl_framework.utils import post_lock
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

_logger = logging.getLogger(__name__)

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


def _fix_taxes_pre_posting_sap(cr, moves_tax_data, tax_account_ids=None):
    """Fix tax line amounts on draft enriched moves to match JDT1 truth.

    Odoo computes tax from percentage * subtotal, which may differ from
    SAP's exact JDT1 amounts. Corrects the difference and rebalances
    the payment_term line to keep the move balanced.

    When computing what's already on a tax account, includes ALL display
    types (tax, product, etc.) — not just tax lines. This prevents
    doubling when a product line already posts to a tax account (e.g.,
    tax payment bills).

    Args:
        cr: Database cursor.
        moves_tax_data: {move_id: {"tax_amounts": [{account_id, debit, credit}],
            "move_type": str}}
        tax_account_ids: Set of account IDs used on tax repartition lines.
    Returns:
        (tax_lines_fixed, payment_term_rebalanced) counts.
    """
    if not moves_tax_data:
        return 0, 0
    if tax_account_ids is None:
        tax_account_ids = set()

    move_ids = tuple(moves_tax_data.keys())

    cr.execute(
        """
        SELECT id, move_id, display_type, account_id,
               debit, credit, amount_currency, company_id, currency_id,
               journal_id, date
          FROM account_move_line
         WHERE move_id IN %s
           AND display_type IN ('tax', 'payment_term', 'product')
        """,
        (move_ids,),
    )
    rows = cr.fetchall()

    # Group by move_id and type.
    # Tax accounts: include ALL display types (tax + product) so we see
    # the true total and don't double amounts already on product lines.
    tax_lines_by_move = defaultdict(lambda: defaultdict(list))
    pt_lines_by_move = defaultdict(list)
    move_meta = {}  # {move_id: {company_id, currency_id, journal_id, date}}
    for (line_id, move_id, dtype, account_id, debit, credit, ac,
         company_id, currency_id, journal_id, date) in rows:
        if move_id not in move_meta:
            move_meta[move_id] = {
                "company_id": company_id,
                "currency_id": currency_id,
                "journal_id": journal_id,
                "date": date,
            }
        if dtype == "tax":
            tax_lines_by_move[move_id][account_id].append({
                "id": line_id,
                "debit": debit,
                "credit": credit,
                "balance": round(debit - credit, 2),
                "amount_currency": ac or 0.0,
            })
        elif dtype == "product" and account_id in tax_account_ids:
            tax_lines_by_move[move_id][account_id].append({
                "id": line_id,
                "debit": debit,
                "credit": credit,
                "balance": round(debit - credit, 2),
                "amount_currency": ac or 0.0,
            })
        elif dtype == "payment_term":
            pt_lines_by_move[move_id].append({
                "id": line_id,
                "debit": debit,
                "credit": credit,
                "balance": round(debit - credit, 2),
                "amount_currency": ac or 0.0,
            })

    fixed_tax = 0
    fixed_pt = 0
    updates = []
    inserts = []

    for move_id, data in moves_tax_data.items():
        tax_amounts = data["tax_amounts"]
        move_type = data.get("move_type", "entry")
        move_tax_lines = tax_lines_by_move.get(move_id, {})

        total_delta_balance = 0.0

        # Compute JDT1 target per account.
        jdt1_tax_by_acct = defaultdict(lambda: [0.0, 0.0])
        for ta in tax_amounts:
            jdt1_tax_by_acct[ta["account_id"]][0] += ta["debit"]
            jdt1_tax_by_acct[ta["account_id"]][1] += ta["credit"]

        # Check all tax accounts — JDT1 targets and Odoo-generated.
        all_tax_accounts = set(jdt1_tax_by_acct) | set(move_tax_lines)

        for acct_id in all_tax_accounts:
            jdt1_dr, jdt1_cr = jdt1_tax_by_acct.get(acct_id, [0.0, 0.0])
            target_balance = round(jdt1_dr - jdt1_cr, 2)

            group = move_tax_lines.get(acct_id, [])
            current_balance = sum(l["balance"] for l in group)
            delta = round(target_balance - current_balance, 2)

            if abs(delta) < 0.005:
                continue

            if group:
                # Adjust existing tax line.
                first = group[0]
                new_balance = round(first["balance"] + delta, 2)
                new_debit = round(max(new_balance, 0.0), 2)
                new_credit = round(max(-new_balance, 0.0), 2)
                new_ac = round(first["amount_currency"] + delta, 2)
                updates.append((
                    new_debit, new_credit, new_balance,
                    new_ac, first["id"],
                ))
                first["balance"] = new_balance
                first["amount_currency"] = new_ac
                fixed_tax += 1
            elif target_balance != 0:
                # Insert a new tax line for this account.
                new_debit = round(max(target_balance, 0.0), 2)
                new_credit = round(max(-target_balance, 0.0), 2)
                meta = move_meta.get(move_id, {})
                inserts.append((
                    move_id, acct_id, new_debit, new_credit,
                    target_balance, target_balance,
                    meta.get("company_id"), meta.get("currency_id"),
                    meta.get("journal_id"), meta.get("date"),
                ))
                fixed_tax += 1

            total_delta_balance += delta

        # Rebalance payment_term.
        if abs(total_delta_balance) > 0.001:
            pt_list = pt_lines_by_move.get(move_id, [])
            if pt_list:
                pt = pt_list[0]
                new_pt_bal = round(pt["balance"] - total_delta_balance, 2)
                new_pt_debit = round(max(new_pt_bal, 0.0), 2)
                new_pt_credit = round(max(-new_pt_bal, 0.0), 2)
                new_pt_ac = round(
                    pt["amount_currency"] - total_delta_balance, 2,
                )
                updates.append((
                    new_pt_debit, new_pt_credit, new_pt_bal,
                    new_pt_ac, pt["id"],
                ))
                fixed_pt += 1
            else:
                _logger.warning(
                    "Move %s: tax delta %.2f but no payment_term line",
                    move_id, total_delta_balance,
                )

    if updates:
        cr.executemany(
            """UPDATE account_move_line
                  SET debit = %s, credit = %s, balance = %s,
                      amount_currency = %s
                WHERE id = %s""",
            updates,
        )

    if inserts:
        cr.executemany(
            """INSERT INTO account_move_line
                  (move_id, account_id, debit, credit, balance,
                   amount_currency, company_id, currency_id,
                   journal_id, date, display_type, name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       'tax', 'SAP tax (JDT1)')""",
            inserts,
        )
        _logger.info("Inserted %d missing tax lines.", len(inserts))

    return fixed_tax, fixed_pt


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
        """Skip COGS — JDT1 already includes inventory/COGS as separate JEs."""
        return []

    # ----------------------------------------------------------------
    # Extract
    # ----------------------------------------------------------------

    @ETL.extract("ojdt")
    def extract_journal_entries(self, ctx: ETLContext) -> ChunkableData:
        """Extract OJDT headers with embedded JDT1 lines and enrichment."""
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

        lines_by_transid = {}
        for line in all_lines:
            lines_by_transid.setdefault(line["transid"], []).append(line)
        for header in headers:
            header["_lines"] = lines_by_transid.get(header["transid"], [])

        # Embed enrichment per record (not in shared context)
        enrichable = [
            h for h in headers if h["transtype"] in _ENRICHABLE_TYPES
        ]
        if enrichable:
            self._embed_enrichment(ctx.cr, enrichable)

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
        """Extract lightweight lookups for transform."""
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

        # Tax accounts: accounts used on tax repartition lines.
        rep_lines = ctx.env["account.tax.repartition.line"].search_read(
            [("account_id", "!=", False)], ["account_id"],
        )
        tax_account_ids = {r["account_id"][0] for r in rep_lines}

        return {
            "partners": partners_dict,
            "lookups": lookups,
            "misc_journal_id": misc_journal.id if misc_journal else False,
            "tax_account_ids": tax_account_ids,
        }

    def _embed_enrichment(self, sap_cr, enrichable_headers):
        """Fetch source doc headers + lines and embed per OJDT record."""
        by_type = {}
        for h in enrichable_headers:
            by_type.setdefault(h["transtype"], []).append(h)

        for transtype, type_headers in by_type.items():
            config = _TRANSTYPE_CONFIG[transtype]
            header_table = config["sap_table"]
            line_table = config["line_table"]

            docentries = tuple(h["createdby"] for h in type_headers)
            sap_cr.execute(
                f"SELECT * FROM {header_table} WHERE docentry IN %s"
                " AND COALESCE(canceled, 'N') != 'C'",
                (docentries,),
            )
            doc_headers = {
                row["docentry"]: row for row in sap_cr.dictfetchall()
            }

            lines_by_doc = {}
            if doc_headers:
                docs = [{"docentry": de} for de in doc_headers]
                lines = self._get_lines(sap_cr, line_table, docs)
                for line in lines:
                    lines_by_doc.setdefault(
                        line["docentry"], [],
                    ).append(line)

            for h in type_headers:
                h["_doc"] = doc_headers.get(h["createdby"])
                h["_doc_lines"] = lines_by_doc.get(h["createdby"], [])

            _logger.info(
                "Embedded %d/%d %s docs for enrichment.",
                len(doc_headers), len(type_headers), header_table,
            )

    # ----------------------------------------------------------------
    # Transform
    # ----------------------------------------------------------------

    @ETL.transform()
    def transform_journal_entries(self, ctx: ETLContext, extracted):
        """Dispatch: enriched for typed, JDT1 for generic."""
        data = extracted["extract_journal_entries"]
        headers = (
            data.records if hasattr(data, "records")
            else data.get("records", [])
        )

        if not headers:
            return {"move_vals": [], "lookups": {}}

        meta = extracted["extract_lookups"]
        partners_dict = meta["partners"]
        lookups = meta["lookups"]
        misc_journal_id = meta["misc_journal_id"]
        tax_account_ids = meta["tax_account_ids"]

        accounts_dict = lookups["accounts"]
        currencies_dict = lookups["currencies"]
        company_currency_id = lookups["company_currency_id"]

        move_vals_list = []
        enriched_count = 0
        companion_count = 0
        generic_count = 0

        for header in headers:
            ref = f"ojdt#{header.get('transid')}"
            with ctx.skippable(ref):
                jdt1_lines = header.pop("_lines", [])
                doc = header.pop("_doc", None)
                doc_lines = header.pop("_doc_lines", [])
                if not jdt1_lines:
                    raise ValueError("No JDT1 lines found")

                transtype = header["transtype"]
                config = _TRANSTYPE_CONFIG.get(transtype)

                move_vals = None
                if transtype in _ENRICHABLE_TYPES and config and doc:
                    move_vals = self._build_enriched_vals(
                        header, doc, doc_lines, config,
                        partners_dict, lookups,
                    )
                    if move_vals:
                        enriched_count += 1
                        self._extract_jdt1_metadata(
                            move_vals, jdt1_lines, accounts_dict,
                            tax_account_ids,
                        )
                        cogs_appended = self._append_jdt1_residuals(
                            header, jdt1_lines, move_vals, accounts_dict,
                            tax_account_ids,
                        )
                        if cogs_appended:
                            companion_count += 1
                    else:
                        _logger.info(
                            "Enriched build returned None for transid=%s "
                            "transtype=%s createdby=%s (doc_lines=%d, "
                            "partner=%s). Falling through to generic.",
                            header.get("transid"), transtype,
                            header.get("createdby"), len(doc_lines),
                            doc.get("cardcode") if doc else "N/A",
                        )
                elif transtype in _ENRICHABLE_TYPES:
                    _logger.info(
                        "Enrichable transid=%s transtype=%s but no doc "
                        "(config=%s, doc=%s). Falling through to generic.",
                        header.get("transid"), transtype,
                        bool(config), bool(doc),
                    )

                if not move_vals:
                    move_vals = self._build_generic_entry_vals(
                        header, jdt1_lines, accounts_dict, partners_dict,
                        currencies_dict, company_currency_id,
                        misc_journal_id,
                    )

                if not move_vals:
                    raise ValueError(
                        f"Both enriched and generic returned None "
                        f"(transtype={transtype}, "
                        f"createdby={header.get('createdby')})"
                    )

                if move_vals.get("sap_table") == "ojdt":
                    generic_count += 1
                move_vals_list.append(move_vals)

        _logger.info(
            "Transformed %d journal entries "
            "(%d enriched, %d with cogs, %d generic).",
            len(move_vals_list), enriched_count,
            companion_count, generic_count,
        )
        return {
            "move_vals": move_vals_list,
            "lookups": lookups,
            "tax_account_ids": tax_account_ids,
        }

    # ----------------------------------------------------------------
    # Load
    # ----------------------------------------------------------------

    @ETL.load()
    def load_journal_entries(self, ctx: ETLContext, transformed):
        """Create and post account.move records."""
        data = transformed.get("transform_journal_entries", {})
        move_vals_list = data.get("move_vals", [])
        lookups = data.get("lookups", {})
        tax_account_ids = data.get("tax_account_ids", set())

        if not move_vals_list:
            return

        self._create_pending_currency_rates(lookups)

        # Strip GL metadata (not real fields) before create().
        gl_truth = {}
        for i, vals in enumerate(move_vals_list):
            truth = {}
            if "_jdt1_arap_account_id" in vals:
                truth["arap_account_id"] = vals.pop("_jdt1_arap_account_id")
            if "_jdt1_tax_amounts" in vals:
                truth["tax_amounts"] = vals.pop("_jdt1_tax_amounts")
            if "_sap_doctotal" in vals:
                truth["doctotal"] = vals.pop("_sap_doctotal")
            if truth:
                truth["move_type"] = vals.get("move_type", "entry")
                gl_truth[i] = truth

        moves = ctx.env["account.move"]
        move_index = {}  # move.id -> index in move_vals_list
        for i, vals in enumerate(move_vals_list):
            ref = (
                f"{vals.get('sap_table', 'ojdt')}#"
                f"{vals.get('sap_docentry', '?')}"
            )
            with ctx.skippable(ref):
                move = ctx.env["account.move"].create(vals)

                # Immediately fix this move's accounts and taxes
                # while still inside the skippable savepoint.
                ctx.env.cr.execute(
                    """
                    UPDATE account_move_line
                       SET account_id = sap_acct_id
                     WHERE sap_acct_id IS NOT NULL
                       AND account_id <> sap_acct_id
                       AND move_id = %s
                    """,
                    (move.id,),
                )

                truth = gl_truth.get(i)
                if truth and "tax_amounts" in truth:
                    _fix_taxes_pre_posting_sap(ctx.env.cr, {
                        move.id: {
                            "tax_amounts": truth["tax_amounts"],
                            "move_type": truth.get("move_type", "entry"),
                        },
                    }, tax_account_ids=tax_account_ids)
                    # _fix_taxes_pre_posting_sap rewrites debit/credit/
                    # balance/amount_currency on payment_term and tax
                    # lines via raw SQL.  The stored computed fields
                    # amount_residual / amount_residual_currency /
                    # reconciled still hold the values computed at
                    # create() time -- before the fix adjusted
                    # amount_currency.  Without an explicit refresh,
                    # downstream reconciliation reads stale residuals
                    # and caps each partial at the pre-fix (untaxed)
                    # amount, leaving invoices stuck with
                    # residual = amount_tax even when SAP shows them
                    # fully paid.
                    #
                    # We can't go through the ORM (`_compute_amount_residual`
                    # + `flush_recordset`) because flush_recordset triggers
                    # an AML write hook that runs `_check_balanced` against
                    # the in-memory cache, and the cache holds the pre-fix
                    # debit/credit/balance values.  That fails on ~50
                    # invoices and rolls them back via the ETL savepoint.
                    #
                    # Recompute directly in SQL instead.  At this point in
                    # the import there are no partial reconciles for the
                    # move yet, so the formula reduces to:
                    #   amount_residual          = balance
                    #   amount_residual_currency = amount_currency
                    # and the line is reconciled iff both are zero.
                    # Only AR/AP/cash control accounts get a non-zero
                    # residual (matches the `need_residual_lines` filter
                    # in account.move.line._compute_amount_residual).
                    ctx.env.cr.execute(
                        """
                        UPDATE account_move_line ml
                        SET amount_residual = ml.balance,
                            amount_residual_currency = ml.amount_currency,
                            reconciled = (
                                ml.balance = 0
                                AND ml.amount_currency = 0
                            )
                        FROM account_account a
                        WHERE ml.move_id = %s
                          AND a.id = ml.account_id
                          AND (
                            a.reconcile
                            OR a.account_type IN (
                                'asset_cash', 'liability_credit_card'
                            )
                          )
                        """,
                        (move.id,),
                    )

                ctx.env.invalidate_all()
                moves |= move
                move_index[move.id] = i

        if not moves:
            return

        # Filter out phantom records from savepoint rollbacks
        moves = moves.exists()
        if not moves:
            return

        # ── Post moves, grouped by journal ──
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

        # ── Post-posting: fix AR/AP account on payment_term lines ──
        corrected_arap = 0
        for move in moves:
            idx = move_index.get(move.id)
            if idx is None or idx not in gl_truth:
                continue
            arap_id = gl_truth[idx].get("arap_account_id")
            if arap_id:
                ctx.env.cr.execute(
                    """
                    UPDATE account_move_line
                       SET account_id = %s
                     WHERE move_id = %s
                       AND display_type = 'payment_term'
                       AND account_id <> %s
                    """,
                    (arap_id, move.id, arap_id),
                )
                corrected_arap += ctx.env.cr.rowcount

        if corrected_arap:
            ctx.env.invalidate_all()
            _logger.info(
                "Post-posting: corrected %d AR/AP accounts.", corrected_arap,
            )

        # ── Verify: Odoo amount_total matches SAP DocTotal ──
        mismatched = 0
        for move in moves:
            idx = move_index.get(move.id)
            if idx is None:
                continue
            truth = gl_truth.get(idx, {})
            doctotal = truth.get("doctotal")
            if doctotal and abs(move.amount_total - doctotal) > 1.0:
                _logger.warning(
                    "amount_total %.2f != SAP DocTotal %.2f (diff=%.2f) "
                    "for %s #%s",
                    move.amount_total, doctotal,
                    move.amount_total - doctotal,
                    move.sap_table, move.sap_docentry,
                )
                mismatched += 1
        if mismatched:
            _logger.warning(
                "%d enriched moves have amount_total != SAP DocTotal.",
                mismatched,
            )

        _logger.info("Created and posted %d journal entries.", len(moves))

    # ----------------------------------------------------------------
    # Enrichment builder
    # ----------------------------------------------------------------

    def _build_enriched_vals(self, header, doc, doc_lines, config,
                             partners_dict, lookups):
        """Build enriched move vals from embedded source doc."""
        partner_id = partners_dict.get(doc.get("cardcode"))
        if not partner_id:
            return None

        lines_dict = {doc["docentry"]: doc_lines}

        vals = self._get_move_vals(
            doc, partner_id, lines_dict,
            config["sap_table"], config["line_table"],
            {}, lookups,
        )

        if not vals.get("line_ids"):
            return None

        self._normalize_move_type(
            vals, config["move_type"], config["refund_type"],
        )

        vals["_sap_doctotal"] = float(doc.get("doctotal") or 0)

        return vals

    # ----------------------------------------------------------------
    # JDT1 metadata extraction (for tax + AR/AP corrections)
    # ----------------------------------------------------------------

    @staticmethod
    def _extract_jdt1_metadata(
        enriched_vals, jdt1_lines, accounts_dict, tax_account_ids,
    ):
        """Extract GL truth from JDT1 for pre/post-posting corrections.

        Stores on enriched_vals (stripped before create):
        - _jdt1_arap_account_id: AR/AP account from JDT1
        - _jdt1_tax_amounts: [{account_id, debit, credit}] for tax lines
        """
        arap_account_id = None
        tax_amounts = []

        for jdt1 in jdt1_lines:
            debit = float(jdt1.get("debit") or 0)
            credit = float(jdt1.get("credit") or 0)

            acct_code = (jdt1.get("acct_formatcode") or "").strip()
            account_info = accounts_dict.get(acct_code)
            if not account_info:
                continue
            account_id, account_type = account_info

            # AR/AP is always line_id=0 in SAP B1 enrichable docs
            if jdt1.get("line_id") == 0:
                arap_account_id = account_id
            elif account_id in tax_account_ids:
                if debit != 0 or credit != 0:
                    tax_amounts.append({
                        "account_id": account_id,
                        "debit": debit,
                        "credit": credit,
                    })

        if arap_account_id:
            enriched_vals["_jdt1_arap_account_id"] = arap_account_id
        # Always set tax_amounts (even empty) so the pre-posting fix
        # runs for all enriched moves — zeroing out Odoo-generated tax
        # lines when JDT1 has no tax (e.g., exempt invoices).
        enriched_vals["_jdt1_tax_amounts"] = tax_amounts

    # ----------------------------------------------------------------
    # JDT1 residual lines (COGS, inventory, variance)
    # ----------------------------------------------------------------

    @staticmethod
    def _append_jdt1_residuals(
        header, jdt1_lines, enriched_vals, accounts_dict,
        tax_account_ids,
    ):
        """Append residual JDT1 lines to the enriched move as cogs lines.

        Computes per-account residuals (JDT1 total − enriched total) and
        appends them with ``display_type='cogs'`` so they don't affect
        ``invoice_line_ids`` or the invoice total.  This replaces the
        former companion entry approach.

        Returns the number of residual lines appended.
        """
        move_type = enriched_vals.get("move_type", "entry")

        # Build set of payable/receivable account IDs for date_maturity
        payable_receivable_ids = {
            aid for aid, atype in accounts_dict.values()
            if atype in ("asset_receivable", "liability_payable")
        }

        primary_arap_id = enriched_vals.get("_jdt1_arap_account_id")
        skip_ids = set(tax_account_ids) | payable_receivable_ids
        if primary_arap_id:
            skip_ids.add(primary_arap_id)

        # 1. Sum enriched move amounts by account.
        enriched_by_acct = defaultdict(lambda: [0.0, 0.0])
        for cmd in enriched_vals.get("line_ids", []):
            if not (isinstance(cmd, (list, tuple)) and cmd[0] == 0):
                continue
            lv = cmd[2]
            if lv.get("display_type") in (
                "line_note", "line_section", "line_subsection",
            ):
                continue
            acct_id = lv.get("sap_acct_id") or lv.get("account_id")
            if not acct_id or acct_id in skip_ids:
                continue
            qty = float(lv.get("quantity", 0) or 0)
            price = float(lv.get("price_unit", 0) or 0)
            signed = round(qty * price, 2)
            if signed == 0:
                continue
            if move_type in ("out_invoice", "in_refund"):
                if signed > 0:
                    enriched_by_acct[acct_id][1] += signed
                else:
                    enriched_by_acct[acct_id][0] += -signed
            else:
                if signed > 0:
                    enriched_by_acct[acct_id][0] += signed
                else:
                    enriched_by_acct[acct_id][1] += -signed

        # 2. Sum JDT1 amounts by account, skipping tax + primary AR/AP +
        #    payable/receivable (same skip set as enriched).
        jdt1_by_acct = defaultdict(lambda: [0.0, 0.0])
        for jdt1 in jdt1_lines:
            debit = float(jdt1.get("debit") or 0)
            credit = float(jdt1.get("credit") or 0)
            if debit == 0 and credit == 0:
                continue
            acct_code = (jdt1.get("acct_formatcode") or "").strip()
            account_info = accounts_dict.get(acct_code)
            if not account_info:
                continue
            account_id = account_info[0]
            if account_id in skip_ids:
                continue
            jdt1_by_acct[account_id][0] += debit
            jdt1_by_acct[account_id][1] += credit

        # 3. Compute residuals.
        appended = 0
        for acct_id in set(jdt1_by_acct) | set(enriched_by_acct):
            jdr, jcr = jdt1_by_acct.get(acct_id, [0.0, 0.0])
            edr, ecr = enriched_by_acct.get(acct_id, [0.0, 0.0])
            res_debit = round(jdr - edr, 2)
            res_credit = round(jcr - ecr, 2)

            if abs(res_debit) <= 0.01 and abs(res_credit) <= 0.01:
                continue

            if res_debit < 0:
                res_credit = round(res_credit - res_debit, 2)
                res_debit = 0.0
            if res_credit < 0:
                res_debit = round(res_debit - res_credit, 2)
                res_credit = 0.0

            if res_debit == 0 and res_credit == 0:
                continue

            # Use display_type='product' so the line participates in
            # Odoo's payment_term auto-balance.  Signed as price_unit so
            # Odoo computes debit/credit from the move_type direction.
            if move_type in ("out_invoice", "in_refund"):
                # Credits are positive, debits are negative
                price = round(res_credit - res_debit, 2)
            else:
                # Debits are positive, credits are negative
                price = round(res_debit - res_credit, 2)

            line_vals = {
                "display_type": "product",
                "account_id": acct_id,
                "sap_acct_id": acct_id,
                "quantity": 1,
                "price_unit": price,
                "name": header.get("memo") or "JDT1 GL residual",
                "sap_table": "jdt1",
            }
            enriched_vals["line_ids"].append(Command.create(line_vals))
            appended += 1

        return appended

    # ----------------------------------------------------------------
    # Generic JDT1 builder
    # ----------------------------------------------------------------

    @staticmethod
    def _build_generic_entry_vals(header, jdt1_lines, accounts_dict,
                                  partners_dict, currencies_dict,
                                  company_currency_id, misc_journal_id):
        """Build move_type='entry' from JDT1 lines."""
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
            "sap_acct_id": account_id,
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
