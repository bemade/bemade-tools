"""Sage 50 `tinvent` -> `product.template`.

Two things about Sage's item data are worth knowing before reading this.

**Units of measure are labels, not conversions.** Sage stores selling,
stocking and buying units as free text with a conversion factor, and in
practice the factor is 1.0 even where the units differ ("each" sold, "kg"
stocked, factor 1.0). Building a UoM tree out of them would invent
relationships that do not exist, so everything maps onto the reference unit
its label suggests, the original Sage string is kept on the product in
`sage_unit`, and the few items with a real factor are reported for review.

**There may be no cost at all.** `dBldCost` is routinely 0 on every item in a
file, because Sage's inventory costing was never switched on. Importing that
would ship a silent zero into Odoo's valuation; it is logged instead.
"""

import collections
import logging
import re

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)

#: Sage unit strings mapped onto Odoo's reference units. Matched on the
#: lower-cased Sage string; anything unrecognised falls back to units.
UOM_MAP = {
    "kg": "uom.product_uom_kgm",
    "kilo": "uom.product_uom_kgm",
    "kgs": "uom.product_uom_kgm",
    "g": "uom.product_uom_gram",
    "lb": "uom.product_uom_lb",
    "lbs": "uom.product_uom_lb",
    "l": "uom.product_uom_litre",
    "litre": "uom.product_uom_litre",
    "liter": "uom.product_uom_litre",
}
DEFAULT_UOM = "uom.product_uom_unit"

#: Sage unit strings that describe packaging rather than a unit ("caisse 12",
#: "sac 20kg"). They belong on a packaging record, not on the UoM, and are
#: reported as such rather than guessed at.
PACKAGING_LIKE = re.compile(r"\d", re.UNICODE)

#: Sage's own id for the base sales price. It becomes `list_price`, not a
#: pricelist rule.
SAGE_PRICELIST_REGULAR = 1

#: Reference units at the root of a MEASURED quantity. Odoo 19 has no UoM
#: categories — units form a tree through `relative_uom_id`, so kg resolves up
#: to g and litre up to ml. A product measured on a scale cannot be produced
#: to an exact figure; one that is counted can.
MEASURED_UOM_ROOTS = (
    "uom.product_uom_gram",
    "uom.product_uom_milliliter",
)


@ETL.pipeline(
    target_model="product.template",
    importer_name="sage.product.importer",
    sap_source="tinvent",
    depends_on=["sage.product.category.importer"],
    allow_multiprocessing=False,
)
class SageProductImporter(models.AbstractModel):
    _name = "sage.product.importer"
    _description = "Sage 50 Product Importer"

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------
    def _uom_xmlid(self, sage_unit: str) -> str:
        return UOM_MAP.get((sage_unit or "").strip().lower(), DEFAULT_UOM)

    def _invoice_policy(self, ctx: ETLContext, record: dict, uom):
        """Ordered or delivered quantities, decided by how the product is
        measured.

        Weighed and poured products are billed on what was **ordered**:
        production does not hit an ordered weight exactly, an order for 5 kg
        comes out at 4.989, and the customer pays for the 5 kg they asked
        for. Left on "delivered", Odoo wants to invoice the difference and
        the order never finishes.

        Counted products keep **delivered**, where 21 ordered against 23
        shipped is not a tolerance but two extra units, and worth seeing.

        Services invoice on **ordered** whatever their unit: nothing delivers
        them, so a delivered quantity would stay at zero forever.

        Set explicitly at import because Odoo's own default does not make
        this distinction: `invoice_policy` is a stored compute that forces
        every `consu` product to "order" whatever its unit. Leaving it to the
        default means somebody changes them all by hand afterwards.

        Note it does not STAY set by itself — the same compute reasserts
        "order" on any write touching `type`. Holding this rule for the life
        of the database takes an override of `_compute_invoice_policy`, which
        is a behavioural change to a core model and belongs in a client
        module rather than here.
        """
        if record.get("type") == "service":
            # Nothing delivers a service, so `qty_delivered` never moves off
            # zero and "delivered quantities" would leave the line permanently
            # un-invoiceable. Not a preference — the other setting is broken.
            return "order"
        if not uom:
            return False
        roots = set()
        for xmlid in MEASURED_UOM_ROOTS:
            root = ctx.env.ref(xmlid, raise_if_not_found=False)
            if root:
                roots.add(root.id)
        path = uom.parent_path or ""
        root_id = int(path.split("/")[0]) if path else uom.id
        return "order" if root_id in roots else "delivery"

    def _sale_tax_xmlid_suffix(self, item: dict):
        """The sales tax an item carries, as an `account.tax` xmlid suffix.

        None means "take the company default", which is the right answer for
        most items in most files: Sage records the tax on the *transaction*,
        not on the item, so the item can only ever carry a sensible default.

        Overriding this is worth it where a file has a clean rule — food
        being zero-rated while service revenue is taxable, say. Where it does
        not, leave it alone rather than inventing one.
        """
        return None

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    @ETL.extract("tinvent")
    def extract_items(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr,
            """select lId, sPartCode, sName, sNameF, bService, bInactive,
                      sSellUnit, sStockUnit, sBuyUnit, dSellRel, dBuyRel,
                      dBldCost, lAcNRev, lAcNExp, lAcNAsset
                 from tinvent order by lId""",
        )

    @ETL.extract("tinvprc")
    def extract_prices(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr, "select lInventId, lPrcListId, dPrice from tinvprc"
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
    def transform_products(self, ctx: ETLContext, extracted: dict) -> list:
        source = ctx.env[ctx.get_config("source_model")].browse(
            ctx.get_config("source_id")
        )
        Categories = ctx.env["sage.product.category.importer"]
        names = {
            row["lId"]: source.sage_name(row, "sName", "sNameAlt")
            for row in extracted["extract_account_names"]
        }
        prices = collections.defaultdict(dict)
        for row in extracted["extract_prices"]:
            prices[row["lInventId"]][row["lPrcListId"]] = row["dPrice"]

        items = extracted["extract_items"]
        # The same commonest-combination rule the category pipeline used, so
        # "differs from its category" means the same thing in both places.
        combinations = collections.defaultdict(collections.Counter)
        for item in items:
            combinations[item["lAcNRev"]][
                (item["lAcNExp"], item["lAcNAsset"])
            ] += 1

        values, odd_uom, packaging_like, costed = [], [], set(), 0
        for item in items:
            default_expense, default_stock = combinations[
                item["lAcNRev"]
            ].most_common(1)[0][0]
            stock_unit = (item["sStockUnit"] or "").strip()
            sell_unit = (item["sSellUnit"] or "").strip()
            if item["dSellRel"] not in (0.0, 1.0) or \
                    item["dBuyRel"] not in (0.0, 1.0):
                odd_uom.append(item)
            for unit in (stock_unit, sell_unit):
                if unit and PACKAGING_LIKE.search(unit):
                    packaging_like.add(unit)
            costed += bool(item["dBldCost"])

            values.append({
                "sage_product_id": item["lId"],
                "default_code": (item["sPartCode"] or "").strip() or None,
                "name": source.sage_name(item, "sName", "sNameF"),
                "type": "service" if item["bService"] else "consu",
                "active": not item["bInactive"],
                "category_path": Categories._category_path(
                    names.get(item["lAcNRev"], "")
                ),
                "uom_xmlid": self._uom_xmlid(stock_unit),
                "sage_unit": stock_unit or None,
                "list_price": prices[item["lId"]].get(
                    SAGE_PRICELIST_REGULAR, 0.0
                ),
                "standard_price": item["dBldCost"] or 0.0,
                "sale_tax": self._sale_tax_xmlid_suffix(item),
                # Only where the item differs from its category default.
                "sage_expense_account": (
                    item["lAcNExp"]
                    if item["lAcNExp"] != default_expense else 0
                ),
            })

        if not costed:
            _logger.warning(
                "No Sage item carries a standard cost (dBldCost is 0 on all "
                "%s). Odoo costing starts from scratch; this is not an import "
                "failure.", len(items),
            )
        if odd_uom:
            _logger.warning(
                "%s items have a real sell/buy conversion factor, which this "
                "import does not carry: %s",
                len(odd_uom),
                ", ".join(
                    f"{i['sPartCode']} {i['sSellUnit']}->{i['sStockUnit']} "
                    f"x{i['dSellRel']}" for i in odd_uom[:10]
                ),
            )
        if packaging_like:
            _logger.warning(
                "Sage units that describe packaging rather than a unit, "
                "imported as plain units: %s", sorted(packaging_like),
            )
        return values

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @ETL.load()
    def load_products(self, ctx: ETLContext, transformed: dict) -> None:
        Product = ctx.env["product.template"]
        Category = ctx.env["product.category"]
        company = ctx.env["res.company"].browse(ctx.get_config("company_id"))
        accounts = ctx.env["sage.account.importer"].sage_account_map(ctx)
        existing = {
            product.sage_product_id: product
            for product in Product.with_context(active_test=False).search(
                [("sage_product_id", "!=", 0)]
            )
        }
        categories, created, updated, taxed, skipped = {}, 0, 0, 0, []
        for record in transformed["transform_products"]:
            path = tuple(record["category_path"])
            if path not in categories:
                categories[path] = self._find_category(Category, path)
            category = categories[path]
            if not category:
                skipped.append(record["default_code"] or record["name"])
                ctx.report.failure(
                    f"No product category at {' / '.join(path)}",
                    source_ref=record["default_code"],
                )
                continue

            uom = ctx.env.ref(record["uom_xmlid"], raise_if_not_found=False)
            values = {
                "sage_product_id": record["sage_product_id"],
                "sage_unit": record["sage_unit"],
                "name": record["name"],
                "default_code": record["default_code"],
                "type": record["type"],
                "active": record["active"],
                "categ_id": category.id,
                "list_price": record["list_price"],
                "standard_price": record["standard_price"],
            }
            if uom:
                values["uom_id"] = uom.id
            policy = self._invoice_policy(ctx, record, uom)
            if policy:
                values["invoice_policy"] = policy
            if record["sale_tax"]:
                tax = ctx.env.ref(
                    f"account.{company.id}_{record['sale_tax']}",
                    raise_if_not_found=False,
                )
                if tax:
                    values["taxes_id"] = [(6, 0, tax.ids)]
                    taxed += 1
            else:
                # Everything else takes the company default explicitly, so a
                # re-run cannot leave a stale tax behind.
                values["taxes_id"] = [(6, 0, company.account_sale_tax_id.ids)]
            expense = accounts.get(record["sage_expense_account"])
            if expense:
                values["property_account_expense_id"] = expense

            product = existing.get(record["sage_product_id"])
            if product:
                product.write(values)
                updated += 1
            else:
                Product.create(values)
                created += 1
            ctx.report.success()

        _logger.info(
            "Sage products: %s created, %s updated, %s carrying an explicit "
            "sales tax.", created, updated, taxed,
        )
        if skipped:
            _logger.error(
                "%s products skipped for a missing category: %s",
                len(skipped), ", ".join(skipped),
            )

    def _find_category(self, Category, path: tuple):
        parent = Category
        for name in path:
            parent = Category.search([
                ("name", "=", name),
                ("parent_id", "=", parent.id if parent else False),
            ], limit=1)
            if not parent:
                return Category
        return parent
