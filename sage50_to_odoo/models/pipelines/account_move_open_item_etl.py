"""Sage 50 open receivables and payables -> posted `account.move`.

Sage keeps a document in `tcustr` / `tventr` and every application against it
in `tcustrdt` / `tventrdt`; the residual is the sum of `dAmount` over a
document's detail rows. Reconstructed that way the two totals tie exactly to
their control accounts, which is what makes this the source of truth for a
take-on rather than the ageing report.

**`tcustr.bHasDetail` is 0 on essentially every document, and it does not
mean the document has no lines.** It says the *receivable record* carries no
detail. The item lines live in `titrec` / `titrline`, keyed on `sSource1` +
`lVenCusId` rather than on the receivable's own id — which is why joining
from the document finds nothing and the GL entry looks like the only source
of line detail. It is not: `titrline` carries the item, the quantity and the
unit price, none of which survives into the GL.

Documents are imported at what Sage says they WERE, not at what is left on
them, because an imported document has to match the paper the client filed.
Anything already applied against one is re-created as a payment of its own
and reconciled, which is what brings the residual back.

Two amount conventions meet here and they are not the same. `tjentact.dAmount`
is signed in the ACCOUNT's natural side; `titrline.dAmt` is signed the way the
DOCUMENT reads. They agree whenever the account's natural side matches the
document's — most lines — and disagree silently on the rest.
"""

import itertools
import logging
from datetime import timedelta

from odoo import fields, models
from odoo.tools.float_utils import float_compare, float_round
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

    def _item_lines(self, ctx, source, partner_id, tax_accounts, original,
                    document_sign):
        """A document's item lines from `titrec` / `titrline`.

        This is the table pair `tcustr.bHasDetail = 0` does NOT tell you
        about. That flag says the *receivable* record carries no detail, and
        it is 0 on essentially every document; the item lines live here
        instead, keyed on `sSource1` + `lVenCusId` rather than on the
        receivable's own id, which is why joining from the document finds
        nothing and the GL entry looks like the only source.

        `dAmt` is pre-tax — `titrec.dInvAmt` is the tax-inclusive total — so
        these lines replace the GL entry's base lines only, and the tax still
        comes from the GL entry.

        Lines carry `lInventId` when the document was written against an
        item and 0 when it was coded straight to an account; both occur in
        the same document. Returns None when the document has no `titrec`
        row at all, so the caller keeps the GL entry's lines.
        """
        records = tools.query(
            ctx.cr,
            """select lId, dInvAmt, dFreight from titrec
                where sSource1 = %s and lVenCusId = %s
                order by lId""",
            (source, partner_id),
        )
        if not records:
            return None
        # A document number is not unique here either: an order and the
        # invoice raised from it both land in `titrec` under the same
        # `sSource1`. Concatenating them doubles the document. Pick the record
        # whose own total matches what the document was worth — the same
        # disambiguation the GL lookup uses — and fall back to the GL entry
        # when none does, rather than guessing.
        matching = [
            record for record in records
            if abs(abs(record["dInvAmt"]) - abs(original)) < 0.02
        ]
        if not matching:
            return None
        lines = []
        for record in matching[-1:]:
            for row in tools.query(
                ctx.cr,
                """select l.nLineNum, l.lInventId, l.lAcctId, l.dQty,
                          l.dPrice, l.dAmt
                     from titrline l
                    where l.lITRecId = %s order by l.nLineNum""",
                (record["lId"],),
            ):
                # Sage pads documents with empty filler rows. Some of them
                # carry an item id and nothing else — no amount, no quantity
                # and no account — so the item alone does not make a line
                # real.
                if not row["dAmt"] and not row["dQty"]:
                    continue
                account = row["lAcctId"]
                if account in tax_accounts:
                    continue
                lines.append({
                    "account": account,
                    # `dAmt` is signed the way the DOCUMENT reads — positive
                    # adds to the invoice — which is NOT how `tjentact.dAmount`
                    # is signed. GL lines are stored in the account's natural
                    # side and need `signed_amount`; these need the document's
                    # side instead. The two rules agree whenever the account's
                    # natural side matches the document's, which is most lines,
                    # and disagree silently on the rest — a liability account
                    # on a bill, an asset account on an invoice.
                    "amount": round(document_sign * row["dAmt"], 2),
                    "sage_product_id": row["lInventId"] or 0,
                    "quantity": row["dQty"] or 0.0,
                    "price_unit": row["dPrice"] or 0.0,
                    "label": None,
                })
        return lines or None

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
            #
            # The document is imported at what Sage says it WAS, not at what
            # is left on it. Anything already applied against it arrives as a
            # payment of its own and is reconciled, which is the only way the
            # imported document matches the paper the client filed.
            original = tools.signed_amount(control, doc["original"])
            residual = tools.signed_amount(control, doc["residual"])

            # Sage's item lines, where it has them. They carry the product,
            # the quantity and the unit price, none of which survives in the
            # GL entry — the GL only knows the account and the amount. Where
            # a document has no item lines the GL entry is still the source.
            item_lines = self._item_lines(
                ctx, header["sSource"], header["partner_id"], tax_accounts,
                doc["original"], -spec["normal_side"],
            )
            if item_lines is not None:
                base_lines = item_lines

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
            # Decided on what the document WAS, not on what is left of it: a
            # partly paid invoice is still an invoice. Sage records some
            # customer deductions as an ordinary invoice (`nTranType` 0)
            # carrying negative amounts throughout, and Odoo refuses to post
            # an invoice with a negative total — rightly, since it is a credit
            # note — so the sign decides, not `nTranType`.
            is_refund = original * spec["normal_side"] < 0
            staged.append({
                "original": abs(round(original, 2)),
                "applications": self._applications(
                    ctx, spec, doc["doc_id"], control, header["partner_id"]
                ),
                # Sage records the payment terms on the DOCUMENT, not on the
                # vendor — the vendor master is routinely 0 on every row while
                # the bills carry real net days. Take the document's, or the
                # ageing the take-on is for comes out wrong.
                "payment_term_days": header["nNetDay"] or 0,
                # The GL entry's own description. Sage leaves the per-LINE
                # comment blank on every line in a file (verified: 8,756 of
                # 8,756 payable lines), so the header comment is the only
                # free text a document has.
                "description": (
                    (gl_lines[0]["sComment"] or "").strip() if gl_lines else ""
                ),
                "side": side,
                "sage_doc_id": header["lId"],
                # The GL entry Sage posted behind this document. The document
                # is about to be re-created as a real invoice, so the
                # general-ledger replay must skip that entry or the same
                # revenue lands twice.
                "sage_gl_entry_id": gl_lines[0]["lId"] if gl_lines else 0,
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

            # The staged lines must add up to the document's original amount,
            # or the move posts for the wrong total. Checked here, where the
            # fix is cheap.
            staged_total = -sum(
                line["amount"] for line in base_lines + tax_lines
            )
            if gl_lines and abs(staged_total - original) > 0.02:
                ctx.report.warning(
                    f"{side} {header['sSource']} ({header['partner_name']}): "
                    f"lines total {staged_total:.2f} but the document was "
                    f"{original:.2f}",
                    source_ref=header["sSource"],
                )
        return staged

    def _applications(self, ctx, spec, doc_id, control, rec_id) -> list:
        """What has already been applied against a document.

        Receipts, payments and applied credit notes, each with the bank
        account and cheque number Sage recorded, so they can be re-created as
        real payments and reconciled rather than netted off the document.

        Types 0 and 8 are the document's OWN rows — an invoice and a credit
        note respectively — not applications against it. Reading a credit
        note's type-8 row as an application makes it pay itself, and the
        document is then counted twice.

        `bReversed` rows are skipped in pairs: Sage keeps both the original
        application and its reversal, which net to zero. Importing them would
        create two payments that cancel out and reconcile against nothing.
        """
        return [
            {
                "sage_id": row["lId"],
                "date": row["dtDate"].strftime("%Y-%m-%d"),
                # Debit-positive like everything else, so an application
                # against a receivable is negative and one against a payable
                # positive.
                "amount": round(
                    tools.signed_amount(control, row["dAmount"]), 2
                ),
                "reference": (row["sSource"] or "").strip() or None,
                "sage_bank_account": row["lBnkAcctId"] or 0,
                "cheque_id": row["lChqId"] or 0,
                # The GL entry Sage posted for the receipt or payment itself.
                # It is about to become a real `account.payment`, so the
                # replay must skip it or the bank moves twice. One receipt
                # settling several invoices is one entry against several
                # application rows, which is why the replay excludes a SET of
                # entry ids rather than pairing them off one to one.
                "sage_gl_entry_id": self._application_entry_id(
                    ctx, spec, row, control, rec_id
                ),
            }
            for row in tools.query(
                ctx.cr,
                f"""select lId, dtDate, dAmount, sSource, nTranType,
                           lBnkAcctId, lChqId
                      from {spec['detail']}
                     where {spec['detail_fk']} = %s
                       and nTranType not in (0, 8) and bReversed = 0
                     order by dtDate, lId""",
                (doc_id,),
            )
        ]

    def _application_entry_id(self, ctx, spec, row, control, rec_id) -> int:
        """The id of the GL entry behind one application, or 0 if unfound.

        Disambiguated on the application's own amount against the control
        account, the same way a document is: a receipt number is no more
        unique than an invoice number, and a reversed-and-reissued receipt
        leaves two entries that are exact mirrors.

        Returning 0 is not an error — an application settled by a credit
        note rather than by money has no separate GL entry of its own, and
        the replay then has nothing to skip.
        """
        source = (row["sSource"] or "").strip()
        if not source:
            return 0
        lines = tools.journal_entry(
            ctx.cr, source, spec["module"], rec_id,
            control_account=control, expected_control=row["dAmount"],
        )
        return lines[0]["lId"] if lines else 0

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
        precision = ctx.env["decimal.precision"].precision_get("Product Price")
        # Odoo stores the quantity at the UoM precision and recomputes the
        # subtotal from the STORED value, so a quantity with more decimals
        # than that (27.305 kg) silently moves the line total. The check below
        # is made against the rounded quantity, not the one Sage supplied.
        #
        # Asked of the FIELD, not of `decimal.precision`. The two disagree
        # whenever the precision has been changed in the running process: the
        # table is updated immediately, the field keeps the digits it was
        # built with until the registry reloads. Trusting the table then lets
        # through a quantity the field is about to re-round, which is exactly
        # the drift this guard exists to catch.
        qty_precision = ctx.env["account.move.line"]._fields[
            "quantity"
        ].get_digits(ctx.env)[1]
        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        # Several Sage numbers can share one Odoo account (the tax aliases
        # do), so this is built off the deduplicated ids rather than by
        # zipping two sequences that are not the same length.
        names_by_id = {
            account.id: account.name
            for account in ctx.env["account.account"].browse(
                set(accounts.values())
            )
        }
        account_names = {
            sage_id: names_by_id.get(account_id)
            for sage_id, account_id in accounts.items()
        }
        products = {
            product.sage_product_id: product.product_variant_id.id
            for product in ctx.env["product.template"].with_context(
                active_test=False
            ).search([("sage_product_id", "!=", 0)])
            if product.product_variant_id
        }
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
        # A document raised before the history starts is already inside the
        # control balance the opening entry carries, so posting it as an
        # invoice as well counts it twice. Nothing downstream would catch
        # it: the trial balance ties account by account either way, because
        # the opening entry and the duplicate sit on the same account.
        history_start = ctx.env[
            "sage.opening.balance.importer"
        ].history_start(ctx)
        for document in transformed["transform_open_items"]:
            if document["sage_doc_id"] in already:
                skipped += 1
                continue
            if history_start and document["date"] < history_start:
                ctx.report.failure(
                    f"{document['side']} {document['number']} is dated "
                    f"{document['date']}, before the history starts on "
                    f"{history_start}. Its balance is already in the opening "
                    f"entry — importing it as a document too would count it "
                    f"twice. Start the history earlier, or code it manually.",
                    source_ref=document["number"],
                )
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
            due_date = fields.Date.from_string(document["date"]) + timedelta(
                days=document.get("payment_term_days") or 0
            )
            empty_tax = ctx.env["account.tax"]
            lines, missing = [], None
            for line in document["base_lines"]:
                account_id = accounts.get(line["account"])
                if not account_id and not products.get(
                    line.get("sage_product_id")
                ):
                    missing = line["account"]
                    break
                # Only the lines the extract found to be taxed. A bill can be
                # part taxable and part not, and taxing the whole base
                # overstates the return.
                line_taxes = taxes if line.get("taxable", True) else empty_tax
                values = {
                    # Sage leaves the per-line GL comment blank, and a line
                    # coded straight to an account IS what that account says.
                    # Falling back to the document number just repeats the
                    # reference on every line and says nothing.
                    "name": (
                        line.get("label")
                        or account_names.get(line["account"])
                        or document["number"]
                    ),
                    # Left out when the line has a product and Sage recorded
                    # no account: the product's category then decides, which
                    # is what Odoo would do for a line typed by hand.
                    "account_id": account_id or False,
                    "quantity": 1.0,
                    "price_unit": sign * line["amount"],
                    "tax_ids": [(6, 0, line_taxes.ids)],
                }
                product_id = products.get(line.get("sage_product_id"))
                quantity = line.get("quantity") or 0.0
                if product_id:
                    values["product_id"] = product_id
                    values.pop("name", None)
                if product_id and quantity:
                    total = sign * line["amount"]
                    quantity = round(quantity, qty_precision)
                    unit = line.get("price_unit") or 0.0
                    rounding = ctx.env["res.company"].browse(
                        company_id
                    ).currency_id.rounding or 0.01
                    if quantity and abs(quantity * unit - total) >= rounding:
                        # Sage's unit price does not multiply out to the line
                        # — a line discount, or a price carrying more decimals
                        # than it stores. Derive one, at the precision the
                        # field will actually keep.
                        unit = round(total / quantity, precision)
                    # Compared the way Odoo will compute it: `price_subtotal`
                    # is the product rounded to the currency, so anything that
                    # merely lands close enough on a float is not close
                    # enough. A cent that survives here is a cent the control
                    # account carries.
                    if quantity and float_compare(
                        float_round(
                            quantity * unit, precision_rounding=rounding
                        ),
                        total,
                        precision_rounding=rounding,
                    ) == 0:
                        values.update(
                            {"quantity": quantity, "price_unit": unit}
                        )
                    # Otherwise the line keeps quantity 1 at its exact amount.
                    # A take-on has to tie to the cent, and no quantity is
                    # worth a control account that is out by a few cents; the
                    # product link, which is what sales analysis needs, is
                    # kept either way.
                lines.append((0, 0, values))
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
                "narration": document.get("description") or False,
                "invoice_date_due": due_date,
                "sage_doc_id": document["sage_doc_id"],
                "sage_gl_entry_id": document["sage_gl_entry_id"],
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
            if abs(abs(move.amount_total) - document["original"]) > 0.02:
                ctx.report.warning(
                    f"Posted {move.amount_total:.2f} against a Sage document "
                    f"of {document['original']:.2f}",
                    source_ref=document["number"],
                )

        _logger.info(
            "Sage open items: %s posted, %s already present. "
            "AR %.2f / AP %.2f.",
            created, skipped, totals["customer"], totals["vendor"],
        )
