"""QBO GL-first Import Pipeline (Journal export as source of truth).

This pipeline inverts the legacy `qbo.gl.import` flow:

- The QBO Journal export XLSX is treated as the primary source of postings.
- Each transaction is routed:
  - **Enrichable types** (Invoice/Bill/CreditMemo/VendorCredit) are imported as
    typed moves using the QBO API entity + `QBOMoveBuilder`. Product-line
    accounts are restored from the export pre-posting, tax amounts are fixed
    from ``TxnTaxDetail`` pre-posting, and AR/AP accounts are corrected
    post-posting.
  - Everything else is imported as `move_type='entry'` from the export lines.

The export transaction ID is assumed to match the QBO entity Id.
"""

import base64
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData, post_lock

from .exchange_rate_helper import ExchangeRateEnsurer
from .extractor import QBOExtractor
from .gl_import_etl import _get_imported_qbo_ids, _parse_journal_export
from .utils import get_api_client

_logger = logging.getLogger(__name__)


_ENRICHABLE: Dict[str, Tuple[str, Dict]] = {
    # export_type: (qbo_entity, builder_kwargs)
    "Invoice": (
        "Invoice",
        dict(
            move_type="out_invoice",
            journal_type="sale",
            partner_type="customer",
            qbo_id_field="qbo_invoice_id",
            line_detail_types=("SalesItemLineDetail", "DiscountLineDetail"),
            tax_use="sale",
            direction="income",
            memo_field="CustomerMemo",
            memo_key="value",
        ),
    ),
    "Bill": (
        "Bill",
        dict(
            move_type="in_invoice",
            journal_type="purchase",
            partner_type="vendor",
            qbo_id_field="qbo_bill_id",
            line_detail_types=(
                "ItemBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail",
            ),
            tax_use="purchase",
            direction="expense",
            memo_field="Memo",
            memo_key=None,
        ),
    ),
    "Credit Memo": (
        "CreditMemo",
        dict(
            move_type="out_refund",
            journal_type="sale",
            partner_type="customer",
            qbo_id_field="qbo_credit_memo_id",
            line_detail_types=("SalesItemLineDetail", "DiscountLineDetail"),
            tax_use="sale",
            direction="income",
            memo_field="CustomerMemo",
            memo_key="value",
        ),
    ),
    "Vendor Credit": (
        "VendorCredit",
        dict(
            move_type="in_refund",
            journal_type="purchase",
            partner_type="vendor",
            qbo_id_field="qbo_vendor_credit_id",
            line_detail_types=(
                "ItemBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail",
            ),
            tax_use="purchase",
            direction="expense",
            memo_field="Memo",
            memo_key=None,
        ),
    ),
}


def _build_code_maps(ctx: ETLContext, extractor: QBOExtractor) -> None:
    """Populate `extractor.extra` with account code, currency, and type maps."""
    ctx.env.cr.execute(
        """
        SELECT aa.id,
               aa.code_store::jsonb->>jsonb_object_keys(
                   aa.code_store::jsonb) as code,
               aa.currency_id,
               aa.account_type
        FROM account_account aa
        """
    )
    code_map = {}
    account_currency_map = {}
    account_type_map = {}  # account_id -> account_type string
    for acct_id, code, currency_id, acct_type in ctx.env.cr.fetchall():
        if code:
            code_map[code] = acct_id
            if currency_id:
                account_currency_map[acct_id] = currency_id
            if acct_type:
                account_type_map[acct_id] = acct_type
    extractor.extra["code_map"] = code_map
    extractor.extra["account_currency_map"] = account_currency_map
    extractor.extra["account_type_map"] = account_type_map
    extractor.extra["company_currency_id"] = ctx.env.company.currency_id.id



def _resolve_account(
    code_map: Dict[str, int],
    account_code: str,
    account_type_map: Optional[Dict[int, str]] = None,
) -> Optional[Tuple[int, Optional[str]]]:
    """Resolve account code to (account_id, account_type) or None."""
    for suffix in ("", ".1", ".2", ".3"):
        account_id = code_map.get(account_code + suffix)
        if account_id:
            acct_type = account_type_map.get(account_id) if account_type_map else None
            return account_id, acct_type
    return None


def _resolve_account_id(code_map: Dict[str, int], account_code: str) -> Optional[int]:
    result = _resolve_account(code_map, account_code)
    return result[0] if result else None


def _annotate_with_gl_accounts(
    vals: Dict,
    export_lines: List[Dict],
    code_map: Dict[str, int],
    account_type_map: Dict[int, str],
    exchange_rate: float = 1.0,
) -> Dict:
    """Annotate an enriched move's line vals with ``qbo_acct_id`` from the
    Journal export, so the GL account can be restored after Odoo's computes.

    For ``invoice_line_ids`` (product lines on typed moves), we match each
    builder-produced line to an export line by amount.  Unmatched lines keep
    whatever account the builder assigned.

    AR/AP account is extracted from the export only as a fallback — the builder
    may have already set ``_gl_arap_account_id`` from ``APAccountRef``.

    Returns the vals dict (mutated in place).
    """
    # Build a pool of non-AR/AP export lines with resolved account IDs.
    # Each entry: (signed_amount, account_id, used)
    export_pool: List[List] = []  # mutable list so we can mark "used"
    arap_accounts: List[Tuple[float, int]] = []

    for ld in export_lines:
        code = str(ld.get("account_code", "")).strip()
        resolved = _resolve_account(code_map, code, account_type_map)
        if not resolved:
            continue
        account_id, account_type = resolved
        signed = float(ld.get("debit") or 0) - float(ld.get("credit") or 0)

        if account_type in ("asset_receivable", "liability_payable"):
            arap_accounts.append((signed, account_id))
        else:
            export_pool.append([signed, account_id, False])  # [amount, id, used]

    # Store AR/AP on vals for post-posting correction (builder value takes priority).
    if arap_accounts and "_gl_arap_account_id" not in vals:
        arap_accounts.sort(key=lambda x: abs(x[0]), reverse=True)
        vals["_gl_arap_account_id"] = arap_accounts[0][1]

    # Annotate product lines (invoice_line_ids).
    line_key = "invoice_line_ids" if "invoice_line_ids" in vals else "line_ids"
    for cmd_tuple in vals.get(line_key, []):
        if not isinstance(cmd_tuple, (list, tuple)) or cmd_tuple[0] != 0:
            continue
        line_vals = cmd_tuple[2]

        # Compute the line's expected signed amount from price_unit * qty.
        # For sales (out_invoice/out_refund), revenue is a credit (negative).
        # For purchases (in_invoice/in_refund), expense is a debit (positive).
        qty = float(line_vals.get("quantity", 1) or 1)
        price = float(line_vals.get("price_unit", 0) or 0)
        # Convert source-currency amount to company currency for matching
        # against the Journal export (which is always in company currency).
        line_amount_ccy = qty * price * exchange_rate

        move_type = vals.get("move_type", "entry")
        if move_type in ("out_invoice", "out_refund"):
            target = -abs(line_amount_ccy)
        elif move_type in ("in_invoice", "in_refund"):
            target = abs(line_amount_ccy)
        else:
            target = line_amount_ccy

        # Find the closest unmatched export line.
        best_idx = None
        best_diff = float("inf")
        for i, (amt, _acct_id, used) in enumerate(export_pool):
            if used:
                continue
            diff = abs(amt - target)
            if diff < best_diff:
                best_diff = diff
                best_idx = i

        if best_idx is not None and best_diff < max(abs(target) * 0.05, 1.0):
            export_pool[best_idx][2] = True  # mark used
            line_vals["qbo_acct_id"] = export_pool[best_idx][1]

    return vals


def _fix_tax_amounts_orm(move, tax_amounts: List[Dict], move_type: str) -> int:
    """Fix tax line amounts on a draft move using ORM writes.

    For each ``_tax_amounts`` entry:

    - If a matching Odoo tax line exists (by ``tax_line_id``), adjust its
      ``amount_currency`` to match the QBO amount.  The inverse syncs
      ``balance``/``debit``/``credit`` and the payment_term auto-rebalances.
    - If **no** matching tax line exists (Odoo computed $0 tax because the
      product lines are tiny/dummy — common on CBSA customs and brokerage
      bills), add an explicit product line on the tax's repartition account
      for the exact amount.  The payment_term still auto-rebalances.

    Args:
        move: Draft ``account.move`` record.
        tax_amounts: ``[{"tax_id": int, "amount": float}]`` from QBO
            ``TxnTaxDetail`` (source-currency, always positive).
        move_type: Move type string (e.g. ``'in_invoice'``).

    Returns:
        Number of lines adjusted or created.
    """
    if not tax_amounts:
        return 0

    tax_lines = move.line_ids.filtered(
        lambda l: l.display_type == "tax" and l.tax_line_id
    )

    # Group tax lines by tax_line_id (child tax).
    by_tax = defaultdict(lambda: move.env["account.move.line"])
    for line in tax_lines:
        by_tax[line.tax_line_id.id] |= line

    line_commands = []
    fixed = 0

    for ta in tax_amounts:
        odoo_tax_id = ta["tax_id"]
        qbo_amount = float(ta["amount"])  # source currency, always positive

        group = by_tax.get(odoo_tax_id)
        if group:
            # Normal path: adjust the existing tax line.
            target_ac = qbo_amount if move_type in ("in_invoice", "out_refund") else -qbo_amount
            current_ac = sum(group.mapped("amount_currency"))
            delta = round(target_ac - current_ac, 2)

            if abs(delta) < 0.005:
                continue

            first = group[0]
            line_commands.append(
                (1, first.id, {"amount_currency": round(first.amount_currency + delta, 2)})
            )
            fixed += 1
        elif qbo_amount:
            # No tax line exists (Odoo computed $0) — add a product line
            # on the tax's repartition account for the exact amount.
            tax = move.env["account.tax"].browse(odoo_tax_id)
            rep_line = tax.invoice_repartition_line_ids.filtered(
                lambda l: l.repartition_type == "tax" and l.account_id
            )
            if not rep_line:
                _logger.warning(
                    "Move %s: no repartition account for tax %s — skipping",
                    move.id, odoo_tax_id,
                )
                continue
            line_commands.append((0, 0, {
                "name": tax.name,
                "account_id": rep_line[0].account_id.id,
                "price_unit": qbo_amount,
                "quantity": 1,
                "tax_ids": [],
            }))
            fixed += 1

    if line_commands:
        move.write({"line_ids": line_commands})

    return fixed


def _journal_entry_vals_from_export(
    *,
    txn_id: str,
    txn_type: str,
    txn_date: str,
    txn_num: str,
    txn_name: str,
    lines_data: List[Dict],
    code_map: Dict[str, int],
    account_currency_map: Dict[int, int],
    company_currency_id: int,
    journal_id: int,
    company_id: int,
) -> Optional[Dict]:
    lines = []
    for ld in lines_data:
        account_code = ld["account_code"]
        account_id = _resolve_account_id(code_map, account_code)
        if not account_id:
            _logger.warning(
                f"Account {account_code} not found for export txn #{txn_id} ({txn_type})"
            )
            return None

        line_name = ld["memo"] or ld["name"] or txn_type
        line_vals = {
            "account_id": account_id,
            "debit": ld["debit"],
            "credit": ld["credit"],
            "name": line_name,
        }

        acct_currency = account_currency_map.get(account_id)
        if acct_currency and acct_currency != company_currency_id:
            foreign_amount = ld["debit"] - ld["credit"]
            line_vals["currency_id"] = acct_currency
            line_vals["amount_currency"] = foreign_amount

        lines.append((0, 0, line_vals))

    if not lines:
        return None

    total_debit = sum(l[2]["debit"] for l in lines)
    total_credit = sum(l[2]["credit"] for l in lines)
    diff = round(total_debit - total_credit, 2)
    if abs(diff) > 0.01:
        _logger.warning(
            f"Export txn #{txn_id} ({txn_type}) unbalanced by {diff:.2f} — skipping"
        )
        return None

    ref = f"JNL-{txn_type}-{txn_id}"
    narration = f"{txn_type}" + (f" #{txn_num}" if txn_num else "") + (
        f" — {txn_name}" if txn_name else ""
    )

    return {
        "move_type": "entry",
        "journal_id": journal_id,
        "date": txn_date,
        "ref": ref,
        "narration": narration,
        "company_id": company_id,
        "line_ids": lines,
    }


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.gl.first.import",
    sap_source="GLExport",
    depends_on=[
        "qbo.account.importer",
        "qbo.customer.importer",
        "qbo.customer.linker",
        "qbo.vendor.importer",
        "qbo.vendor.linker",
        "qbo.item.importer",
        "qbo.tax.importer",
        "qbo.partner.account.linker",
        "qbo.category.account.fixer",
    ],
)
class QboGlFirstImporter(models.AbstractModel):
    _name = "qbo.gl.first.import"
    _description = "QBO GL-first importer (Journal export primary)"

    @ETL.extract("GLExport")
    def extract_gl_first(self, ctx: ETLContext) -> ChunkableData:
        extractor = QBOExtractor(ctx)

        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if not connection.gl_export_file:
            _logger.info("No Journal export file uploaded — skipping GL-first import")
            return ChunkableData(records=[], context={"extractor": extractor.export(), "entities": {}})

        file_content = base64.b64decode(connection.gl_export_file)
        transactions = _parse_journal_export(file_content)
        _logger.info(f"Parsed {len(transactions)} transactions from Journal export")

        imported_ids = _get_imported_qbo_ids(ctx)
        _logger.info(f"Found {len(imported_ids)} already-imported QBO IDs")
        new_txns = [t for t in transactions if str(t["id"]) not in imported_ids]

        type_counts = defaultdict(int)
        for t in new_txns:
            if t["lines"]:
                type_counts[t["lines"][0]["type"]] += 1
        _logger.info(
            f"After filtering: {len(new_txns)} unimported transactions "
            f"({', '.join(f'{t}: {c}' for t, c in sorted(type_counts.items()))})"
        )

        # Preload everything needed to build typed moves when enriching.
        extractor.preload(
            "account",
            "customer",
            "vendor",
            "product",
            "product_income",
            "product_expense",
            "sale_tax",
            "sale_tax_rate",
            "purchase_tax",
            "purchase_tax_rate",
            "currency",
        )
        extractor.preload_journals("general", "sale", "purchase")
        _build_code_maps(ctx, extractor)

        # Fetch QBO entities for enrichable transaction types only.
        api_client = get_api_client(ctx)
        entities: Dict[str, Dict[str, Dict]] = {k: {} for k in _ENRICHABLE}

        enrich_ids: Dict[str, Set[str]] = {k: set() for k in _ENRICHABLE}
        for t in new_txns:
            if not t.get("lines"):
                continue
            txn_type = t["lines"][0]["type"]
            if txn_type in _ENRICHABLE:
                enrich_ids[txn_type].add(str(t["id"]))

        total_to_fetch = sum(len(v) for v in enrich_ids.values())
        _logger.info(f"Need to fetch {total_to_fetch} enrichable QBO entities")

        for export_type, ids in enrich_ids.items():
            if not ids:
                continue
            entity_name, _ = _ENRICHABLE[export_type]
            wanted = set(ids)
            _logger.info(f"Bulk fetching {export_type} via query_all ({len(wanted)} needed)")
            with ctx.skippable(f"bulk fetch QBO {entity_name} ({len(wanted)} ids)"):
                # Much faster than per-id GET, and QBOApiClient already logs progress
                # every 1000 records.
                all_recs = api_client.query_all(entity=entity_name, order_by="Id")

            fetched = 0
            for rec in all_recs:
                rec_id = str(rec.get("Id") or "")
                if rec_id and rec_id in wanted:
                    entities[export_type][rec_id] = rec
                    fetched += 1
            _logger.info(f"Fetched {fetched}/{len(wanted)} {export_type} entities (filtered)")

        # Ensure exchange rates exist for all enrichable foreign-currency txns
        # before the transform creates moves (otherwise Odoo falls back to 1.0).
        all_api_records = [
            rec
            for type_dict in entities.values()
            for rec in type_dict.values()
        ]
        if all_api_records:
            inserted = ExchangeRateEnsurer(ctx.env).ensure_rates(all_api_records)
            _logger.info(f"Exchange rates: ensured from {len(all_api_records)} API records ({inserted} new)")

        return ChunkableData(
            records=new_txns,
            context={
                "extractor": extractor.export(),
                "entities": entities,
            },
        )

    @ETL.transform()
    def transform_gl_first(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        data = extracted.get("extract_gl_first")
        if not data:
            return []
        transactions = data.records if hasattr(data, "records") else data.get("records", [])
        context = data.context if hasattr(data, "context") else {}
        extractor_data = context.get("extractor", {})
        entities = context.get("entities", {}) or {}

        if not transactions:
            return []

        from .move_builder import QBOMoveBuilder

        builder = QBOMoveBuilder(extractor_data)
        code_map = builder.get_extra("code_map") or {}
        account_type_map = builder.get_extra("account_type_map") or {}
        account_currency_map = builder.get_extra("account_currency_map") or {}
        company_currency_id = builder.get_extra("company_currency_id")
        company_id = builder._company_id

        general_journal_id = builder.get_journal_id("general")

        move_vals: List[Dict] = []
        skipped = 0
        enriched = 0
        fallback = 0
        failed_enrichment: Dict[str, List[str]] = defaultdict(list)

        for txn in transactions:
            qbo_id = str(txn["id"])
            lines_data = txn.get("lines") or []
            if not lines_data:
                skipped += 1
                continue

            first = lines_data[0]
            txn_type = first["type"]
            txn_date = first["date"]
            txn_num = first["num"]
            txn_name = first["name"]

            # Enriched typed moves when possible, fallback to entry.
            if txn_type in _ENRICHABLE:
                entity = (entities.get(txn_type) or {}).get(qbo_id)
                if entity:
                    _entity_name, kwargs = _ENRICHABLE[txn_type]
                    vals = builder.build_invoice_move_vals(entity, **kwargs)
                    if vals:
                        # QBO ExchangeRate = home currency per 1 foreign unit
                        # (e.g. 1.4 means 1 USD = 1.4 CAD).  Domestic = 1.0.
                        fx_rate = float(entity.get("ExchangeRate", 1.0) or 1.0)
                        _annotate_with_gl_accounts(
                            vals, lines_data, code_map, account_type_map,
                            exchange_rate=fx_rate,
                        )
                        if fx_rate != 1.0:
                            vals["_exchange_rate"] = fx_rate
                        move_vals.append(vals)
                        enriched += 1
                        continue
                # Log why enrichment failed for investigation.
                reason = "not in API" if not entity else "builder returned None"
                failed_enrichment[txn_type].append(f"{qbo_id} ({reason})")

            entry_vals = _journal_entry_vals_from_export(
                txn_id=qbo_id,
                txn_type=txn_type,
                txn_date=txn_date,
                txn_num=txn_num,
                txn_name=txn_name,
                lines_data=lines_data,
                code_map=code_map,
                account_currency_map=account_currency_map,
                company_currency_id=company_currency_id,
                journal_id=general_journal_id,
                company_id=company_id,
            )
            if entry_vals:
                # Mark QBO ID field so downstream pipelines skip duplicates
                # and the reconciliation pipeline can find these moves.
                _QBO_TYPE_FIELD = {
                    "Invoice": "qbo_invoice_id",
                    "Bill": "qbo_bill_id",
                    "Credit Memo": "qbo_credit_memo_id",
                    "Vendor Credit": "qbo_vendor_credit_id",
                    "Payment": "qbo_payment_id",
                    "Bill Payment (Cheque)": "qbo_bill_payment_id",
                    "Bill Payment (Credit Card)": "qbo_bill_payment_id",
                    "Expense": "qbo_expense_id",
                    "Transfer": "qbo_transfer_id",
                    "Deposit": "qbo_deposit_id",
                    "Journal Entry": "qbo_journal_entry_id",
                    "Sales Receipt": "qbo_sales_receipt_id",
                    "Refund Receipt": "qbo_refund_receipt_id",
                    "Sales Tax Payment": "qbo_tax_payment_id",
                    "Tax Payment": "qbo_tax_payment_id",
                    "Credit Card Payment": "qbo_cc_payment_id",
                }
                field = _QBO_TYPE_FIELD.get(txn_type)
                if field:
                    entry_vals[field] = int(qbo_id)
                move_vals.append(entry_vals)
                if txn_type in _ENRICHABLE:
                    fallback += 1
            else:
                skipped += 1

        # Log enrichment stats.
        _logger.info(
            f"Transformed {len(move_vals)} moves "
            f"(enriched={enriched}, fallback={fallback}, skipped={skipped})"
        )
        for etype, ids in failed_enrichment.items():
            _logger.info(
                f"  {etype}: {len(ids)} fell through to entry — "
                f"first 10: {ids[:10]}"
            )
        return move_vals

    @ETL.load()
    def load_gl_first(self, ctx: ETLContext, transformed: Dict) -> None:
        move_vals = transformed.get("transform_gl_first", [])
        if not move_vals:
            _logger.info("No GL-first transactions to create")
            return

        # Separate GL metadata that isn't a real field before create().
        gl_truth: Dict[int, Dict] = {}  # index in move_vals -> truth

        for i, vals in enumerate(move_vals):
            truth = {}
            if "_gl_arap_account_id" in vals:
                truth["arap"] = vals.pop("_gl_arap_account_id")
            if "_tax_amounts" in vals:
                truth["tax_amounts"] = vals.pop("_tax_amounts")
            if "_exchange_rate" in vals:
                vals.pop("_exchange_rate")  # Used only for annotation
            if truth:
                gl_truth[i] = truth

        moves = ctx.env["account.move"]
        move_index: Dict[int, int] = {}  # move.id -> vals index
        for i, vals in enumerate(move_vals):
            ref = vals.get("ref") or vals.get("name") or "?"
            with ctx.skippable(f"create move {ref}"):
                move = ctx.env["account.move"].create(vals)
                moves |= move
                move_index[move.id] = i

        _logger.info(f"Created {len(moves)} moves")

        # ── Pre-posting corrections (on draft moves) ──

        # 1. Fix tax amounts via ORM (writes amount_currency, inverse syncs
        #    balance/debit/credit, payment_term auto-rebalances).
        #    Must run BEFORE the qbo_acct_id SQL restore so the sync stack
        #    sees the same base-line state as create().
        fixed_tax = 0
        for move in moves:
            idx = move_index.get(move.id)
            truth = gl_truth.get(idx) if idx is not None else None
            if not truth or "tax_amounts" not in truth:
                continue
            with ctx.skippable(f"fix taxes on {move.ref or move.name or '?'}"):
                fixed_tax += _fix_tax_amounts_orm(
                    move,
                    truth["tax_amounts"],
                    move_vals[idx].get("move_type", "entry"),
                )
        if fixed_tax:
            _logger.info(f"Pre-posting tax fix: {fixed_tax} tax lines adjusted (ORM)")

        # 2. Restore GL accounts on product lines (bulk SQL — safe because
        #    account_id changes don't affect amounts or tax computation).
        if moves:
            ctx.env.cr.execute(
                """
                UPDATE account_move_line
                   SET account_id = qbo_acct_id
                 WHERE qbo_acct_id IS NOT NULL
                   AND account_id <> qbo_acct_id
                   AND move_id IN %s
                """,
                (tuple(moves.ids),),
            )
            restored = ctx.env.cr.rowcount
            if restored:
                _logger.info(f"Restored GL accounts on {restored} lines (pre-posting)")
                ctx.env.invalidate_all()

        # ── Post moves, grouped by journal ──
        posted = 0
        by_journal: Dict[int, "models.Model"] = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post move {move.ref or move.name or '?'}"):
                        move.with_context(
                            skip_cogs_generation=True,
                        ).action_post()
                        posted += 1

        _logger.info(f"Posted {posted} moves")

        # ── Post-posting: fix payment_term account_id → GL truth ──
        corrected_arap = 0
        for move in moves:
            idx = move_index.get(move.id)
            if idx is None or idx not in gl_truth:
                continue
            arap_id = gl_truth[idx].get("arap")
            if arap_id:
                for line in move.line_ids:
                    if line.display_type == "payment_term" and line.account_id.id != arap_id:
                        ctx.env.cr.execute(
                            "UPDATE account_move_line SET account_id = %s WHERE id = %s",
                            (arap_id, line.id),
                        )
                        corrected_arap += 1

        if corrected_arap:
            ctx.env.invalidate_all()
            _logger.info(f"Post-posting: corrected {corrected_arap} AR/AP accounts")

