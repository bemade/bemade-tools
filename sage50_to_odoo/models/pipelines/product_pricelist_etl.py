"""Sage 50 `tprclist` -> `product.pricelist`.

Sage names its pricelists in both languages (`sDesc` / `sDescF`), so a
configurator that has already created them under the French names is matched
rather than duplicated. That is the normal case: the pricelists usually exist
before the take-on runs, because customers have to be pinned to them.
"""

import logging

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="product.pricelist",
    importer_name="sage.pricelist.importer",
    sap_source="tprclist",
    allow_multiprocessing=False,
)
class SagePricelistImporter(models.AbstractModel):
    _name = "sage.pricelist.importer"
    _description = "Sage 50 Pricelist Importer"

    @ETL.extract("tprclist")
    def extract_pricelists(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr,
            "select lId, sDesc, sDescF, bActive from tprclist order by lId",
        )

    @ETL.transform()
    def transform_pricelists(self, ctx: ETLContext, extracted: dict) -> list:
        source = ctx.env[ctx.get_config("source_model")].browse(
            ctx.get_config("source_id")
        )
        return [
            {
                "name": source.sage_name(row, "sDesc", "sDescF"),
                "active": bool(row["bActive"]),
            }
            for row in extracted["extract_pricelists"]
        ]

    @ETL.load()
    def load_pricelists(self, ctx: ETLContext, transformed: dict) -> None:
        Pricelist = ctx.env["product.pricelist"]
        company_id = ctx.get_config("company_id")
        created = matched = 0
        for values in transformed["transform_pricelists"]:
            pricelist = Pricelist.with_context(active_test=False).search([
                ("name", "=", values["name"]),
                ("company_id", "in", (company_id, False)),
            ], limit=1)
            if pricelist:
                matched += 1
            else:
                Pricelist.create({
                    "name": values["name"],
                    "active": values["active"],
                    "company_id": company_id,
                    "currency_id": ctx.env["res.company"].browse(
                        company_id
                    ).currency_id.id,
                })
                created += 1
            ctx.report.success()
        _logger.info(
            "Sage pricelists: %s created, %s already present.",
            created, matched,
        )

    # ------------------------------------------------------------------
    # Shared lookup
    # ------------------------------------------------------------------
    def sage_pricelist_map(self, ctx: ETLContext) -> dict:
        """Sage pricelist id -> Odoo pricelist id, resolved by name.

        Used by the partner and pricelist-item pipelines. Kept here so the
        name-matching rule lives in one place, and so a client layer that
        overrides the naming only has to override it once.
        """
        source = ctx.env[ctx.get_config("source_model")].browse(
            ctx.get_config("source_id")
        )
        company_id = ctx.get_config("company_id")
        rows = tools.query(
            ctx.cr, "select lId, sDesc, sDescF from tprclist"
        )
        mapping = {}
        for row in rows:
            pricelist = ctx.env["product.pricelist"].with_context(
                active_test=False
            ).search([
                ("name", "=", source.sage_name(row, "sDesc", "sDescF")),
                ("company_id", "in", (company_id, False)),
            ], limit=1)
            if pricelist:
                mapping[row["lId"]] = pricelist.id
        return mapping
