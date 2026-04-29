"""ETL pipelines for SAP payment-application tables (RCT2 / VPM2).

These tables record SAP's pairwise "this payment paid that invoice/credit
memo for $SumApplied" data -- the QBO ``LinkedTxn`` equivalent.  Importing
them produces one ``account.partial.reconcile`` per row, at the exact
amount SAP recorded, mirroring SAP's relationship map in Odoo.

Coverage on the RWI dataset is ~81% of OITR groups (RCT2: 22.5k, VPM2:
18.5k of 50.8k groups).  The remaining ~19% of groups -- direct
credit-memo-to-invoice applications, internal-recon-only entries, JE/
inventory reconciliations -- are picked up by the ITR1 fallback pipeline
in ``account_internal_reconciliation_etl``.

Both pipelines run *before* the ITR1 pipeline; ITR1's idempotency guard
skips groups already linked here.
"""

import logging
from collections import defaultdict

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData

from .reconcile_helpers import (
    SRCOBJTYP_MAP,
    pick_open_arap_line,
    reconcile_at_amount,
    resolve_doc_to_move_map,
)

_logger = logging.getLogger(__name__)


def _extract_applications(ctx, table, payment_srcobjtyp):
    """Read RCT2 or VPM2 and group rows by payment doc.

    Schema notes (RCT2 / VPM2 are SAP's payment-application tables):

    * ``r.docnum`` is the **payment's docnum** (joins ``ORCT.docnum`` /
      ``OVPM.docnum``) -- this is how each row identifies its parent
      payment.  *Not* a foreign key on docentry, despite the name.
    * ``r.docentry`` is the **target document's docentry** (the invoice,
      credit memo, etc. being paid), interpreted via ``r.invtype``.
    * ``r.invoiceid`` is an installment / sub-line index, *not* a doc
      reference -- ~75% of rows have it as 0.  Ignore it.
    * ``r.sumapplied`` is the amount applied, in the payment's document
      currency.

    Args:
        ctx: ETL context (for cursor + env).
        table: ``"rct2"`` or ``"vpm2"``.
        payment_srcobjtyp: ``"24"`` for incoming, ``"46"`` for outgoing
            -- used to look up the payment's account.move.

    Returns:
        ``ChunkableData`` whose ``records`` is a list of dicts, one per
        payment doc, each with ``payment_doc_id`` and ``rows``
        (the application rows).  ``context`` carries the
        ``doc_move_map`` used to translate (srcobjtyp, doc_id) to move ids.
    """
    _logger.info("[%s] Extracting payment applications from SAP...", table.upper())

    parent_table = "orct" if table == "rct2" else "ovpm"
    ctx.cr.execute(
        f"""
        SELECT
            o.docentry        AS payment_doc_id,
            r.docentry        AS target_doc_id,
            r.invtype         AS target_srcobjtyp,
            r.sumapplied      AS sum_applied,
            r.appliedfc       AS applied_fc,
            r.docline         AS docline
        FROM {table} r
        JOIN {parent_table} o ON r.docnum = o.docnum
        WHERE r.sumapplied <> 0
          AND o.canceled = 'N'
        ORDER BY o.docentry, r.docline
        """
    )
    all_rows = ctx.cr.dictfetchall()

    by_payment = defaultdict(list)
    for row in all_rows:
        by_payment[row["payment_doc_id"]].append(row)

    records = [
        {"payment_doc_id": pid, "rows": rows}
        for pid, rows in by_payment.items()
    ]

    # Collect all (srcobjtyp, doc_id) pairs that need move resolution
    doc_ids_by_type = defaultdict(set)
    for row in all_rows:
        # Payment side
        doc_ids_by_type[payment_srcobjtyp].add(int(row["payment_doc_id"]))
        # Target side
        target_typ = str(row["target_srcobjtyp"]).strip()
        if target_typ in SRCOBJTYP_MAP:
            doc_ids_by_type[target_typ].add(int(row["target_doc_id"]))

    doc_move_map = resolve_doc_to_move_map(ctx.cr, ctx.env, doc_ids_by_type)

    _logger.info(
        "[%s] %d application rows across %d payments; resolved %d moves",
        table.upper(), len(all_rows), len(records), len(doc_move_map),
    )

    return ChunkableData(
        records=records,
        context={
            "doc_move_map": doc_move_map,
            "payment_srcobjtyp": payment_srcobjtyp,
        },
    )


def _transform_applications(extracted_key, extracted):
    """Resolve (payment_doc_id, target_doc_id) pairs to (move_id, move_id).

    Drops rows whose payment or target move is missing in Odoo.
    """
    data = extracted.get(extracted_key)
    payments = data.records if data else []
    cache = data.context if data else {}
    doc_move_map = cache.get("doc_move_map", {})
    payment_srcobjtyp = cache.get("payment_srcobjtyp")

    transformed = []
    dropped_payment = 0
    dropped_target = 0

    for payment in payments:
        pid = int(payment["payment_doc_id"])
        payment_move_id = doc_move_map.get((payment_srcobjtyp, pid))
        if payment_move_id is None:
            dropped_payment += 1
            continue

        applications = []
        for row in payment["rows"]:
            target_typ = str(row["target_srcobjtyp"]).strip()
            target_doc_id = int(row["target_doc_id"])
            target_move_id = doc_move_map.get((target_typ, target_doc_id))
            if target_move_id is None:
                dropped_target += 1
                continue
            if target_move_id == payment_move_id:
                # Self-application is a no-op (shouldn't happen but defend)
                continue
            applications.append({
                "target_move_id": target_move_id,
                "amount": abs(float(row["sum_applied"])),
                "applied_fc": (
                    abs(float(row["applied_fc"]))
                    if row["applied_fc"] is not None else None
                ),
                "target_srcobjtyp": target_typ,
            })

        if applications:
            transformed.append({
                "payment_move_id": payment_move_id,
                "applications": applications,
            })

    if dropped_payment or dropped_target:
        _logger.info(
            "Dropped %d payments and %d application rows whose moves were "
            "not found in Odoo",
            dropped_payment, dropped_target,
        )
    return transformed


def _load_applications(ctx, transformed):
    """Create one ``account.partial.reconcile`` per application row.

    For each payment, locate its AR/AP control AML, then for each target
    locate the target's AR/AP control AML and call
    :func:`reconcile_at_amount` at SAP's ``SumApplied``.
    """
    if not transformed:
        return

    move_ids = {p["payment_move_id"] for p in transformed}
    for p in transformed:
        for app in p["applications"]:
            move_ids.add(app["target_move_id"])
    moves_by_id = {m.id: m for m in ctx.env["account.move"].browse(move_ids)}

    reconciled = 0
    no_payment_line = 0
    no_target_line = 0
    already_done = 0
    same_side_skipped = 0

    for payment in transformed:
        pay_move = moves_by_id.get(payment["payment_move_id"])
        if not pay_move:
            continue

        for app in payment["applications"]:
            target_move = moves_by_id.get(app["target_move_id"])
            if not target_move:
                continue

            with ctx.skippable(
                f"reconcile move#{pay_move.id} <-> move#{target_move.id}"
            ):
                pay_line = pick_open_arap_line(pay_move)
                if not pay_line:
                    no_payment_line += 1
                    continue
                tgt_line = pick_open_arap_line(target_move)
                if not tgt_line:
                    no_target_line += 1
                    continue
                if pay_line.account_id != tgt_line.account_id:
                    # Different AR/AP accounts -- can't reconcile pairwise.
                    # This is rare and usually indicates a data issue;
                    # ITR1 fallback may catch it.
                    continue
                # Same-side check: a payment AML is on one side of AR/AP
                # (CR for incoming, DR for outgoing).  Targets that land on
                # the *same* side -- e.g. a credit memo on an incoming
                # payment, or another payment as refund -- are not real
                # pairwise reconciliations: SAP records them in RCT2/VPM2
                # to identify what made up the reconciliation event, but
                # the actual offset goes against the opposite-side targets.
                # Pairing two same-side AMLs in Odoo creates no partial
                # but consumes residual capacity in the load loop's mental
                # model, throwing off subsequent applications.  Defer to
                # ITR1 for these.
                pay_is_credit = pay_line.credit > 0
                tgt_is_credit = tgt_line.credit > 0
                if pay_is_credit == tgt_is_credit:
                    same_side_skipped += 1
                    continue
                # Already linked? (either same partial or same full reconcile)
                existing = ctx.env["account.partial.reconcile"].search([
                    "|",
                    "&", ("debit_move_id", "=", pay_line.id),
                         ("credit_move_id", "=", tgt_line.id),
                    "&", ("debit_move_id", "=", tgt_line.id),
                         ("credit_move_id", "=", pay_line.id),
                ], limit=1)
                if existing:
                    already_done += 1
                    continue

                # Pick reconciliation amount.  ``sum_applied`` is in the
                # payment's document currency; ``applied_fc`` is the foreign
                # currency variant (when the row is in a foreign currency).
                # Use applied_fc when both lines share a non-company currency,
                # else sum_applied.
                amount = app["amount"]
                if (
                    app["applied_fc"]
                    and pay_line.currency_id == tgt_line.currency_id
                    and pay_line.currency_id != pay_line.company_currency_id
                ):
                    amount = app["applied_fc"]

                reconcile_at_amount(pay_line, tgt_line, amount)
                reconciled += 1

    _logger.info(
        "Chunk: %d reconciled, %d already done, %d skipped (no payment "
        "line), %d skipped (no target line), %d skipped (same-side: "
        "credit memo or peer payment, deferred to ITR1)",
        reconciled, already_done, no_payment_line, no_target_line,
        same_side_skipped,
    )


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

@ETL.pipeline(
    target_model="account.partial.reconcile",
    importer_name="account.payment.application.rct2.importer",
    sap_source="rct2",
    depends_on=[
        "account.move.jdt1.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
    max_workers=4,
)
class AccountPaymentApplicationRct2(models.AbstractModel):
    _name = "account.payment.application.rct2"
    _description = "SAP Incoming Payment Applications (RCT2)"

    @ETL.extract("rct2")
    def extract_rct2(self, ctx: ETLContext):
        return _extract_applications(ctx, "rct2", "24")

    @ETL.transform()
    def transform_rct2(self, ctx: ETLContext, extracted):
        return _transform_applications("extract_rct2", extracted)

    @ETL.load()
    def load_rct2(self, ctx: ETLContext, transformed):
        _load_applications(
            ctx, transformed.get("transform_rct2", [])
        )


@ETL.pipeline(
    target_model="account.partial.reconcile",
    importer_name="account.payment.application.vpm2.importer",
    sap_source="vpm2",
    depends_on=[
        "account.move.jdt1.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
    max_workers=4,
)
class AccountPaymentApplicationVpm2(models.AbstractModel):
    _name = "account.payment.application.vpm2"
    _description = "SAP Outgoing Payment Applications (VPM2)"

    @ETL.extract("vpm2")
    def extract_vpm2(self, ctx: ETLContext):
        return _extract_applications(ctx, "vpm2", "46")

    @ETL.transform()
    def transform_vpm2(self, ctx: ETLContext, extracted):
        return _transform_applications("extract_vpm2", extracted)

    @ETL.load()
    def load_vpm2(self, ctx: ETLContext, transformed):
        _load_applications(
            ctx, transformed.get("transform_vpm2", [])
        )
