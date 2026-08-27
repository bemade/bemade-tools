"""Sage 50 open receivables and payables -> posted `account.move`.

Sage keeps a document in `tcustr` / `tventr` and every application against it
in `tcustrdt` / `tventrdt`; the residual is the sum of `dAmount` over a
document's detail rows. Reconstructed that way the two totals tie exactly to
their control accounts, which is what makes this the source of truth for a
take-on rather than the ageing report.

There are usually no item lines to migrate — `bHasDetail` is 0 on every
document in most files, because Sage invoices were coded straight to GL
accounts. **The GL entry therefore *is* the line detail**, and it carries the
real revenue and expense accounts and the real tax amounts, which is exactly
what Sage's own CSV export drops.

Partially paid documents are imported at their residual with the non-control
lines scaled pro rata. The alternative — importing the original amount and
re-entering the payments — needs bank entries that have no counterpart in
Odoo yet, and the counter-entry neutralises the revenue side either way.
"""

import itertools
import logging

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)

#: Anything under this is rounding, not a difference worth a human looking.
TOLERANCE = 0.005

#: Matching a subset of base lines to the taxable base is exponential in the
#: number of lines. Documents have one to six; this guards a pathological one.
MAX_SUBSET_LINES = 12

#: How a debit-positive line amount maps onto `price_unit`, which says how
#: much a line ADDS to the document total.
MOVE_TYPE_SIGN = {
    "out_invoice": -1,   # revenue is a credit; the receivable is the debit
    "out_refund": 1,
    "in_invoice": 1,     # expense is a debit; the payable is the credit
    "in_refund": -1,
}

SIDES = {
    "customer": {
        "header": "tcustr",
        "detail": "tcustrdt",
        "detail_fk": "lCusTrId",
        "partner_fk": "lCusId",
        "partner_table": "tcustomr",
        "module": tools.MODULE_RECEIVABLE,
        "account_type": "asset_receivable",
        "move_type": {"invoice": "out_invoice", "refund": "out_refund"},
        "normal_side": 1,
    },
    "vendor": {
        "header": "tventr",
        "detail": "tventrdt",
        "detail_fk": "lVenTrId",
        "partner_fk": "lVenId",
        "partner_table": "tvendor",
        "module": tools.MODULE_PAYABLE,
        "account_type": "liability_payable",
        "move_type": {"invoice": "in_invoice", "refund": "in_refund"},
        "normal_side": -1,
    },
}


def base_tolerance(total: float) -> float:
    """How far a derived taxable base may sit from a candidate.

    Dividing a rounded tax amount by its rate magnifies the rounding: a
    half-cent of GST becomes a dime of base. On top of that, a partly paid
    document has had its lines scaled pro rata, which moves the base again.
    So the band is a fixed floor for the rounding plus a proportional term
    for the scaling — still nowhere near wide enough to swallow a genuinely
    part-taxable document, where the gap is a large fraction of the base.
    """
    return max(0.15, 0.002 * abs(total))


@ETL.pipeline(
    target_model="account.move",
    importer_name="sage.open.item.importer",
    sap_source="tcustr",
    depends_on=["sage.partner.importer", "sage.account.importer"],
    allow_multiprocessing=False,
)
class SageOpenItemImporter(models.AbstractModel):
    _name = "sage.open.item.importer"
    _description = "Sage 50 Open Receivable and Payable Importer"

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------
    def _tax_account_map(self) -> dict:
        """Sage tax GL account -> (side, kind).

        `side` is "sale" or "purchase", `kind` is the tax's own name — "gst",
        "qst", "hst", "pst". Empty by default: which account holds which tax
        is a property of the client's chart. With it empty, tax lines are
        treated as ordinary base lines, which gets the general ledger right
        and the tax return wrong, so a Canadian file wants this filled in.
        """
        return {}

    def _tax_rate_candidates(self) -> list:
        """Rate maps to try when working out how much of a document was taxed.

        A list of {kind: rate} dicts, tried in order. The default is Québec's
        GST/QST, then the pair Sage writes for the 50 % input-tax-credit
        restriction on meals and entertainment. The two cannot be told apart
        by the ratio between the taxes — halving both leaves it unchanged —
        so the full rate is tried first and the halved one only if the full
        one implies more taxable base than the document has.
        """
        return [
            {"gst": 0.05, "qst": 0.09975},
            {"gst": 0.025, "qst": 0.049875},
        ]

    def _tax_combinations(self) -> dict:
        """Which Odoo tax a document's set of Sage tax kinds implies.

        Keyed by the frozenset of kinds found on the GL entry, then by the
        base move type. Values are `account.tax` xmlid suffixes, resolved
        against the company as `account.<company_id>_<suffix>`.
        """
        return {
            frozenset({"gst", "qst"}): {
                "out_invoice": "gstqst_sale_tax_14975",
                "in_invoice": "gstqst_purchase_tax_14975",
            },
            frozenset({"gst"}): {
                "out_invoice": "gst_sale_tax_5",
                "in_invoice": "gst_purchase_tax_5",
            },
            frozenset({"qst"}): {
                "out_invoice": "qst_sale_tax_9975",
                "in_invoice": "qst_purchase_tax_9975",
            },
        }

    def _control_accounts(self, ctx: ETLContext) -> dict:
        """Side -> the Sage number of that side's control account.

        Derived from the imported chart rather than configured, so the only
        place a control account is named is the account-type override table.
        """
        company_id = ctx.get_config("company_id")
        mapping = {}
        for side, spec in SIDES.items():
            account = ctx.env["account.account"].search([
                ("company_ids", "in", company_id),
                ("account_type", "=", spec["account_type"]),
                ("sage_account_id", "!=", 0),
            ], limit=1)
            if account:
                mapping[side] = account.sage_account_id
        return mapping

    # ------------------------------------------------------------------
    # Taxable-base arithmetic
    # ------------------------------------------------------------------
    def _taxable_base(self, tax_lines: list, total_base: float):
        """How much of the document was actually taxed.

        Divided out of the tax amount rather than assumed to be the whole
        base: a bill can be part taxable and part not, and it usually is when
        food is involved. Returns None when the tax lines imply no sensible
        base.
        """
        for rates in self._tax_rate_candidates():
            for kind, rate in rates.items():
                amounts = [
                    line["amount"] for line in tax_lines
                    if line["tax"] == kind
                ]
                if not amounts or not rate:
                    continue
                base = round(sum(amounts) / rate, 2)
                # Same rounding band as the matching below: dividing a rounded
                # tax by its rate can put the derived base slightly *above*
                # the document's own, which is not a reason to reject it.
                if abs(base) <= abs(total_base) + base_tolerance(total_base):
                    return base
        return None

    def _split_taxable(self, base_lines: list, target: float) -> bool:
        """Mark which base lines carry the tax, splitting one if need be.

        Three cases, in order of how much they disturb the document: the whole
        base is taxable; some subset of the lines is exactly the taxable base;
        or one line has to be split, because part of a single line was taxed.
        All three occur in real files.

        Returns False when the target cannot be reached, leaving the caller to
        report it rather than guess.
        """
        total = round(sum(line["amount"] for line in base_lines), 2)
        if abs(total - target) < base_tolerance(total):
            for line in base_lines:
                line["taxable"] = True
            return True

        if len(base_lines) <= MAX_SUBSET_LINES:
            for size in range(1, len(base_lines)):
                for combination in itertools.combinations(base_lines, size):
                    subtotal = sum(line["amount"] for line in combination)
                    if abs(subtotal - target) < base_tolerance(subtotal):
                        for line in base_lines:
                            line["taxable"] = line in combination
                        return True

        # Split the largest line, which is where a mixed bill puts the taxable
        # part.
        largest = max(base_lines, key=lambda line: abs(line["amount"]))
        if abs(target) > abs(largest["amount"]) + base_tolerance(
            largest["amount"]
        ):
            return False
        for line in base_lines:
            line["taxable"] = False
        largest["amount"] = round(largest["amount"] - target, 2)
        base_lines.append({
            "account": largest["account"],
            "amount": target,
            "label": largest.get("label"),
            "taxable": True,
        })
        return True

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    @ETL.extract("tcustr")
    def extract_open_items(self, ctx: ETLContext) -> list:
        controls = self._control_accounts(ctx)
        tax_accounts = self._tax_account_map()
        staged = []
        for side, spec in SIDES.items():
            control = controls.get(side)
            if not control:
                _logger.error(
                    "No imported %s control account; %s documents cannot be "
                    "imported. Name it in the account-type overrides.",
                    spec["account_type"], side,
                )
                continue
            staged.extend(
                self._collect(ctx, side, spec, control, tax_accounts)
            )
        return staged

    def _collect(self, ctx, side, spec, control, tax_accounts) -> list:
        open_docs = tools.query(
            ctx.cr,
            f"""select d.{spec['detail_fk']} as doc_id,
                       round(sum(d.dAmount), 2) as residual,
                       round(sum(case when d.nTranType in (0, 8)
                                      then d.dAmount else 0 end), 2) as original
                  from {spec['detail']} d
                 group by d.{spec['detail_fk']}
                having abs(round(sum(d.dAmount), 2)) > %s""",
            (TOLERANCE,),
        )

        staged = []
        for doc in open_docs:
            headers = tools.query(
                ctx.cr,
                f"""select h.lId, h.{spec['partner_fk']} as partner_id,
                           h.dtDate, h.sSource, h.nTranType, h.nNetDay, h.sRef,
                           p.sName as partner_name
                      from {spec['header']} h
                      join {spec['partner_table']} p
                        on p.lId = h.{spec['partner_fk']}
                     where h.lId = %s""",
                (doc["doc_id"],),
            )
            if not headers:
                ctx.report.warning(
                    f"{side} document {doc['doc_id']} has no header row",
                    source_ref=str(doc["doc_id"]),
                )
                continue
            header = headers[0]

            # Both detail tables store amounts in the control account's
            # natural side, so the expected control amount is the document's
            # original amount exactly as Sage recorded it — on both sides.
            # Negating it on the payable side silently picks the wrong entry
            # whenever a bill has been posted, reversed and reposted: the pair
            # are exact mirrors, and the reversal matches just as well.
            gl_lines = tools.journal_entry(
                ctx.cr, header["sSource"], spec["module"], header["partner_id"],
                control_account=control, expected_control=doc["original"],
            )
            if not gl_lines:
                ctx.report.warning(
                    f"{side} {header['sSource']} ({header['partner_name']}): "
                    f"no GL entry in any generation — code manually",
                    source_ref=header["sSource"],
                )

            base_lines, tax_lines, control_total = [], [], 0.0
            for line in gl_lines:
                account = line["lAcctId"]
                amount = tools.signed_amount(account, line["dAmount"])
                if account == control:
                    control_total += amount
                elif account in tax_accounts:
                    tax_lines.append({
                        "account": account,
                        "amount": round(amount, 2),
                        "tax": tax_accounts[account][1],
                    })
                else:
                    base_lines.append({
                        "account": account,
                        "amount": round(amount, 2),
                        "label": (line["szComment"] or "").strip() or None,
                    })

            # Everything below is debit-positive, so both sides can be
            # reasoned about the same way: a receivable is a debit, a payable
            # a credit.
            residual = tools.signed_amount(control, doc["residual"])

            ratio = 1.0
            if control_total and abs(control_total - residual) > TOLERANCE:
                ratio = residual / control_total
                for line in base_lines + tax_lines:
                    line["amount"] = round(line["amount"] * ratio, 2)

            for line in base_lines:
                line["taxable"] = not tax_lines
            if tax_lines and base_lines:
                base_total = round(
                    sum(line["amount"] for line in base_lines), 2
                )
                target = self._taxable_base(tax_lines, base_total)
                if target is None or not self._split_taxable(
                    base_lines, target
                ):
                    ctx.report.warning(
                        f"{side} {header['sSource']} "
                        f"({header['partner_name']}): cannot work out which "
                        f"lines carry the tax — taxing the whole base, check "
                        f"it by hand",
                        source_ref=header["sSource"],
                    )
                    for line in base_lines:
                        line["taxable"] = True

            # Whether a document is a credit note is decided by the sign of
            # what it leaves outstanding, not by `nTranType`. Sage records
            # some customer deductions as an ordinary invoice (`nTranType` 0)
            # carrying negative amounts throughout, and Odoo refuses to post
            # an invoice with a negative total — rightly, since it is a credit
            # note.
            is_refund = residual * spec["normal_side"] < 0
            staged.append({
                "side": side,
                "sage_doc_id": header["lId"],
                "sage_partner_id": header["partner_id"],
                "partner_name": header["partner_name"],
                "move_type": spec["move_type"][
                    "refund" if is_refund else "invoice"
                ],
                "number": header["sSource"],
                "reference": (header["sRef"] or "").strip() or None,
                "date": header["dtDate"].strftime("%Y-%m-%d"),
                "residual": abs(round(residual, 2)),
                "base_lines": base_lines,
                "tax_lines": tax_lines,
            })

            # The staged lines must add up to the residual, or the document
            # would post an unbalanced move. Checked here, where the fix is
            # cheap.
            staged_total = -sum(
                line["amount"] for line in base_lines + tax_lines
            )
            if gl_lines and abs(staged_total - residual) > 0.02:
                ctx.report.warning(
                    f"{side} {header['sSource']} ({header['partner_name']}): "
                    f"lines total {staged_total:.2f} but the residual is "
                    f"{residual:.2f}",
                    source_ref=header["sSource"],
                )
        return staged

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------
    @ETL.transform()
    def transform_open_items(self, ctx: ETLContext, extracted: dict) -> list:
        return extracted["extract_open_items"]

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    def _taxes_for(self, ctx: ETLContext, document: dict):
        """The Odoo tax a document's Sage tax lines imply.

        Taxes are attached to the base lines rather than posted as raw journal
        lines on the tax accounts. Both get the general ledger right, but only
        this one stamps the tax grids — and without grids the take-on invoices
        are invisible to the GST/QST report, which then cannot be neutralised
        by the counter-entry either, because there is nothing there to
        reverse.

        A document with no tax lines gets no tax, not the company default: the
        default exists for what people type in later, and forcing it onto a
        document Sage recorded as untaxed would invent a number.
        """
        empty = ctx.env["account.tax"]
        kinds = {line["tax"] for line in document["tax_lines"]}
        if not kinds:
            return empty
        combination = self._tax_combinations().get(frozenset(kinds))
        if not combination:
            return empty
        # A refund uses the same tax as the document it reverses.
        base_type = (
            "out_invoice" if document["move_type"].startswith("out_")
            else "in_invoice"
        )
        company_id = ctx.get_config("company_id")
        return ctx.env.ref(
            f"account.{company_id}_{combination[base_type]}",
            raise_if_not_found=False,
        ) or empty

    @ETL.load()
    def load_open_items(self, ctx: ETLContext, transformed: dict) -> None:
        Move = ctx.env["account.move"]
        company_id = ctx.get_config("company_id")
        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        partners = {
            ("customer", partner.sage_customer_id): partner.id
            for partner in ctx.env["res.partner"].search(
                [("sage_customer_id", "!=", 0)]
            )
        }
        partners.update({
            ("vendor", partner.sage_vendor_id): partner.id
            for partner in ctx.env["res.partner"].search(
                [("sage_vendor_id", "!=", 0)]
            )
        })
        already = set(Move.search([
            ("sage_doc_id", "!=", 0), ("company_id", "=", company_id),
        ]).mapped("sage_doc_id"))

        created = skipped = 0
        totals = {"customer": 0.0, "vendor": 0.0}
        for document in transformed["transform_open_items"]:
            if document["sage_doc_id"] in already:
                skipped += 1
                continue
            partner_id = partners.get(
                (document["side"], document["sage_partner_id"])
            )
            if not partner_id:
                ctx.report.failure(
                    f"No partner for Sage {document['side']} "
                    f"{document['sage_partner_id']} "
                    f"({document['partner_name']})",
                    source_ref=document["number"],
                )
                continue

            # The staged amounts are debit-positive. On an Odoo invoice the
            # move type carries the document's sign and `price_unit` says how
            # much a line ADDS to the total, so each line needs one sign flip
            # that depends only on the move type. Taking abs() per line
            # instead would look right on a simple invoice and quietly flip
            # every negative line — the discounts and the credit lines — on
            # the ones that have them.
            sign = MOVE_TYPE_SIGN[document["move_type"]]
            taxes = self._taxes_for(ctx, document)
            empty_tax = ctx.env["account.tax"]
            lines, missing = [], None
            for line in document["base_lines"]:
                account_id = accounts.get(line["account"])
                if not account_id:
                    missing = line["account"]
                    break
                # Only the lines the extract found to be taxed. A bill can be
                # part taxable and part not, and taxing the whole base
                # overstates the return.
                line_taxes = taxes if line.get("taxable", True) else empty_tax
                lines.append((0, 0, {
                    "name": line.get("label") or document["number"],
                    "account_id": account_id,
                    "quantity": 1.0,
                    "price_unit": sign * line["amount"],
                    "tax_ids": [(6, 0, line_taxes.ids)],
                }))
            if missing is not None:
                ctx.report.failure(
                    f"No Odoo account for Sage {missing}",
                    source_ref=document["number"],
                )
                continue
            if not lines:
                ctx.report.failure(
                    "No lines — the GL entry could not be found",
                    source_ref=document["number"],
                )
                continue

            move = Move.create({
                "move_type": document["move_type"],
                "partner_id": partner_id,
                "invoice_date": document["date"],
                "date": document["date"],
                "ref": f"Sage {document['number']}",
                "sage_doc_id": document["sage_doc_id"],
                "company_id": company_id,
                "invoice_line_ids": lines,
            })
            move.action_post()
            created += 1
            ctx.report.success()
            totals[document["side"]] += abs(move.amount_total)

            staged_tax = round(
                sum(abs(line["amount"]) for line in document["tax_lines"]), 2
            )
            if abs(round(move.amount_tax, 2) - staged_tax) > 0.02:
                ctx.report.warning(
                    f"Odoo computed {move.amount_tax:.2f} of tax, Sage "
                    f"recorded {staged_tax:.2f}",
                    source_ref=document["number"],
                )
            if abs(abs(move.amount_total) - document["residual"]) > 0.02:
                ctx.report.warning(
                    f"Posted {move.amount_total:.2f} against a staged "
                    f"residual of {document['residual']:.2f}",
                    source_ref=document["number"],
                )

        _logger.info(
            "Sage open items: %s posted, %s already present. "
            "AR %.2f / AP %.2f.",
            created, skipped, totals["customer"], totals["vendor"],
        )
