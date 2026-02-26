"""QuickBooks Online Bill ETL Pipeline

This module handles the migration of Bills (Vendor Bills) from QBO to Odoo
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
    importer_name="qbo.bill.importer",
    sap_source="Bill",
    depends_on=[
        "qbo.vendor.importer",
        "qbo.item.importer",
        "qbo.tax.importer",
        "qbo.purchase.order.importer",
        "qbo.partner.account.linker",
        "qbo.category.account.fixer",
    ],
    chunk_size=100,
    multiprocessing_threshold=200,
)
class QboBillImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Bills."""

    _name = "qbo.bill.importer"
    _description = "QBO Bill Importer"

    @ETL.extract("Bill")
    def extract_bills(self, ctx: ETLContext) -> ChunkableData:
        """Extract bills from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO bill IDs
        existing_ids = extractor.existing_qbo_ids("account_move", "qbo_bill_id")
        _logger.info(f"Found {len(existing_ids)} existing bills in Odoo")

        # Fetch all bills from QBO
        bills = api_client.query_all(entity="Bill", order_by="Id")

        # Filter out already imported
        new_bills = [bill for bill in bills if str(bill.get("Id")) not in existing_ids]

        _logger.info(f"Extracted {len(bills)} bills from QBO, {len(new_bills)} are new")

        # Preload maps for transform
        extractor.preload(
            "account", "vendor", "product", "product_expense",
            "purchase_tax", "purchase_tax_rate", "currency",
        )
        extractor.preload_journals("purchase")

        # Pipeline-specific: PO lookup for linking bills to purchase orders
        extractor.extra["po_map"] = extractor.qbo_id_map(
            "purchase_order", "qbo_purchase_order_id"
        )
        extractor.extra["po_name_map"] = extractor.qbo_name_map(
            "purchase_order", "qbo_purchase_order_id"
        )

        return ChunkableData(
            records=new_bills,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_bills(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO bills into Odoo account.move values."""
        data = extracted.get("extract_bills")
        if not data:
            return []
        bills = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])
        po_map = builder.get_extra("po_map") or {}
        po_name_map = builder.get_extra("po_name_map") or {}

        move_vals = []
        skipped = 0

        for bill in bills:
            vals = builder.build_invoice_move_vals(
                bill,
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
            )
            if not vals:
                skipped += 1
                continue

            # Link to purchase order if found via LinkedTxn
            for linked in bill.get("LinkedTxn", []):
                if linked.get("TxnType") == "PurchaseOrder":
                    txn_id = str(linked.get("TxnId", ""))
                    if txn_id in po_map:
                        vals["invoice_origin"] = po_name_map.get(txn_id, "")
                        break

            move_vals.append(vals)

        _logger.info(f"Transformed {len(move_vals)} bills, skipped {skipped}")
        return move_vals

    @ETL.load()
    def load_bills(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load bills into Odoo."""
        move_vals = transformed.get("transform_bills", [])

        if not move_vals:
            _logger.info("No new bills to create")
            return

        moves = ctx.env["account.move"]
        for vals in move_vals:
            with ctx.skippable(f"create bill QBO#{vals.get('qbo_bill_id', '?')}"):
                moves |= ctx.env["account.move"].create(vals)

        _logger.info(f"Created {len(moves)} bills")

        posted = 0
        by_journal = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post bill QBO#{move.qbo_bill_id or '?'}"):
                        move.action_post()
                        posted += 1

        _logger.info(f"Posted {posted} bills")
