"""ETL Pipeline for reconciling credit memos with invoices/bills.

Now that credit memos are imported as actual account.move records (out_refund/in_refund),
we can directly reconcile their receivable/payable lines with the corresponding
invoice/bill receivable/payable lines.

SAP Links:
- RIN1.baseentry -> OINV.docentry (A/R credit memo applied to A/R invoice)
- RPC1.baseentry -> OPCH.docentry (A/P credit memo applied to A/P bill)
"""

import logging

from odoo import models
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.credit.memo.reconciliation",
    sap_source="orin",
    depends_on=[
        "account.move.credit.memo.importer",
        "account.move.vendor.credit.memo.importer",
    ],
    allow_multiprocessing=False,
)
class AccountCreditMemoReconciliation(models.AbstractModel):
    _name = "account.credit.memo.reconciliation"
    _description = "Reconcile Credit Memos with Invoices/Bills"

    @ETL.extract("orin")
    def extract_credit_memo_links(self, ctx: ETLContext):
        """Extract credit memo to invoice/bill links from SAP."""
        _logger.info("[CreditMemoReconciliation] Extracting credit memo links...")

        # A/R Credit Memos (ORIN) linked to A/R Invoices (OINV)
        # basetype=13 means the line is based on an A/R Invoice
        ctx.cr.execute(
            """
            SELECT DISTINCT
                cm.docentry as cm_docentry,
                l.baseentry::integer as invoice_docentry,
                'customer' as cm_type
            FROM orin cm
            JOIN rin1 l ON cm.docentry = l.docentry
            WHERE l.basetype = 13 AND l.baseentry IS NOT NULL
            """
        )
        customer_links = ctx.cr.dictfetchall()

        # A/P Credit Memos (ORPC) linked to A/P Bills (OPCH)
        # basetype=18 means the line is based on an A/P Invoice
        ctx.cr.execute(
            """
            SELECT DISTINCT
                cm.docentry as cm_docentry,
                l.baseentry::integer as invoice_docentry,
                'vendor' as cm_type
            FROM orpc cm
            JOIN rpc1 l ON cm.docentry = l.docentry
            WHERE l.basetype = 18 AND l.baseentry IS NOT NULL
            """
        )
        vendor_links = ctx.cr.dictfetchall()

        _logger.info(
            f"[CreditMemoReconciliation] Found {len(customer_links)} A/R and "
            f"{len(vendor_links)} A/P credit memo links"
        )

        return {"customer": customer_links, "vendor": vendor_links}

    @ETL.transform()
    def transform_credit_memo_links(self, ctx: ETLContext, extracted):
        """Map SAP docentry values to Odoo move IDs."""
        links = extracted.get("extract_credit_memo_links", {})
        customer_links = links.get("customer", [])
        vendor_links = links.get("vendor", [])

        # Get all credit memo docentries
        ar_cm_docentries = list({l["cm_docentry"] for l in customer_links})
        ap_cm_docentries = list({l["cm_docentry"] for l in vendor_links})

        # Get all invoice/bill docentries
        ar_inv_docentries = list({l["invoice_docentry"] for l in customer_links})
        ap_inv_docentries = list({l["invoice_docentry"] for l in vendor_links})

        # Build maps: sap_docentry -> odoo move_id
        ar_cm_map = {}
        if ar_cm_docentries:
            cms = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", ar_cm_docentries),
                    ("sap_table", "=", "orin"),
                    ("state", "=", "posted"),
                ]
            )
            ar_cm_map = {m.sap_docentry: m.id for m in cms}

        ap_cm_map = {}
        if ap_cm_docentries:
            cms = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", ap_cm_docentries),
                    ("sap_table", "=", "orpc"),
                    ("state", "=", "posted"),
                ]
            )
            ap_cm_map = {m.sap_docentry: m.id for m in cms}

        ar_inv_map = {}
        if ar_inv_docentries:
            invs = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", ar_inv_docentries),
                    ("sap_table", "=", "oinv"),
                    ("state", "=", "posted"),
                ]
            )
            ar_inv_map = {m.sap_docentry: m.id for m in invs}

        ap_inv_map = {}
        if ap_inv_docentries:
            bills = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", ap_inv_docentries),
                    ("sap_table", "=", "opch"),
                    ("state", "=", "posted"),
                ]
            )
            ap_inv_map = {m.sap_docentry: m.id for m in bills}

        # Build reconciliation pairs: (credit_memo_id, invoice_id, type)
        reconciliation_pairs = []

        for link in customer_links:
            cm_id = ar_cm_map.get(link["cm_docentry"])
            inv_id = ar_inv_map.get(link["invoice_docentry"])
            if cm_id and inv_id:
                reconciliation_pairs.append(
                    {"cm_id": cm_id, "inv_id": inv_id, "type": "customer"}
                )

        for link in vendor_links:
            cm_id = ap_cm_map.get(link["cm_docentry"])
            inv_id = ap_inv_map.get(link["invoice_docentry"])
            if cm_id and inv_id:
                reconciliation_pairs.append(
                    {"cm_id": cm_id, "inv_id": inv_id, "type": "vendor"}
                )

        _logger.info(
            f"[CreditMemoReconciliation] Prepared {len(reconciliation_pairs)} "
            f"reconciliation pairs"
        )
        return reconciliation_pairs

    @ETL.load()
    def reconcile_credit_memos(self, ctx: ETLContext, transformed):
        """Reconcile credit memo receivable/payable lines with invoice lines."""
        pairs = transformed.get("transform_credit_memo_links", [])

        if not pairs:
            _logger.info("[CreditMemoReconciliation] No pairs to reconcile")
            return

        reconciled = 0
        already_reconciled = 0
        failed = 0

        for pair in pairs:
            cm = ctx.env["account.move"].browse(pair["cm_id"])
            inv = ctx.env["account.move"].browse(pair["inv_id"])

            if pair["type"] == "customer":
                account_type = "asset_receivable"
            else:
                account_type = "liability_payable"

            # Get unreconciled receivable/payable lines from both moves
            cm_line = cm.line_ids.filtered(
                lambda l, at=account_type: l.account_id.account_type == at
                and not l.reconciled
            )
            inv_line = inv.line_ids.filtered(
                lambda l, at=account_type: l.account_id.account_type == at
                and not l.reconciled
            )

            if not cm_line or not inv_line:
                if not cm_line and not inv_line:
                    already_reconciled += 1
                else:
                    _logger.debug(
                        f"Cannot reconcile CM {cm.name} with {inv.name}: "
                        f"cm_line={bool(cm_line)}, inv_line={bool(inv_line)}"
                    )
                    failed += 1
                continue

            try:
                (cm_line + inv_line).reconcile()
                reconciled += 1
            except Exception as e:
                _logger.warning(
                    f"Failed to reconcile CM {cm.name} with {inv.name}: {e}"
                )
                failed += 1

        _logger.info(
            f"[CreditMemoReconciliation] Complete: {reconciled} reconciled, "
            f"{already_reconciled} already reconciled, {failed} failed"
        )
