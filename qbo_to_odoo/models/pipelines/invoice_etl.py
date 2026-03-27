"""QuickBooks Online Invoice ETL Pipeline

This module handles the migration of Invoices from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData, post_lock

from .extractor import QBOExtractor
from .move_builder import QBOMoveBuilder
from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.invoice.importer",
    sap_source="Invoice",
    depends_on=[
        "qbo.customer.importer",
        "qbo.item.importer",
        "qbo.tax.importer",
        "qbo.estimate.importer",
        "qbo.partner.account.linker",
        "qbo.category.account.fixer",
    ],
    chunk_size=100,
    multiprocessing_threshold=200,
)
class QboInvoiceImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Invoices."""

    _name = "qbo.invoice.importer"
    _description = "QBO Invoice Importer"

    @ETL.extract("Invoice")
    def extract_invoices(self, ctx: ETLContext) -> ChunkableData:
        """Extract invoices from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO invoice IDs
        existing_ids = extractor.existing_qbo_ids("account_move", "qbo_invoice_id")
        _logger.info(f"Found {len(existing_ids)} existing invoices in Odoo")

        # Fetch all invoices from QBO
        invoices = api_client.query_all(entity="Invoice", order_by="Id")

        # Filter out already imported
        new_invoices = [
            inv for inv in invoices if str(inv.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(invoices)} invoices from QBO, {len(new_invoices)} are new"
        )

        # Preload maps for transform
        extractor.preload(
            "account", "customer", "product", "product_income",
            "sale_tax", "sale_tax_rate", "currency",
        )
        extractor.preload_journals("sale")

        # Resolve shipping account from QBO Preferences
        api_client = get_api_client(ctx)
        prefs = api_client.query("Preferences", max_results=1)
        if prefs:
            ship_qbo_id = (prefs[0] if isinstance(prefs, list) else prefs
                           ).get("SalesFormsPrefs", {}).get("DefaultShippingAccount")
            if ship_qbo_id:
                extractor.extra["shipping_account_id"] = extractor.account_map.get(
                    int(ship_qbo_id)
                )

        # Pipeline-specific: estimate lookup for linking invoices to sale orders
        extractor.extra["estimate_map"] = extractor.qbo_id_map(
            "sale_order", "qbo_estimate_id"
        )
        extractor.extra["estimate_name_map"] = extractor.qbo_name_map(
            "sale_order", "qbo_estimate_id"
        )

        return ChunkableData(
            records=new_invoices,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_invoices(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO invoices into Odoo account.move values."""
        data = extracted.get("extract_invoices")
        if not data:
            return []
        invoices = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])
        estimate_name_map = builder.get_extra("estimate_name_map") or {}
        estimate_map = builder.get_extra("estimate_map") or {}

        move_vals = []
        skipped = 0

        for inv in invoices:
            vals = builder.build_invoice_move_vals(
                inv,
                move_type="out_invoice",
                journal_type="sale",
                partner_type="customer",
                qbo_id_field="qbo_invoice_id",
                line_detail_types=("SalesItemLineDetail", "DiscountLineDetail"),
                tax_use="sale",
                direction="income",
                memo_field="CustomerMemo",
                memo_key="value",
            )
            if not vals:
                skipped += 1
                continue

            # Link to sale order if found via LinkedTxn
            for linked in inv.get("LinkedTxn", []):
                if linked.get("TxnType") == "Estimate":
                    txn_id = str(linked.get("TxnId", ""))
                    if txn_id in estimate_map:
                        vals["invoice_origin"] = estimate_name_map.get(txn_id, "")
                        break

            move_vals.append(vals)

        _logger.info(f"Transformed {len(move_vals)} invoices, skipped {skipped}")
        return move_vals

    @ETL.load()
    def load_invoices(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load invoices into Odoo."""
        move_vals = transformed.get("transform_invoices", [])

        if not move_vals:
            _logger.info("No new invoices to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals:
            with ctx.skippable(f"create invoice QBO#{vals.get('qbo_invoice_id', '?')}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} invoices")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post invoice QBO#{move.qbo_invoice_id or '?'}"):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} invoices")
