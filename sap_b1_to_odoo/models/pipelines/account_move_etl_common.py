from odoo import models, api, Command, _
from odoo.exceptions import UserError
from odoo.tools.sql import SQL
import logging
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

_logger = logging.getLogger(__name__)


class AccountMoveCommon(models.AbstractModel):
    _name = "sap.account.move.importer.mixin"
    _description = "Common functionality for SAP invoice and bill importers"

    @api.model
    def _get_row_vals(self, row, sap_table, order_lines_dict, lookups):
        """Transform a SAP line into Odoo account.move.line values.

        Args:
            row: SAP line dict
            sap_table: SAP line table name (e.g., 'inv1', 'pch1')
            order_lines_dict: Dict mapping (docentry, linenum) to order line IDs
            lookups: Pre-computed lookup dicts with keys:
                - products: {sap_item_code: product_id}
                - accounts: {sap_acct_code: (account_id, account_type)}
                - taxes: {(sap_tax_code, type_tax_use): tax_id}
                - unit_uom_id: ID of uom.product_uom_unit
        """
        # Handle text lines from INV10/PCH10
        if row.get("line_type") == "text":
            vals = {
                "display_type": "line_note",
                "name": row["linetext"] or " ",
                "quantity": 0.0,
                "price_unit": 0.0,
                "sap_line_num": 0,
                "sap_aftlinenum": (row["aftlinenum"] or 0) + 2,
                "sap_lineseq": (row["lineseq"] or 0) + 2,
                "sap_table": sap_table.replace("1", "10"),
                "sequence": (
                    row["aftlinenum"] * 100 + row["lineseq"]
                    if row["aftlinenum"] and row["lineseq"]
                    else 0
                ),
            }
            return vals

        # Handle expense lines from INV3/PCH3/RIN3/RPC3
        if row.get("line_type") == "expense":
            accounts_dict = lookups.get("accounts", {})
            acct_info = accounts_dict.get(row.get("acct_formatcode"))
            if not acct_info:
                return None
            account_id = acct_info[0]
            vals = {
                "name": row.get("expnsname") or "Document expense",
                "quantity": 1,
                "price_unit": row.get("linetotal", 0),
                "account_id": account_id,
                "sap_acct_id": account_id,
                "display_type": "product",
                "sap_table": sap_table.replace("1", "3"),
            }
            # Map SAP tax code (vatgroup) to Odoo tax
            tax_id = self._lookup_tax(row, sap_table, lookups.get("taxes", {}))
            if tax_id:
                vals["tax_ids"] = [Command.set([tax_id])]
            return vals

        # Handle product lines
        products_dict = lookups["products"]
        product_id = products_dict.get(row["itemcode"])

        # Always derive effective price from SAP's linetotal (the authoritative final amount)
        # rather than trusting price + discprcnt which can have bogus values
        quantity = row["quantity"] if row["quantity"] else 0.0
        linetotal = row.get("linetotal") or 0.0

        if quantity and quantity != 0:
            price_unit = linetotal / quantity
        else:
            # Service/expense line: set quantity to 1 and use linetotal as price
            quantity = 1.0
            price_unit = linetotal

        vals = {
            "product_id": product_id,
            "quantity": quantity,
            "price_unit": price_unit,
            "sap_line_num": (row["linenum"] or 0) + 2,  # Increment by 2 to avoid 0
            "sap_aftlinenum": 0,  # Product lines don't have aftlinenum, use 0 as null
            "sap_lineseq": 0,  # Product lines don't have lineseq, use 0 as null
            "sap_table": sap_table,  # Use INV1/PCH1 for product lines
            "sequence": row["linenum"] * 100 if row["linenum"] else 0,
        }
        if not vals["product_id"]:
            vals["name"] = row["dscription"] or ""
            vals["product_uom_id"] = lookups["unit_uom_id"]

        # Map SAP account code to Odoo account
        # Use acct_formatcode (human-readable) instead of acctcode (_SYS codes)
        acct_formatcode = row.get("acct_formatcode")
        if acct_formatcode:
            accounts_dict = lookups.get("accounts", {})
            account_info = accounts_dict.get(acct_formatcode)
            if account_info:
                account_id, account_type = account_info
                # Always store SAP's GL account for post-create SQL correction
                vals["sap_acct_id"] = account_id
                # Skip receivable/payable on account_id — Odoo rejects
                # these on product lines at create() time
                if account_type not in [
                    "asset_receivable",
                    "liability_payable",
                ]:
                    vals["account_id"] = account_id
                else:
                    _logger.debug(
                        f"Skipping {account_type} account {acct_formatcode} on {sap_table} line"
                    )
            else:
                _logger.warning(
                    f"Could not find account for SAP code {acct_formatcode} on {sap_table} line"
                )

        # Map SAP tax code (vatgroup) to Odoo tax
        tax_id = self._lookup_tax(row, sap_table, lookups.get("taxes", {}))
        if tax_id:
            vals["tax_ids"] = [Command.set([tax_id])]

        # Link to order line if available
        order_line_id = order_lines_dict.get((row["docentry"], row["linenum"]))
        if order_line_id:
            vals.update(self._get_order_line_link_vals(order_line_id))

        return vals

    @api.model
    def _get_cogs_line_vals(self, row, lookups):
        """Generate COGS journal entry lines from SAP's stockvalue.

        For customer invoices, SAP stores the historical COGS value in stockvalue.
        We create two lines:
        1. Credit to Stock Valuation account (inventory reduction)
        2. Debit to COGS account (expense recognition)

        Args:
            row: SAP line dict with stockvalue, stockprice, cogs_formatcode
            lookups: Pre-computed lookup dicts

        Returns:
            List of line value dicts (empty if no COGS needed)
        """
        # Skip text lines or lines without stock value
        if "linetext" in row or not row.get("stockvalue"):
            return []

        stockvalue = row.get("stockvalue") or 0.0
        if stockvalue == 0:
            return []

        # Get the unit cost from SAP (for price_unit field)
        stockprice = row.get("stockprice") or 0.0
        quantity = row.get("quantity") or 0.0

        # Look up COGS account from SAP's cogs_formatcode
        cogs_formatcode = row.get("cogs_formatcode")
        accounts_dict = lookups.get("accounts", {})

        cogs_account_info = (
            accounts_dict.get(cogs_formatcode) if cogs_formatcode else None
        )
        if not cogs_account_info:
            _logger.warning(
                f"COGS account not found for SAP code {cogs_formatcode}, "
                f"docentry={row.get('docentry')}, linenum={row.get('linenum')}"
            )
            return []

        cogs_account_id = cogs_account_info[0]

        # Get stock valuation account from SAP item group configuration.
        # Resolved during extraction: itemcode → OITM.itmsgrpcod → OITB.balinvntac
        stock_acct_formatcode = row.get("stock_acct_formatcode")
        stock_account_info = (
            accounts_dict.get(stock_acct_formatcode) if stock_acct_formatcode else None
        )
        stock_account_id = stock_account_info[0] if stock_account_info else None

        if not stock_account_id:
            _logger.warning(
                f"Stock valuation account not found for itemcode={row.get('itemcode')}, "
                f"stock_acct_formatcode={stock_acct_formatcode}, skipping COGS for "
                f"docentry={row.get('docentry')}, linenum={row.get('linenum')}"
            )
            return []

        # Get product_id for the COGS lines
        products_dict = lookups.get("products", {})
        product_id = products_dict.get(row.get("itemcode"))

        # Create two COGS lines with display_type='cogs' to match Odoo's format
        # Line 1: Credit Stock Valuation (reduce inventory)
        # Line 2: Debit COGS (recognize expense)
        cogs_lines = [
            {
                "name": row.get("dscription", "")[:64] if row.get("dscription") else "",
                "product_id": product_id,
                "quantity": quantity,
                "price_unit": stockprice,  # Unit cost, not total
                "debit": 0.0,
                "credit": stockvalue,
                "account_id": stock_account_id,
                "display_type": "cogs",
                "tax_ids": [],
            },
            {
                "name": row.get("dscription", "")[:64] if row.get("dscription") else "",
                "product_id": product_id,
                "quantity": quantity,
                "price_unit": -stockprice,  # Negative for expense side
                "debit": stockvalue,
                "credit": 0.0,
                "account_id": cogs_account_id,
                "display_type": "cogs",
                "tax_ids": [],
            },
        ]

        return cogs_lines

    @api.model
    def _lookup_tax(self, row, sap_table, taxes_dict):
        """Look up Odoo tax ID by SAP tax code using pre-computed dict.

        Args:
            row: Dict with SAP line values (must include vatgroup/taxcode when set)
            sap_table: SAP table name (inv1 for sales, pch1 for purchases)
            taxes_dict: Pre-computed dict {(sap_tax_code, type_tax_use): tax_id}

        Returns:
            tax_id (int) or False
        """
        type_tax_use = "sale" if "inv" in sap_table.lower() else "purchase"
        vatgroup = (row.get("vatgroup") or "").strip()
        taxcode = (row.get("taxcode") or "").strip()

        # For purchases (PCH1) SAP uses taxcode; for sales (INV1) it uses vatgroup.
        primary = vatgroup if type_tax_use == "sale" else taxcode
        fallback = taxcode if type_tax_use == "sale" else vatgroup

        for code in [primary, fallback]:
            if not code:
                continue
            tax_id = taxes_dict.get((code, type_tax_use))
            if tax_id:
                return tax_id

        if primary or fallback:
            _logger.warning(
                "Tax not found for SAP code '%s' (fallback '%s') type=%s table=%s docentry=%s line=%s",
                primary or fallback,
                fallback if primary else "",
                type_tax_use,
                sap_table,
                row.get("docentry"),
                row.get("linenum"),
            )

        return False

    # Display types that don't contribute to the invoice/bill total
    _NON_AMOUNT_DISPLAY_TYPES = (
        "line_note", "line_section", "line_subsection", "cogs",
    )

    @staticmethod
    def _compute_move_line_total(line_commands):
        """Estimate the untaxed total based on prepared line commands."""

        total = 0.0
        for command in line_commands or []:
            if command[0] != 0:
                continue
            line_vals = command[2]
            if line_vals.get("display_type") in (
                AccountMoveCommon._NON_AMOUNT_DISPLAY_TYPES
            ):
                continue
            qty = line_vals.get("quantity") or 0.0
            price = line_vals.get("price_unit") or 0.0
            discount = line_vals.get("discount") or 0.0
            total += qty * price * (1 - discount / 100.0)
        return total

    @staticmethod
    def _invert_move_line_commands(line_commands):
        """Invert line amounts so refunds have positive totals.

        Only invert quantity (not price_unit) to flip the sign of the line total.
        Inverting both would leave the total unchanged.
        """
        for command in line_commands or []:
            if command[0] != 0:
                continue
            line_vals = command[2]
            if line_vals.get("display_type") in (
                AccountMoveCommon._NON_AMOUNT_DISPLAY_TYPES
            ):
                continue
            if "quantity" in line_vals and line_vals["quantity"]:
                line_vals["quantity"] = -line_vals["quantity"]

    def _normalize_move_type(self, move_vals, invoice_move_type, refund_move_type):
        """Ensure the move has the correct type/sign based on line totals."""

        total = self._compute_move_line_total(move_vals.get("line_ids"))
        if total < 0:
            move_vals["move_type"] = refund_move_type
            self._invert_move_line_commands(move_vals.get("line_ids"))
        else:
            move_vals["move_type"] = invoice_move_type

    @api.model
    def import_order_invoiced_qty(self, cr):
        """Get the invoiced quantity for each SAP order line that has been invoiced."""
        links = self._get_order_line_links_raw(cr)
        order_lines = [
            (line["orderdocentry"], (line["orderlinenum"] or 0) + 2, line["quantity"])
            for line in links
            if line["orderdocentry"] and line["orderlinenum"] is not None
        ]
        model = self._get_order_line_link_config()["order_line_model"]
        table = model.replace(".", "_")
        field = "sap_qty_invoiced"
        # Create a temporary table to hold the data
        self.env.cr.execute("DROP TABLE IF EXISTS temp_order_lines")
        self.env.cr.execute(
            """
            CREATE TEMP TABLE temp_order_lines (
                docentry INTEGER,
                linenum INTEGER,
                quantity FLOAT
            ) ON COMMIT DROP
        """
        )

        _logger.info(f"Updating sap_qty_invoiced for {len(links)} {model} entries.")
        # Process in chunks to avoid PostgreSQL's limit on target list entries (1664)
        chunk_size = 500  # Safe value well below the limit
        for i in range(0, len(order_lines), chunk_size):
            chunk = order_lines[i : i + chunk_size]
            _logger.info(
                f"Processing chunk {i//chunk_size + 1} with {len(chunk)} entries"
            )

            # Insert data into the temporary table
            args = ",".join(["(%s,%s,%s)"] * len(chunk))
            params = [val for tup in chunk for val in tup]
            self.env.cr.execute(
                f"""
                INSERT INTO temp_order_lines (docentry, linenum, quantity)
                VALUES {args}
            """,
                params,
            )

        # Update the target table using the temporary table
        self.env.cr.execute(
            f"""
            UPDATE {table}
            SET {field} = temp_sum.quantity
            FROM (
                SELECT docentry, linenum, SUM(quantity) as quantity
                FROM temp_order_lines
                GROUP BY docentry, linenum
            ) temp_sum
            WHERE {table}.sap_docentry = temp_sum.docentry
            AND {table}.sap_line_num = temp_sum.linenum
        """
        )

        # As there are open SAP invoices that we have imported, we need to deduct
        # the open Odoo invoice quantity stemming from SAP invoices

        # Since this is a stored field, we now need to trigger recalculation.
        # Invalidate the ORM cache for sap_qty_invoiced first so the compute
        # reads the freshly-written DB values instead of the pre-UPDATE cache.
        self.env[model].invalidate_model(["sap_qty_invoiced"])
        lines = self.env[model].search([("sap_qty_invoiced", "!=", False)])
        self._trigger_recomputation(lines)

    def _get_order_line_links_raw(self, cr):
        config = self._get_order_line_link_config()
        if not config:
            return {}

        cr.execute(
            """
            SELECT 
                {invoice_line_table}.DocEntry AS invoicedocentry,
                {invoice_line_table}.LineNum AS invoicelinenum,
                {invoice_line_table}.Quantity AS quantity,
                CASE 
                    WHEN {invoice_line_table}.BaseType = {order_basetype} THEN {invoice_line_table}.BaseEntry  -- Direct from sales order
                    WHEN {invoice_line_table}.BaseType = {picking_basetype} THEN (  -- Through delivery
                        SELECT BaseEntry 
                        FROM {picking_table}
                        WHERE DocEntry = {invoice_line_table}.BaseEntry 
                        AND LineNum = {invoice_line_table}.BaseLine
                    )
                END as orderdocentry,
                CASE 
                    WHEN {invoice_line_table}.BaseType = {order_basetype} THEN {invoice_line_table}.BaseLine  -- Direct from sales order
                    WHEN {invoice_line_table}.BaseType = {picking_basetype} THEN (  -- Through delivery
                        SELECT BaseLine 
                        FROM {picking_table}
                        WHERE DocEntry = {invoice_line_table}.BaseEntry 
                        AND LineNum = {invoice_line_table}.BaseLine
                    )
                END as orderlinenum
            FROM {invoice_line_table}
            WHERE {invoice_line_table}.BaseType IN ({picking_basetype}, {order_basetype})  -- delivery or sales order
            """.format(
                invoice_line_table=config["invoice_line_table"],
                picking_table=config["picking_table"],
                picking_basetype=config["picking_basetype"],
                order_basetype=config["order_basetype"],
            )
        )
        return cr.dictfetchall()

    @api.model
    def _get_order_line_links(self, cr):
        """Get links between move lines and order lines.

        This method finds links between invoice lines and order lines through two paths:
        1. Direct link: Invoice line -> Sales Order line
        2. Through delivery: Invoice line -> Delivery line -> Sales Order line
        """
        config = self._get_order_line_link_config()
        if not config:
            return {}

        rel_lines = self._get_order_line_links_raw(cr)

        # Only get product lines (where sap_line_num is set)
        order_lines = self.env[config["order_line_model"]].search_read(
            [
                ("sap_docentry", "!=", False),
                ("sap_line_num", "!=", False),
                ("sap_table", "=", config["order_line_table"].lower()),
            ],
            ["id", "sap_docentry", "sap_line_num"],
        )
        order_lines_dict = {
            # The sap_line_num in order lines already has +2, so we need to subtract it here
            (line["sap_docentry"], line["sap_line_num"] - 2): line["id"]
            for line in order_lines
        }
        return {
            # The invoice line numbers from SAP don't have +2 yet
            (row["invoicedocentry"], row["invoicelinenum"]): order_lines_dict.get(
                (row["orderdocentry"], row["orderlinenum"])
            )
            for row in rel_lines
        }

    def _get_order_line_link_config(self):
        """Get configuration for linking to order lines. Override in child classes."""
        raise NotImplementedError(
            "Subclasses must implement _get_order_line_link_config()"
        )

    def _get_order_line_link_vals(self, order_line_id):
        """Get the values to link to an order line. Override in child classes."""
        raise NotImplementedError(
            "Subclasses must implement _get_order_line_link_vals()"
        )

    @api.model
    def _get_lines(self, cr, lines_table, sap_orders):
        docentries = [order["docentry"] for order in sap_orders]
        # Get product lines with account formatcode, COGS account, and stock
        # valuation account (resolved from item group: OITM → OITB → OACT)
        query = SQL(
            """
            SELECT
                l.*,
                'product' as line_type,
                a.formatcode as acct_formatcode,
                cogs.formatcode as cogs_formatcode,
                stock_acct.formatcode as stock_acct_formatcode
            FROM %s l
            LEFT JOIN oact a ON l.acctcode = a.acctcode
            LEFT JOIN oact cogs ON l.cogsacct = cogs.acctcode
            LEFT JOIN oitm ON l.itemcode = oitm.itemcode
            LEFT JOIN oitb ON oitm.itmsgrpcod = oitb.itmsgrpcod
            LEFT JOIN oact stock_acct ON oitb.balinvntac = stock_acct.acctcode
            WHERE l.docentry in %s
            ORDER BY l.docentry, l.linenum
            """,
            SQL.identifier(lines_table),
            tuple(docentries),
        )
        cr.execute(query)
        product_lines = cr.dictfetchall()

        # Get text lines from INV10/PCH10
        text_table = lines_table.replace(
            "1", "10"
        )  # Convert INV1->INV10 or PCH1->PCH10
        query = SQL(
            """
            SELECT *, 'text' as line_type
            FROM %s
            WHERE docentry in %s
                AND linetext IS NOT NULL
                AND linetext <> ''
            ORDER BY aftlinenum, lineseq
            """,
            SQL.identifier(text_table),
            tuple(docentries),
        )
        cr.execute(query)
        text_lines = cr.dictfetchall()

        # Get document-level expense lines from INV3/PCH3/RIN3/RPC3
        expense_table = lines_table.replace("1", "3")
        cr.execute(
            SQL(
                """
                SELECT e.docentry, e.expnscode, e.linetotal, e.vatgroup,
                       e.vatsum, x.expnsname,
                       a.formatcode AS acct_formatcode,
                       'expense' AS line_type
                  FROM %s e
                  JOIN oexd x ON e.expnscode = x.expnscode
                  JOIN oact a ON x.expnsacct = a.acctcode
                 WHERE e.docentry IN %s
                   AND e.linetotal <> 0
                 ORDER BY e.docentry, e.expnscode
                """,
                SQL.identifier(expense_table),
                tuple(docentries),
            )
        )
        expense_lines = cr.dictfetchall()

        # Merge and return all lines
        return product_lines + text_lines + expense_lines

    @api.model
    def _get_move_vals(
        self,
        order,
        partner_id,
        lines,
        sap_header_table,
        sap_line_table,
        order_lines_dict,
        lookups,
    ):
        """Get common values for both invoices and bills.

        Args:
            order: SAP header record
            partner_id: Odoo partner ID (int)
            lines: Dict of lines by docentry
            sap_header_table: SAP header table name (e.g., 'oinv', 'opch')
            sap_line_table: SAP line table name (e.g., 'inv1', 'pch1')
            order_lines_dict: Dict mapping (docentry, linenum) to order line IDs
            lookups: Pre-computed lookup dicts with keys:
                - products: {sap_item_code: product_id}
                - users: {sap_slpcode: user_id}
                - currencies: {currency_code: currency_id}
                - currency_rates: {(currency_id, date): rate_id}
                - accounts: {sap_acct_code: (account_id, account_type)}
                - taxes: {(sap_tax_code, type_tax_use): tax_id}
                - unit_uom_id: ID of uom.product_uom_unit
                - company_currency_id: ID of company currency
        """
        if order["docentry"] in lines:
            doc_lines = lines[order["docentry"]]

            # Compute document-level discount factor.  SAP's discsum is
            # distributed proportionally across product line totals.
            discsum = float(order.get("discsum") or 0)
            product_total = sum(
                float(l.get("linetotal") or 0)
                for l in doc_lines
                if l.get("line_type") == "product"
            )
            if discsum and product_total:
                discount_factor = 1.0 - discsum / product_total
            else:
                discount_factor = 1.0

            move_lines = []
            for line in doc_lines:
                row_vals = self._get_row_vals(
                    line, sap_line_table, order_lines_dict, lookups,
                )
                if row_vals is None:
                    continue

                # Apply document discount to product lines
                if (discount_factor != 1.0
                        and line.get("line_type") == "product"):
                    row_vals["price_unit"] = round(
                        row_vals["price_unit"] * discount_factor, 2,
                    )

                move_lines.append(Command.create(row_vals))

                # Add COGS lines for sales documents
                if (line.get("line_type") == "product"
                        and sap_line_table.lower() in ("inv1", "rin1")):
                    cogs_lines = self._get_cogs_line_vals(line, lookups)
                    for cogs_vals in cogs_lines:
                        move_lines.append(Command.create(cogs_vals))
        else:
            move_lines = []

        users_dict = lookups["users"]
        invoice_user_id = users_dict.get(order.get("slpcode"), False)

        # Get the currency from SAP's DocCur field, default to company currency if not set
        currencies_dict = lookups["currencies"]
        company_currency_id = lookups["company_currency_id"]
        currency_code = order.get("doccur") or None
        currency_id = currencies_dict.get(currency_code, company_currency_id)

        # Get the currency rate from SAP's DocRate field
        # SAP stores the rate as foreign currency to base currency
        # Odoo stores it as 1 / that rate
        rate = order.get("docrate", 1.0)
        if rate and rate != 1.0:
            date = fix_tz(order["docdate"])
            currency_rates = lookups.get("currency_rates", {})
            rate_key = (currency_id, str(date))
            if rate_key not in currency_rates:
                # Collect missing rate for batch creation later
                # Store as (currency_id, date, rate) tuple in pending_rates
                pending_rates = lookups.setdefault("pending_rates", [])
                pending_rates.append(
                    {
                        "currency_id": currency_id,
                        "rate": 1.0 / rate,  # Invert the rate for Odoo
                        "name": date,
                        "company_id": lookups.get("company_id", self.env.company.id),
                    }
                )
                # Mark as pending so we don't add duplicates
                currency_rates[rate_key] = True

        vals = {
            "partner_id": partner_id,
            "invoice_date": fix_tz(order["docdate"]),
            "date": fix_tz(order["docdate"]),
            "invoice_date_due": fix_tz(order["docduedate"]),
            "sap_docentry": order["docentry"],
            "sap_docnum": order["docnum"],
            "sap_table": sap_header_table,
            "ref": order["numatcard"],
            "line_ids": move_lines,
            "invoice_user_id": invoice_user_id,
            "currency_id": currency_id,
        }

        return vals

    @api.model
    def _create_pending_currency_rates(self, lookups):
        """Batch-create any pending currency rates collected during transform.

        This should be called before creating account.move records to ensure
        the currency rates exist.
        """
        pending_rates = lookups.get("pending_rates", [])
        if not pending_rates:
            return

        # Deduplicate by (currency_id, name) - keep first occurrence
        seen = set()
        unique_rates = []
        for rate in pending_rates:
            key = (rate["currency_id"], str(rate["name"]))
            if key not in seen:
                seen.add(key)
                unique_rates.append(rate)

        if unique_rates:
            _logger.info(f"Batch-creating {len(unique_rates)} currency rates")
            self.env["res.currency.rate"].create(unique_rates)

        # Clear pending rates
        lookups["pending_rates"] = []

    @api.model
    def _build_lookups(self):
        """Build all lookup dicts needed for transform.

        Returns a dict with:
            - products: {sap_item_code: product_id}
            - users: {sap_slpcode: user_id}
            - currencies: {currency_code: currency_id}
            - currency_rates: {} (mutable, populated during transform)
            - accounts: {sap_acct_code: (account_id, account_type)}
            - taxes: {(sap_tax_code, type_tax_use): tax_id}
            - unit_uom_id: ID of uom.product_uom_unit
            - company_currency_id: ID of company currency
        """
        # Products
        products = self.env["product.product"].search_read(
            [("sap_item_code", "!=", False)],
            ["id", "sap_item_code"],
        )
        products_dict = {p["sap_item_code"]: p["id"] for p in products}

        # Users
        users = self.env["res.users"].search_read(
            [("sap_slpcode", "!=", False), ("active", "in", [False, True])],
            ["id", "sap_slpcode"],
        )
        users_dict = {u["sap_slpcode"]: u["id"] for u in users}

        # Currencies
        currencies = self.env["res.currency"].search_read(
            [("active", "in", [False, True])],
            ["id", "name"],
        )
        currencies_dict = {c["name"]: c["id"] for c in currencies}

        # Pre-fetch existing currency rates to avoid per-document lookups
        company_id = self.env.company.id
        existing_rates = self.env["res.currency.rate"].search_read(
            [("company_id", "=", company_id)],
            ["currency_id", "name"],
        )
        # Key: (currency_id, date_string), Value: True (exists)
        currency_rates_dict = {
            (r["currency_id"][0], str(r["name"])): True for r in existing_rates
        }

        # Accounts (for vendor bills)
        accounts = self.env["account.account"].search_read(
            [("sap_acct_code", "!=", False)],
            ["id", "sap_acct_code", "account_type"],
        )
        accounts_dict = {
            a["sap_acct_code"]: (a["id"], a["account_type"]) for a in accounts
        }

        # Taxes
        taxes = self.env["account.tax"].search_read(
            [("sap_tax_code", "!=", False)],
            ["id", "sap_tax_code", "type_tax_use"],
        )
        taxes_dict = {(t["sap_tax_code"], t["type_tax_use"]): t["id"] for t in taxes}

        # UoM unit
        unit_uom_id = self.env.ref("uom.product_uom_unit").id

        # Company currency
        company_currency_id = self.env.company.currency_id.id

        # Unallocated Earnings clearing account (code 999999) —
        # destination for P&L legs of SAP B1 Period-End-Closing JEs
        # (OJDT.transtype='-3').  Looked up by Odoo `code` (not by
        # sap_acct_code) because the account exists only in Odoo's
        # chart, not in SAP's OACT.  Hard-fail if missing or mistyped
        # so a misconfigured target DB surfaces immediately rather
        # than silently mis-classifying year-end closing entries.
        unallocated_account = self.env["account.account"].search(
            [
                ("code", "=", "999999"),
                ("company_ids", "in", [company_id]),
            ],
            limit=1,
        )
        if not unallocated_account:
            unallocated_account = self.env["account.account"].search(
                [("code", "=", "999999")], limit=1,
            )
        if not unallocated_account:
            raise UserError(_(
                "Unallocated Earnings account (code '999999') not found. "
                "It is required to redirect P&L legs of SAP B1 "
                "Period-End-Closing journal entries (transtype='-3'). "
                "Please create an account with code '999999' and "
                "account_type='equity_unaffected' before importing."
            ))
        if unallocated_account.account_type != "equity_unaffected":
            raise UserError(_(
                "Account '999999' must have account_type "
                "'equity_unaffected' (currently '%s'). "
                "This account is used as the closing-entry clearing "
                "account for SAP B1 Period-End-Closing JEs."
            ) % unallocated_account.account_type)
        unallocated_earnings_id = unallocated_account.id

        return {
            "products": products_dict,
            "users": users_dict,
            "currencies": currencies_dict,
            "currency_rates": currency_rates_dict,
            "accounts": accounts_dict,
            "taxes": taxes_dict,
            "unit_uom_id": unit_uom_id,
            "company_currency_id": company_currency_id,
            "company_id": company_id,
            "unallocated_earnings_id": unallocated_earnings_id,
        }

    @api.model
    def _get_users_dict(self):
        """Deprecated: Use _build_lookups() instead."""
        return {
            user.sap_slpcode: user.id
            for user in self.env["res.users"].search(
                [
                    ("sap_slpcode", "!=", False),
                    ("active", "in", [False, True]),
                ]
            )
        }

    @api.model
    def _get_products_dict(self):
        """Deprecated: Use _build_lookups() instead."""
        return {
            product.sap_item_code: product
            for product in self.env["product.product"].search(
                [("sap_item_code", "!=", False)]
            )
        }

    @api.model
    def _get_partners_dict(self):
        return {
            partner.sap_card_code: partner
            for partner in self.env["res.partner"].search(
                [("sap_card_code", "!=", False)]
            )
        }
