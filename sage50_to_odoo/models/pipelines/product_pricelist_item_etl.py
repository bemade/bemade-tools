"""Sage 50 `tinvprc` -> `product.pricelist.item`.

Sage stores one price per (item, pricelist) with no rules, discounts or
date ranges, so every row becomes a fixed price on a single product. Rows on
the base pricelist are skipped: that price is the product's own `list_price`,
already set by the product pipeline. So is any row equal to it, because a
rule that restates the sales price is noise a user then has to maintain.
"""

import logging

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools
from .product_template_etl import SAGE_PRICELIST_REGULAR

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="product.pricelist.item",
    importer_name="sage.pricelist.item.importer",
    sap_source="tinvprc",
    depends_on=[
        "sage.product.importer",
        "sage.pricelist.importer",
    ],
    allow_multiprocessing=False,
)
class SagePricelistItemImporter(models.AbstractModel):
    _name = "sage.pricelist.item.importer"
    _description = "Sage 50 Pricelist Rule Importer"

    @ETL.extract("tinvprc")
    def extract_prices(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr,
            """select lInventId, lPrcListId, dPrice
                 from tinvprc order by lInventId, lPrcListId""",
        )

    @ETL.transform()
    def transform_items(self, ctx: ETLContext, extracted: dict) -> list:
        base_prices = {}
        for row in extracted["extract_prices"]:
            if row["lPrcListId"] == SAGE_PRICELIST_REGULAR:
                base_prices[row["lInventId"]] = row["dPrice"]
        return [
            {
                "sage_pricelist_id": row["lPrcListId"],
                "sage_product_id": row["lInventId"],
                "fixed_price": row["dPrice"],
            }
            for row in extracted["extract_prices"]
            if row["lPrcListId"] != SAGE_PRICELIST_REGULAR
            and row["dPrice"] != base_prices.get(row["lInventId"])
        ]

    @ETL.load()
    def load_items(self, ctx: ETLContext, transformed: dict) -> None:
        Item = ctx.env["product.pricelist.item"]
        pricelists = ctx.env["sage.pricelist.importer"].sage_pricelist_map(ctx)
        products = {
            product.sage_product_id: product.id
            for product in ctx.env["product.template"].with_context(
                active_test=False
            ).search([("sage_product_id", "!=", 0)])
        }
        written = skipped = 0
        for record in transformed["transform_items"]:
            pricelist_id = pricelists.get(record["sage_pricelist_id"])
            product_id = products.get(record["sage_product_id"])
            if not pricelist_id or not product_id:
                skipped += 1
                ctx.report.warning(
                    "No Odoo pricelist or product for Sage pricelist "
                    f"{record['sage_pricelist_id']} / item "
                    f"{record['sage_product_id']}"
                )
                continue
            values = {
                "pricelist_id": pricelist_id,
                "applied_on": "1_product",
                "product_tmpl_id": product_id,
                "compute_price": "fixed",
                "fixed_price": record["fixed_price"],
            }
            item = Item.search([
                ("pricelist_id", "=", pricelist_id),
                ("product_tmpl_id", "=", product_id),
            ], limit=1)
            if item:
                item.write(values)
            else:
                Item.create(values)
            written += 1
            ctx.report.success()
        _logger.info(
            "Sage pricelist rules: %s written, %s skipped.", written, skipped
        )
