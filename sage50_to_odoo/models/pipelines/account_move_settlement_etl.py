"""Sage receipts and payments -> settlement moves, reconciled as Sage applied them.

Only with `import_closed_documents`, where every document of the replayed
years is imported rather than just the open ones. The per-application
`account.payment` path (`sage.payment.importer`) does not scale to that: one
receipt settling ten invoices becomes ten payments, an early-payment discount
becomes money in the bank, and an application with no bank account on its row
cannot be placed at all.

So each Sage receipt or payment GL entry is posted **as itself** — every line
Sage posted, bank, discount or anything else, at its own amount — except that
its control line is split into one line per application: the document's
partner, exactly the amount Sage applied, reconciled with that document. The
ledger is Sage's by construction and the reconciliation is Sage's pair by
pair; nothing is left to Odoo's matching.

The move carries the entry's `sage_gl_entry_ref` and is posted in the take-on
journal, so the general-ledger replay, which runs after this, finds it already
there and does not post the entry a second time.
"""

import logging
from collections import defaultdict

from odoo import _, models
from odoo.exceptions import UserError
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

from .account_move_open_item_etl import SIDES

_logger = logging.getLogger(__name__)

#: Settlements posted between commits. Same reasoning as the replay: tens of
#: thousands of moves in one transaction exhaust the cache.
BATCH_SIZE = 200

#: Below this a difference is float noise, not an amount.
TOLERANCE = 0.005


def build_settlement(entry, applications, control_accounts,
                     remainder_partner_id):
    """One Sage GL entry plus its applications -> a settlement move spec.

    `entry["lines"]` are debit-positive, in Sage account numbers. Each
    application is `{"sage_id", "move_id", "partner_id", "control", "amount"}`
    with `amount` debit-positive on the control account.

    Non-control lines are carried over unchanged. Each control account the
    entry touches is replaced by one line per application against it plus, if
    the applications do not account for all of it, one remainder line with
    `remainder_partner_id` and nothing to reconcile.

    Raises ValueError when an application names a control account the entry
    does not touch: the application then belongs to some other entry, and
    forcing it in would unbalance this one.
    """
    control_totals = defaultdict(float)
    lines = []
    for line in entry["lines"]:
        if line["account"] in control_accounts:
            control_totals[line["account"]] += line["balance"]
            continue
        lines.append({
            "account": line["account"],
            "balance": round(line["balance"], 2),
            "label": line.get("label") or "",
            "partner_id": False,
            "reconcile_move_id": False,
        })

    by_control = defaultdict(list)
    for application in applications:
        if application["control"] not in control_totals:
            raise ValueError(
                f"Application {application['sage_id']} is on control "
                f"account {application['control']}, which entry "
                f"{entry['sage_gl_entry_ref']} does not touch."
            )
        by_control[application["control"]].append(application)

    for control, total in control_totals.items():
        applied = 0.0
        for application in by_control.get(control, []):
            amount = round(application["amount"], 2)
            applied += amount
            lines.append({
                "account": control,
                "balance": amount,
                "label": entry["source"] or "",
                "partner_id": application["partner_id"],
                "reconcile_move_id": application["move_id"],
            })
        remainder = round(total - applied, 2)
        if abs(remainder) >= TOLERANCE:
            lines.append({
                "account": control,
                "balance": remainder,
                "label": entry["source"] or "",
                "partner_id": remainder_partner_id or False,
                "reconcile_move_id": False,
            })

    return {
        "sage_gl_entry_ref": entry["sage_gl_entry_ref"],
        "date": entry["date"],
        "ref": f"Sage {entry['source']}" if entry["source"] else False,
        "narration": entry.get("comment") or False,
        "lines": lines,
    }


def build_cash_settlement(entry, payment_accounts, applications, control):
    """The payment half of a document paid on the spot.

    Sage posted the document and its payment as one entry with no control
    line. The document was imported from that entry's expense and tax lines;
    this is the rest of it — the lines on the accounts the money came from,
    exactly as Sage posted them — balanced by the control account split per
    application and reconciled with the document. Document plus settlement
    equal Sage's single entry.

    The reference is derived from the entry's but is not the entry's: that
    one is the document's, and it is what keeps the replay from posting the
    entry again.
    """
    payment_lines = [
        line for line in entry["lines"] if line["account"] in payment_accounts
    ]
    if not payment_lines:
        # Balancing the control account against itself would post nothing
        # and lose the money: the entry's payment line would be claimed by
        # the document and posted by no one.
        raise ValueError(
            f"{entry['sage_gl_entry_ref']}: none of the payment accounts "
            f"{sorted(payment_accounts)} appears in the entry."
        )
    paid = round(sum(line["balance"] for line in payment_lines), 2)
    spec = build_settlement(
        dict(entry, lines=payment_lines + [
            {"account": control, "balance": -paid, "label": ""},
        ]),
        applications, {control},
        applications[0]["partner_id"] if applications else False,
    )
    spec["sage_gl_entry_ref"] = f"{entry['sage_gl_entry_ref']}+payment"
    return spec


def match_applications(candidates, applications, control, claimed):
    """Match one day's applications to one day's entries, by amount.

    `candidates` are the entries sharing the applications' number, partner
    and date; `claimed` the entry references already taken. Tried in order,
    each step on what the previous one left:

    1. the whole group to one entry — one receipt settling several
       documents;
    2. each cheque's applications to one entry — several payments to one
       partner on one day under one number ("Comptant"), which Sage tells
       apart only by cheque id;
    3. each application alone to one entry;
    4. whatever is still unmatched, to the one entry left if there is exactly
       one — the difference is money applied to something not imported and
       stays on a remainder line.

    Returns (list of (entry, applications), unmatched applications). An
    entry is matched at most once.
    """
    taken = set(claimed)
    matched = []

    def control_total(entry):
        return round(sum(
            line["balance"] for line in entry["lines"]
            if line["account"] == control
        ), 2)

    def take(apps):
        applied = round(sum(app["amount"] for app in apps), 2)
        exact = [
            entry for entry in candidates
            if entry["sage_gl_entry_ref"] not in taken
            and abs(control_total(entry) - applied) < 0.01
        ]
        if not exact:
            return False
        # Posted, reversed and re-posted the same day: the latest is live.
        entry = max(exact, key=lambda entry: entry["entry_id"])
        taken.add(entry["sage_gl_entry_ref"])
        matched.append((entry, apps))
        return True

    if take(applications):
        return matched, []

    left = []
    by_cheque = defaultdict(list)
    for app in applications:
        by_cheque[app.get("cheque") or None].append(app)
    for cheque, apps in by_cheque.items():
        if cheque is None or not take(apps):
            left.extend(apps)

    still = [app for app in left if not take([app])]

    free = [
        entry for entry in candidates
        if entry["sage_gl_entry_ref"] not in taken
    ]
    if still and len(free) == 1:
        taken.add(free[0]["sage_gl_entry_ref"])
        matched.append((free[0], still))
        still = []
    return matched, still


@ETL.pipeline(
    target_model="account.move",
    importer_name="sage.settlement.importer",
    sap_source="tcustrdt",
    depends_on=["sage.open.item.importer", "sage.account.importer"],
    allow_multiprocessing=False,
)
class SageSettlementImporter(models.AbstractModel):
    _name = "sage.settlement.importer"
    _description = "Sage 50 Settlement Importer"

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    @ETL.extract("tcustrdt")
    def extract_settlements(self, ctx: ETLContext) -> list:
        if not ctx.get_config("import_closed_documents"):
            # Registered unconditionally; inert unless asked for. Without
            # closed documents the applications go through
            # `sage.payment.importer` as they always have.
            return []
        start = ctx.env["sage.opening.balance.importer"].history_start(ctx)
        controls = ctx.env["sage.open.item.importer"]._control_accounts(ctx)
        documents = self._imported_documents(ctx)
        claimed = {ref for ref, _move, _partner in documents.values() if ref}
        partners = ctx.env["sage.journal.entry.importer"]._partner_map(ctx)
        index, entry_lines = self._gl_index(ctx, start)
        by_ref = {
            entry["sage_gl_entry_ref"]: entry
            for entries in index.values() for entry in entries
        }

        specs, left, cash = [], 0, 0
        for side, spec in SIDES.items():
            control = controls.get(side)
            if not control:
                continue
            cash_documents = {
                key: document for key, document in documents.items()
                if key[0] == side and document[0] in entry_lines
                and not any(
                    line["account"] == control
                    for line in entry_lines[document[0]]
                )
            }
            groups = self._application_groups(ctx, side, spec, control,
                                              documents)
            by_cash_document = defaultdict(list)
            for key in list(groups):
                kept = []
                for application in groups[key]:
                    doc_key = (side, application["doc_id"])
                    if doc_key in cash_documents:
                        by_cash_document[doc_key].append(application)
                    else:
                        kept.append(application)
                if kept:
                    groups[key] = kept
                else:
                    del groups[key]
            opener = ctx.env["sage.open.item.importer"]
            tax_accounts = opener._tax_account_map()
            for doc_key, applications in by_cash_document.items():
                ref = cash_documents[doc_key][0]
                entry = by_ref[ref]
                touched = {line["account"] for line in entry["lines"]}
                payment_accounts = (
                    opener._payment_accounts(ctx, spec, doc_key[1]) & touched
                ) or tools.infer_payment_accounts(
                    entry["lines"], tax_accounts,
                    -round(sum(app["amount"] for app in applications), 2),
                )
                if not payment_accounts:
                    left += len(applications)
                    ctx.report.warning(
                        f"{ref}: paid on the spot, but neither Sage nor the "
                        f"entry says which account the money came from — "
                        f"settle it by hand",
                        source_ref=ref,
                    )
                    continue
                try:
                    specs.append(build_cash_settlement(
                        entry, payment_accounts, applications, control,
                    ))
                except ValueError as error:
                    left += len(applications)
                    ctx.report.warning(str(error), source_ref=ref)
                    continue
                cash += 1

            for key, applications in groups.items():
                pairs, unmatched = match_applications(
                    index.get(key, []), applications, control, claimed,
                )
                for entry, apps in pairs:
                    claimed.add(entry["sage_gl_entry_ref"])
                    try:
                        specs.append(build_settlement(
                            entry, apps, {control},
                            partners.get((spec["module"], key[2])),
                        ))
                    except ValueError as error:
                        left += len(apps)
                        ctx.report.warning(str(error), source_ref=key[0])
                if not unmatched:
                    continue
                if self._is_contra(index.get(key, []), claimed, unmatched):
                    # A credit note applied to an invoice: the amount moves
                    # from one document to the other and Sage posts no entry
                    # at all. Reconcile the documents with each other.
                    specs.append({
                        "contra": sorted({
                            app["move_id"] for app in unmatched
                        }),
                        "ref": key[0],
                    })
                    continue
                left += len(unmatched)
                ctx.report.warning(
                    f"{side} receipt/payment {key[0] or '(no number)'} of "
                    f"{key[3]}: {len(unmatched)} application(s) match no Sage "
                    f"entry — left to the general-ledger replay, unreconciled",
                    source_ref=key[0],
                )
        _logger.info(
            "Sage settlements: %s to post (%s of them paid on the spot), %s "
            "applications left to the replay.", len(specs), cash, left,
        )
        return specs

    def _is_contra(self, candidates, claimed, applications) -> bool:
        """Applications that only move an amount between documents."""
        free = [
            entry for entry in candidates
            if entry["sage_gl_entry_ref"] not in claimed
        ]
        return (
            not free
            and len({app["move_id"] for app in applications}) > 1
            and abs(sum(app["amount"] for app in applications)) < 0.01
        )

    def _imported_documents(self, ctx: ETLContext) -> dict:
        """(side, Sage document id) -> (GL entry ref, move id, partner id)."""
        documents = {}
        for move in ctx.env["account.move"].search([
            ("sage_doc_id", "!=", 0),
            ("company_id", "=", ctx.get_config("company_id")),
            ("state", "=", "posted"),
        ]):
            side = "customer" if move.move_type.startswith("out_") else "vendor"
            documents[(side, move.sage_doc_id)] = (
                move.sage_gl_entry_ref, move.id, move.partner_id.id,
            )
        return documents

    def _application_groups(self, ctx, side, spec, control, documents):
        """Live applications on imported documents, grouped by the Sage entry
        they must have come from: (number, module, tiers id, date).

        The date is part of the key because a receipt number is not unique
        across years, and the lookup that ignores it finds whichever
        generation it searches first.
        """
        partner_of = {
            row["lId"]: row["partner_id"]
            for row in tools.query(
                ctx.cr,
                f"select lId, {spec['partner_fk']} as partner_id "
                f"from {spec['header']}",
            )
        }
        groups = defaultdict(list)
        for row in tools.query(
            ctx.cr,
            f"""select lId, {spec['detail_fk']} as doc_id, dtDate, dAmount,
                       sSource, lChqId
                  from {spec['detail']}
                 where nTranType not in (0, 8) and bReversed = 0
                 order by dtDate, lId""",
        ):
            document = documents.get((side, row["doc_id"]))
            if not document:
                continue
            key = (
                (row["sSource"] or "").strip(),
                spec["module"],
                partner_of.get(row["doc_id"]),
                row["dtDate"].strftime("%Y-%m-%d"),
            )
            groups[key].append({
                "sage_id": row["lId"],
                "doc_id": row["doc_id"],
                "cheque": row["lChqId"] or None,
                "move_id": document[1],
                "partner_id": document[2],
                "control": control,
                "amount": round(
                    tools.signed_amount(control, row["dAmount"]), 2
                ),
            })
        return groups

    def _gl_index(self, ctx, start):
        """Receivable/payable GL entries from the history start, indexed by
        (number, module, tiers id, date), with their debit-positive lines."""
        index, entry_lines = defaultdict(list), {}
        for header, lines in tools.GENERATIONS:
            rows = tools.query(
                ctx.cr,
                f"""select j.lId, j.dtJourDate, j.sSource, j.nModule,
                           j.lRecId, j.sComment, l.lAcctId, l.dAmount,
                           l.szComment
                      from {header} j
                      join {lines} l on l.lJEntId = j.lId
                     where j.nModule in (%s, %s) and j.dtJourDate >= %s
                     order by j.lId, l.nLineNum""",
                (tools.MODULE_PAYABLE, tools.MODULE_RECEIVABLE, start),
            )
            for row in rows:
                ref = tools.entry_ref(header, row["lId"])
                if ref not in entry_lines:
                    entry_lines[ref] = []
                    source = (row["sSource"] or "").strip()
                    date = row["dtJourDate"].strftime("%Y-%m-%d")
                    index[(source, row["nModule"], row["lRecId"], date)].append({
                        "sage_gl_entry_ref": ref,
                        "entry_id": row["lId"],
                        "date": date,
                        "source": source,
                        "comment": (row["sComment"] or "").strip(),
                        "lines": entry_lines[ref],
                    })
                entry_lines[ref].append({
                    "account": row["lAcctId"],
                    "balance": round(
                        tools.signed_amount(row["lAcctId"], row["dAmount"]), 2
                    ),
                    "label": (row["szComment"] or "").strip(),
                })
        return index, entry_lines

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------
    @ETL.transform()
    def transform_settlements(self, ctx: ETLContext, extracted: dict) -> list:
        return extracted["extract_settlements"]

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @ETL.load()
    def load_settlements(self, ctx: ETLContext, transformed: dict) -> None:
        specs = transformed["transform_settlements"]
        if not specs:
            return
        if not ctx.get_config("journal_id"):
            raise UserError(_("Settlements need the take-on journal."))
        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        already = set(ctx.env["account.move"].search([
            ("sage_gl_entry_ref", "!=", False),
            ("company_id", "=", ctx.get_config("company_id")),
            ("journal_id", "=", ctx.get_config("journal_id")),
        ]).mapped("sage_gl_entry_ref"))
        posted = skipped = 0
        Move = ctx.env["account.move"]
        for spec in specs:
            if spec.get("contra"):
                with ctx.skippable(source_ref=spec["ref"]):
                    self._reconcile_documents(ctx, Move.browse(spec["contra"]))
                continue
            if spec["sage_gl_entry_ref"] in already:
                skipped += 1
                continue
            with ctx.skippable(source_ref=spec["ref"] or
                               spec["sage_gl_entry_ref"]):
                self._post_settlement(ctx, spec, accounts)
                posted += 1
                ctx.report.success()
            if posted and not posted % BATCH_SIZE:
                ctx.env.cr.commit()
                ctx.env.invalidate_all()
                _logger.info("Sage settlements: %s posted.", posted)
        _logger.info(
            "Sage settlements: %s posted, %s already present.", posted, skipped,
        )

    def _reconcile_documents(self, ctx: ETLContext, moves) -> None:
        """Reconcile documents with each other on their control accounts."""
        lines = moves.line_ids.filtered(
            lambda line: line.account_id.account_type in (
                "asset_receivable", "liability_payable",
            ) and not line.reconciled
        )
        for account in lines.account_id:
            group = lines.filtered(lambda line: line.account_id == account)
            if group.filtered(lambda line: line.balance > 0) and \
                    group.filtered(lambda line: line.balance < 0):
                group.reconcile()

    def _post_settlement(self, ctx: ETLContext, spec: dict, accounts: dict):
        """Post one settlement and reconcile each split line with its document.

        Lines are created in spec order, so sorting the posted lines by id
        pairs them back with the spec. A split line is reconciled only against
        a document line on the same account and of the opposite sign: a
        returned cheque re-opening a settled invoice debits the receivable
        like the invoice does, and there is nothing for it to settle — it
        stays open, which is what Sage shows too.
        """
        commands = []
        for line in spec["lines"]:
            account_id = accounts.get(line["account"])
            if not account_id:
                raise UserError(_(
                    "No Odoo account for Sage %(account)s, used by %(ref)s.",
                    account=line["account"], ref=spec["sage_gl_entry_ref"],
                ))
            balance = line["balance"]
            commands.append((0, 0, {
                "name": line["label"] or spec["ref"] or "/",
                "account_id": account_id,
                "partner_id": line["partner_id"] or False,
                "debit": balance if balance > 0 else 0.0,
                "credit": -balance if balance < 0 else 0.0,
            }))
        move = ctx.env["account.move"].create({
            "move_type": "entry",
            "journal_id": ctx.get_config("journal_id"),
            "date": spec["date"],
            "ref": spec["ref"],
            "narration": spec["narration"],
            "sage_gl_entry_ref": spec["sage_gl_entry_ref"],
            "company_id": ctx.get_config("company_id"),
            "line_ids": commands,
        })
        move.action_post()

        Move = ctx.env["account.move"]
        for spec_line, line in zip(spec["lines"], move.line_ids.sorted("id")):
            if not spec_line["reconcile_move_id"]:
                continue
            document = Move.browse(spec_line["reconcile_move_id"])
            targets = document.line_ids.filtered(
                lambda target: target.account_id == line.account_id
                and not target.reconciled
                and (target.balance > 0) != (line.balance > 0)
            )
            if targets:
                (line | targets).reconcile()
            else:
                ctx.report.warning(
                    f"{spec['ref']}: nothing open on {document.name} to "
                    f"settle {line.balance:,.2f} against",
                    source_ref=spec["sage_gl_entry_ref"],
                )
        return move
