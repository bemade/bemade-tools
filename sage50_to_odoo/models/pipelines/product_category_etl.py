"""Sage 50 item accounts -> `product.category`.

Sage 50 *has* a category table, and sites routinely leave it empty. What they
use instead is a **revenue account per item**, and by the time a file is a few
years old that account has usually become a stand-in for the dimensions Sage
does not have — channel, product family, brand — spread across dozens of GL
accounts.

Because the account hangs off the item and not the customer, whatever it
encodes is a property of the product, which is exactly what an Odoo product
category is for. So the categories are derived from the revenue account, and
`_category_path` is the hook where a client says how to read its own account
names. The default is one category per revenue account, named after it, which
is correct but flat.

Expense and stock accounts are set per item in Sage, usually in dozens of
one-off combinations. The category takes the commonest combination for its
revenue account; only the genuinely different items carry their own. That is
the Odoo convention and it keeps the category tree readable.
"""

import collections
import logging

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="product.category",
    importer_name="sage.product.category.importer",
    sap_source="tinvent",
    depends_on=["sage.account.importer"],
    allow_multiprocessing=False,
)
class SageProductCategoryImporter(models.AbstractModel):
    _name = "sage.product.category.importer"
    _description = "Sage 50 Product Category Importer"

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------
    def _category_path(self, revenue_account_name: str) -> list:
        """The Odoo category path a Sage revenue account maps to.

        Returns a list of names, outermost first, so a client layer can split
        an account name like "Sales — private label — sausages" into as many
        levels as it actually encodes. The default keeps it flat.
        """
        return [(revenue_account_name or "").strip() or "Uncategorised"]

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    @ETL.extract("tinvent")
    def extract_item_accounts(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr,
            "select lId, lAcNRev, lAcNExp, lAcNAsset from tinvent order by lId",
        )

    @ETL.extract("taccount")
    def extract_account_names(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr, "select lId, sName, sNameAlt from taccount"
        )

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------
    @ETL.transform()
    def transform_categories(self, ctx: ETLContext, extracted: dict) -> list:
        source = ctx.env[ctx.get_config("source_model")].browse(
            ctx.get_config("source_id")
        )
        names = {
            row["lId"]: source.sage_name(row, "sName", "sNameAlt")
            for row in extracted["extract_account_names"]
        }
        combinations = collections.defaultdict(collections.Counter)
        for item in extracted["extract_item_accounts"]:
            combinations[item["lAcNRev"]][
                (item["lAcNExp"], item["lAcNAsset"])
            ] += 1

        categories = {}
        for revenue_id, counter in combinations.items():
            path = tuple(self._category_path(names.get(revenue_id, "")))
            expense_id, stock_id = counter.most_common(1)[0][0]
            # Two revenue accounts can legitimately map to one path when a
            # client layer normalises spellings. First one wins on the
            # accounts; the product count is the sum.
            entry = categories.setdefault(path, {
                "path": list(path),
                "sage_income_account": revenue_id or 0,
                "sage_expense_account": expense_id or 0,
                "sage_stock_account": stock_id or 0,
                "product_count": 0,
            })
            entry["product_count"] += sum(counter.values())
        return sorted(categories.values(), key=lambda c: c["path"])

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @ETL.load()
    def load_categories(self, ctx: ETLContext, transformed: dict) -> None:
        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        created = updated = 0
        for record in transformed["transform_categories"]:
            category, was_created = self._resolve_path(ctx, record["path"])
            income = accounts.get(record["sage_income_account"])
            expense = accounts.get(record["sage_expense_account"])
            category.write({
                "sage_income_account": record["sage_income_account"],
                "property_account_income_categ_id": income or False,
                "property_account_expense_categ_id": expense or False,
            })
            created += bool(was_created)
            updated += not was_created
            ctx.report.success()
        _logger.info(
            "Sage product categories: %s created, %s updated.",
            created, updated,
        )

    def _resolve_path(self, ctx: ETLContext, path: list) -> tuple:
        """Find or create the category at `path`, creating parents as needed."""
        Category = ctx.env["product.category"]
        parent, created = Category, False
        for name in path:
            category = Category.search([
                ("name", "=", name),
                ("parent_id", "=", parent.id if parent else False),
            ], limit=1)
            if not category:
                category = Category.create({
                    "name": name,
                    "parent_id": parent.id if parent else False,
                })
                created = True
            parent = category
        return parent, created
