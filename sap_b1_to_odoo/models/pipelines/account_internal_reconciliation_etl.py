"""ETL pipeline for SAP Internal Reconciliation (OITR / ITR1).

SAP's OITR/ITR1 records groups of journal-line matches -- the equivalent
of an Odoo ``account.full.reconcile`` (or a connected component of
``account.partial.reconcile``).

This pipeline is the **fallback** for groups not covered by the upstream
RCT2/VPM2 payment-application pipelines (see
``account_payment_application_etl``).  Those pipelines handle ~81% of
groups using SAP's pairwise application data; ITR1 catches the rest:

* Direct credit-memo-to-invoice applications (no payment doc).
* Manual reconciliations entered via SAP's *Internal Reconciliation*
  screen.
* Inventory / production / journal-entry account reconciliations.

The load step gathers every member's AR/AP control AML, buckets them by
``account_id``, and calls ``account.move.line.reconcile()`` on each
bucket.  Odoo's stock reconcile machinery handles the partial creation,
exchange-difference moves, and the final ``account.full.reconcile``
stitching -- there is no value in reimplementing it here.
"""

import logging
from collections import defaultdict

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData

from .reconcile_helpers import (
    SRCOBJTYP_MAP,
    pick_open_arap_lines,
    resolve_doc_to_move_map,
)

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.internal.reconciliation",
    sap_source="itr1",
    depends_on=[
        "account.move.jdt1.importer",
        "account.payment.application.rct2.importer",
        "account.payment.application.vpm2.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
    max_workers=4,
)
class AccountInternalReconciliation(models.AbstractModel):
    _name = "account.internal.reconciliation"
    _description = "SAP Internal Reconciliation -- ITR1 fallback reconciler"

    @ETL.extract("itr1")
    def extract_internal_reconciliations(self, ctx: ETLContext):
        """Extract active ITR1 groups and merge cross-group chains.

        SAP can split a single logical settlement event across multiple
        OITR records when a payment / DP / JE bridges two or more matches
        (classic case: a payment that partly clears one invoice and partly
        another invoice via a follow-up correction; SAP records two ITR
        rows joined by the shared payment).  Treating each ``reconnum``
        independently leaves Odoo unable to balance these chains because
        the bridging AML's residual gets exhausted on the first call,
        starving the second.

        We resolve this by computing connected components over the
        "share-a-member" graph: any two ``reconnum`` values that share a
        ``(srcobjtyp, srcobjabs)`` tuple belong to one super-group.  The
        load step then reconciles each super-group's AMLs in one
        ``bucket.reconcile()`` call per account, exactly as the user would
        in Odoo's reconcile widget.

        Most components are tiny (2-9 ITR groups).  A handful of historical
        write-off / cleanup events form components with hundreds of groups;
        those are intentional SAP events and benefit from being reconciled
        as one unit.
        """
        _logger.info("[ITR] Extracting reconciliation groups from SAP...")

        ctx.cr.execute(
            """
            SELECT
                r.reconnum,
                r.lineseq,
                r.srcobjtyp,
                r.srcobjabs::integer AS doc_id,
                r.iscredit,
                r.reconsum   AS reconciled_amount,
                r.reconsumsc AS reconciled_amount_sc,
                r.account
            FROM itr1 r
            JOIN oitr h ON r.reconnum = h.reconnum
            WHERE h.canceled = 'N'
            ORDER BY r.reconnum, r.lineseq
            """
        )
        all_lines = ctx.cr.dictfetchall()

        groups_by_reconnum = defaultdict(list)
        for line in all_lines:
            groups_by_reconnum[line["reconnum"]].append(line)

        _logger.info(
            "[ITR] Found %d ITR1 groups (%d total lines); merging "
            "cross-group chains via union-find...",
            len(groups_by_reconnum), len(all_lines),
        )

        # ---- Union-find: merge groups that share any (srcobjtyp, doc_id) ---
        # Each group's id is its reconnum.  For each (srcobjtyp, doc_id),
        # union all reconnums that contain it.
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Init each reconnum as its own component
        for r in groups_by_reconnum:
            find(r)

        # Union by shared member
        member_to_first_group = {}
        for reconnum, lines in groups_by_reconnum.items():
            for line in lines:
                key = (line["srcobjtyp"], line["doc_id"])
                if key in member_to_first_group:
                    union(reconnum, member_to_first_group[key])
                else:
                    member_to_first_group[key] = reconnum

        # Build super-groups: root -> list of constituent reconnums
        components = defaultdict(list)
        for r in groups_by_reconnum:
            components[find(r)].append(r)

        # Materialize as the same record shape the transform/load expect:
        # a list of {"reconnum": <root>, "lines": [merged lines]} dicts.
        # The "reconnum" field carries the root id (purely for logging).
        super_groups = []
        for root, members in components.items():
            merged_lines = []
            for r in members:
                merged_lines.extend(groups_by_reconnum[r])
            super_groups.append({
                "reconnum": root,
                "constituent_reconnums": members,
                "lines": merged_lines,
            })

        # Stats
        sizes = sorted((len(c) for c in components.values()), reverse=True)
        multi = [s for s in sizes if s > 1]
        _logger.info(
            "[ITR] %d super-groups after merge (%d singletons, %d merged "
            "from %d ITR groups; largest super-group: %d ITR groups, "
            "median multi-group size: %d)",
            len(super_groups),
            len(super_groups) - len(multi),
            len(multi),
            sum(multi),
            sizes[0] if sizes else 0,
            multi[len(multi) // 2] if multi else 0,
        )

        # Build the doc-to-move map across all referenced docs
        doc_ids_by_type = defaultdict(set)
        for line in all_lines:
            srcobjtyp = line["srcobjtyp"]
            if srcobjtyp in SRCOBJTYP_MAP:
                doc_ids_by_type[srcobjtyp].add(line["doc_id"])
        doc_move_map = resolve_doc_to_move_map(
            ctx.cr, ctx.env, doc_ids_by_type
        )
        _logger.info("[ITR] Pre-loaded %d document moves", len(doc_move_map))

        return ChunkableData(
            records=super_groups,
            context={"doc_move_map": doc_move_map},
        )

    @ETL.transform()
    def transform_internal_reconciliations(self, ctx: ETLContext, extracted):
        """Map each ITR group to the moves of its resolvable members.

        Members whose Odoo move can't be resolved are *skipped* rather
        than dropping the whole group.  This is intentional: many SAP ITR
        groups span both AR/AP documents and inventory/production docs
        (Goods Receipts, Production Orders) whose JEs are zero-amount and
        therefore not posted in Odoo.  Dropping such groups would also
        discard any AR/AP reconciliation they happened to carry; instead,
        we let the load step's per-account bucketing naturally skip
        groups that end up with no reconcilable AR/AP lines.

        Groups that resolve to fewer than two distinct moves are dropped
        (nothing to reconcile).
        """
        data = extracted.get("extract_internal_reconciliations")
        groups = data.records if data else []
        cache = data.context if data else {}
        doc_move_map = cache.get("doc_move_map", {})

        out = []
        skipped_members = 0
        dropped_under_two = 0

        for group in groups:
            reconnum = group["reconnum"]
            move_ids = []
            for line in group["lines"]:
                mid = doc_move_map.get((line["srcobjtyp"], line["doc_id"]))
                if mid is None:
                    skipped_members += 1
                    _logger.debug(
                        "[ITR] reconnum %s: skipping member srcobjtyp=%s "
                        "doc_id=%s (no Odoo move)",
                        reconnum, line["srcobjtyp"], line["doc_id"],
                    )
                    continue
                move_ids.append(mid)

            distinct = set(move_ids)
            if len(distinct) >= 2:
                out.append({"reconnum": reconnum, "move_ids": list(distinct)})
            else:
                dropped_under_two += 1

        _logger.info(
            "[ITR] %d groups ready (of %d total); %d members skipped "
            "(no Odoo move, typically zero-amount inventory/production "
            "JEs); %d groups dropped (<2 resolvable members)",
            len(out), len(groups), skipped_members, dropped_under_two,
        )
        return out

    @ETL.load()
    def load_internal_reconciliations(self, ctx: ETLContext, transformed):
        """Reconcile each group's AR/AP lines via ``amls.reconcile()``.

        For each group:

        1. Resolve every member's open AR/AP control AML.
        2. Bucket AMLs by ``account_id`` -- Odoo's ``reconcile()`` requires
           same account; cross-account groups (e.g. AR offset against AP)
           are split and each bucket reconciled independently.
        3. Skip the bucket if it's already linked (idempotency: RCT2/VPM2
           ran first and may have already reconciled this group).
        4. Call ``bucket_amls.reconcile()``.
        """
        groups = transformed.get("transform_internal_reconciliations", [])

        if not groups:
            _logger.info("[ITR] No reconciliation groups in chunk")
            return

        all_move_ids = list({m for g in groups for m in g["move_ids"]})
        moves_by_id = {
            m.id: m for m in ctx.env["account.move"].browse(all_move_ids)
        }

        reconciled_groups = 0
        reconciled_buckets = 0
        skipped_one_sided = 0
        skipped_no_amls = 0

        for group in groups:
            reconnum = group["reconnum"]
            move_ids = group["move_ids"]

            # -- Resolve all open AR/AP AMLs per move ---------------------
            # Multiple lines per move matter for OITR bridge JEs
            # (transtype 321 "Manual Reconciliation Transaction") that post
            # on both an AR and an AP control account: both lines must
            # appear in their respective account buckets.
            #
            # ``pick_open_arap_lines`` filters out already-reconciled AMLs,
            # so super-groups whose AMLs were entirely cleared upstream by
            # RCT2/VPM2 naturally fall through here.
            member_amls = ctx.env["account.move.line"]
            for mid in move_ids:
                move = moves_by_id.get(mid)
                if not move:
                    continue
                member_amls |= pick_open_arap_lines(move)

            if len(member_amls) < 2:
                skipped_no_amls += 1
                continue

            # -- Bucket by account_id -------------------------------------
            by_account = defaultdict(lambda: ctx.env["account.move.line"])
            for aml in member_amls:
                by_account[aml.account_id.id] |= aml

            group_did_anything = False

            for account_id, bucket in by_account.items():
                if len(bucket) < 2:
                    continue

                # NOTE: no per-bucket idempotency early-skip.  Super-groups
                # (post-merge) routinely contain a mix of already-reconciled
                # AMLs (handled upstream by RCT2/VPM2) and not-yet-reconciled
                # AMLs.  ``reconcile()`` itself is idempotent: it walks debit
                # and credit iterators using ``amount_residual``, so AMLs
                # already at zero contribute nothing and no duplicate
                # partials are created.  We track "did anything" via the
                # creation count post-call instead.
                with ctx.skippable(
                    f"ITR group {reconnum} account {account_id}"
                ):
                    bucket.reconcile()
                    reconciled_buckets += 1
                    group_did_anything = True

            if group_did_anything:
                reconciled_groups += 1
            elif len(by_account) == 1:
                skipped_one_sided += 1

        _logger.info(
            "[ITR] Chunk: %d super-groups reconciled (%d buckets), "
            "%d skipped (no AMLs), %d skipped (one-sided)",
            reconciled_groups, reconciled_buckets,
            skipped_no_amls, skipped_one_sided,
        )
