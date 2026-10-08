"""Sales orders still open in Sage at cutover -> confirmed Odoo orders.

Only the orders Sage has not cleared. Historical orders are not imported:
Sage keeps no link from an invoice to the order it filled (`tcustr.lOrdId` is
0 throughout), the invoices already carry the product history, and a closed
order in Odoo would have to be forced out of "to invoice".

`tsoline` quantities: `dOrdered` is what was ordered, `dQuantity` what has
been delivered so far, `dRemaining` Sage's difference of the two — which is
meaningless when the order was taken by the piece and delivered by weight
(12 "chaque" ordered, 5.418 kg delivered, "6.582 remaining"). What is left to
deliver is decided by `remaining_quantity` instead.
"""

import logging

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)

#: Sage unit names that mean weight. Free text in Sage; compared lower-case.
WEIGHT_UNITS = {"kg", "kilo", "kilos", "kilogramme", "kg cuit", "lb", "lbs"}

#: Sage unit names meaning "one of it", in either language.
COUNT_SYNONYMS = {"chaque", "unité", "unite", "unités", "unites", "unit",
                  "units", "each", "ea", "pc", "pce", "pcs"}

#: Sage unit names meaning kilograms.
KG_SYNONYMS = {"kg", "kilo", "kilos", "kilogramme", "kilogrammes"}

#: Below this a quantity is float noise.
EPSILON = 0.0005


def remaining_quantity(line: dict, weight_units=WEIGHT_UNITS) -> float:
    """What is still to deliver on one Sage order line.

    - nothing delivered yet: the ordered quantity;
    - sold by weight, something delivered: done — the order was for an
      estimated weight, the delivery the actual one, and it is invoiced on
      what was delivered;
    - sold by count, part delivered: the rest — a real partial;
    - sold by count but delivered in fractions: it was weighed, not counted,
      so done;
    - delivered in full or over: done.
    """
    ordered = line.get("dOrdered") or 0.0
    delivered = line.get("dQuantity") or 0.0
    if delivered <= EPSILON:
        return max(ordered, 0.0)
    if (line.get("sUnits") or "").strip().lower() in weight_units:
        return 0.0
    if abs(delivered - round(delivered)) > EPSILON:
        return 0.0
    return max(round(ordered - delivered, 6), 0.0)


def same_unit(sage_unit: str, uom) -> bool:
    """Whether Sage's free-text unit is the product's Odoo unit.

    Only a real difference — a caisse on a product stocked in kg — needs a
    conversion factor the Sage file does not have; "chaque" on Units or
    "KILO" on kg is the same unit named differently.
    """
    name = (sage_unit or "").strip().lower()
    if not name or name == (uom.name or "").strip().lower():
        return True
    env = uom.env
    if name in COUNT_SYNONYMS:
        return uom == env.ref("uom.product_uom_unit", raise_if_not_found=False)
    if name in KG_SYNONYMS:
        return uom == env.ref("uom.product_uom_kgm", raise_if_not_found=False)
    return False


@ETL.pipeline(
    target_model="sale.order",
    importer_name="sage.sale.order.importer",
    sap_source="tsalordr",
    depends_on=["sage.partner.importer", "sage.product.importer"],
    allow_multiprocessing=False,
)
class SageSaleOrderImporter(models.AbstractModel):
    _name = "sage.sale.order.importer"
    _description = "Sage 50 Open Sales Order Importer"

    def _weight_units(self) -> set:
        """Hook: the Sage unit names this file uses for weight."""
        return WEIGHT_UNITS

    @ETL.extract("tsalordr")
    def extract_orders(self, ctx: ETLContext) -> list:
        weight_units = self._weight_units()
        orders = []
        for header in tools.query(
            ctx.cr,
            """select lId, lCusId, sSONum, dtSODate, dtShipDate, sComment
                 from tsalordr
                where bCleared = 0 and bQuote = 0
                order by dtSODate, lId""",
        ):
            lines = []
            for row in tools.query(
                ctx.cr,
                """select nLineNum, lInventId, sDesc, dOrdered, dQuantity,
                          dPrice, sUnits
                     from tsoline where lSOId = %s order by nLineNum""",
                (header["lId"],),
            ):
                if not row["lInventId"]:
                    continue
                quantity = remaining_quantity(row, weight_units)
                if quantity <= EPSILON:
                    continue
                lines.append({
                    "sage_product_id": row["lInventId"],
                    "description": (row["sDesc"] or "").strip(),
                    "quantity": quantity,
                    "price_unit": row["dPrice"] or 0.0,
                    "units": (row["sUnits"] or "").strip(),
                })
            orders.append({
                "sage_order_id": header["lId"],
                "number": (header["sSONum"] or "").strip(),
                "sage_customer_id": header["lCusId"],
                "date": header["dtSODate"].strftime("%Y-%m-%d")
                if header["dtSODate"] else False,
                "ship_date": header["dtShipDate"].strftime("%Y-%m-%d")
                if header["dtShipDate"] else False,
                "note": (header["sComment"] or "").strip(),
                "lines": lines,
            })
        return orders

    @ETL.transform()
    def transform_orders(self, ctx: ETLContext, extracted: dict) -> list:
        return extracted["extract_orders"]

    @ETL.load()
    def load_orders(self, ctx: ETLContext, transformed: dict) -> None:
        self._load_orders(ctx, transformed["transform_orders"])

    def _load_orders(self, ctx: ETLContext, orders: list) -> None:
        env = ctx.env
        company_id = ctx.get_config("company_id")
        partners = {
            partner.sage_customer_id: partner.id
            for partner in env["res.partner"].search(
                [("sage_customer_id", "!=", 0)]
            )
        }
        products = {
            product.sage_product_id: product.product_variant_id
            for product in env["product.template"].with_context(
                active_test=False
            ).search([("sage_product_id", "!=", 0)])
            if product.product_variant_id
        }
        already = set(env["sale.order"].search([
            ("sage_order_id", "!=", 0), ("company_id", "=", company_id),
        ]).mapped("sage_order_id"))

        created = skipped = empty = 0
        for order in orders:
            if order["sage_order_id"] in already:
                skipped += 1
                continue
            if not order["lines"]:
                empty += 1
                continue
            partner_id = partners.get(order["sage_customer_id"])
            if not partner_id:
                ctx.report.failure(
                    f"Order {order['number']}: no partner for Sage customer "
                    f"{order['sage_customer_id']}",
                    source_ref=order["number"],
                )
                continue
            lines, missing = [], None
            for line in order["lines"]:
                product = products.get(line["sage_product_id"])
                if not product:
                    missing = line["sage_product_id"]
                    break
                name = line["description"] or product.display_name
                if not same_unit(line["units"], product.uom_id):
                    # Sold by the caisse, stocked in kg: Sage has no
                    # conversion factor for it, so the quantity stays in
                    # Sage's unit and says so.
                    name = f"{name} ({line['quantity']:g} {line['units']})"
                    ctx.report.warning(
                        f"Order {order['number']}: {product.display_name} "
                        f"sold in '{line['units']}', product unit is "
                        f"'{product.uom_id.name}'",
                        source_ref=order["number"],
                    )
                lines.append((0, 0, {
                    "product_id": product.id,
                    "name": name,
                    "product_uom_qty": line["quantity"],
                    "price_unit": line["price_unit"],
                }))
            if missing is not None:
                ctx.report.failure(
                    f"Order {order['number']}: no product for Sage item "
                    f"{missing}",
                    source_ref=order["number"],
                )
                continue
            with ctx.skippable(source_ref=order["number"]):
                sale = env["sale.order"].create({
                    "partner_id": partner_id,
                    "company_id": company_id,
                    "client_order_ref": order["number"] or False,
                    "date_order": order["date"] or False,
                    "commitment_date": order["ship_date"] or False,
                    "note": order.get("note") or False,
                    "sage_order_id": order["sage_order_id"],
                    "order_line": lines,
                })
                # Confirmed so Odoo creates the deliveries still to make. No
                # `send_email` in the context: the customer is not mailed.
                sale.action_confirm()
                # Confirming stamps the order date with "now"; the order was
                # taken on Sage's date.
                if order["date"]:
                    sale.date_order = order["date"]
                created += 1
                ctx.report.success()
        _logger.info(
            "Sage open orders: %s confirmed, %s already present, %s with "
            "nothing left to deliver.", created, skipped, empty,
        )
