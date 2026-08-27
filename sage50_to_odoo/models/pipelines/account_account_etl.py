"""Sage 50 `taccount` -> `account.account`.

The account *type* is what drives every financial report in Odoo, not the
number, so this is where a take-on is won or lost. Sage does not record one:
it draws its reports from the account's position in the chart, and its charts
are strictly sectioned by leading digit. The type is therefore derived from
the number range, with named overrides for the accounts whose behaviour
cannot be read off their range — the control accounts, the banks, the credit
cards, retained earnings, depreciation.

Both are hooks. `_account_type_overrides` is where a client layer belongs;
`_range_rules` only needs touching for a chart that departs from Sage's own
sectioning.
"""

import logging

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

from odoo.addons.sage50_to_odoo import tools

_logger = logging.getLogger(__name__)

#: Number-range rules, evaluated in order. Each entry is
#: (low, high_inclusive, odoo_account_type). These follow Sage's own chart
#: sections: 1 assets, 2 liabilities, 3 equity, 4 revenue, 5 cost of sales,
#: 6-9 expenses.
RANGE_RULES = [
    (10000000, 12999999, "asset_current"),
    (13000000, 13999999, "asset_prepayments"),
    (15000000, 15999999, "asset_current"),      # inventory
    (16000000, 16999999, "asset_non_current"),
    (18000000, 18999999, "asset_fixed"),        # incl. accumulated depreciation
    (19000000, 19999999, "asset_non_current"),  # intangibles: no Odoo type
    (20000000, 25999999, "liability_current"),
    (26000000, 29999999, "liability_non_current"),
    (30000000, 39999999, "equity"),
    (40000000, 48999999, "income"),
    (49000000, 49999999, "income_other"),
    (50000000, 59999999, "expense_direct_cost"),
    (60000000, 99999999, "expense"),
]


@ETL.pipeline(
    target_model="account.account",
    importer_name="sage.account.importer",
    sap_source="taccount",
    allow_multiprocessing=False,
)
class SageAccountImporter(models.AbstractModel):
    _name = "sage.account.importer"
    _description = "Sage 50 Chart of Accounts Importer"

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------
    def _account_type_overrides(self) -> dict:
        """Sage account number -> Odoo `account_type`, for the exceptions.

        Empty by default: every chart's exceptions are its own. At minimum a
        real chart needs its receivable and payable control accounts named
        here, since nothing in their number says what they are, and the whole
        open-items step keys off finding them.
        """
        return {}

    def _range_rules(self) -> list:
        return RANGE_RULES

    def _tax_account_aliases(self) -> dict:
        """Sage tax account number -> the Odoo account code to use instead.

        Sage's own GST/QST accounts are deliberately NOT imported when a
        localisation is installed. l10n_ca's tax accounts are already wired
        into the tax repartition lines and the GST/QST report; importing
        Sage's alongside them gives the company two sets and leaves the
        report reading the wrong one. Mapping Sage's numbers onto Odoo's
        instead keeps one set and one report.
        """
        return {}

    def _odoo_code(self, sage_account_id: int) -> str:
        """Sage's 8-digit numbers are 4 significant digits and four zeros.

        Carrying all eight into Odoo makes every report unreadable for no
        gain. The mapping stays reversible because `sage_account_id` keeps
        the Sage number verbatim on the account.
        """
        trimmed = str(sage_account_id).rstrip("0")
        return trimmed if len(trimmed) >= 4 else str(sage_account_id)[:4]

    def _odoo_account_type(self, sage_account_id: int) -> str:
        override = self._account_type_overrides().get(sage_account_id)
        if override:
            return override
        for low, high, account_type in self._range_rules():
            if low <= sage_account_id <= high:
                return account_type
        raise ValueError(f"no rule for Sage account {sage_account_id}")

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    @ETL.extract("taccount")
    def extract_accounts(self, ctx: ETLContext) -> list:
        return tools.query(
            ctx.cr,
            """select lId, sName, sNameAlt, cFunc, sGifiCode,
                      bInactive, bUsed, dYts, dYtc
                 from taccount
                where cFunc in %s
                order by lId""",
            (tools.POSTABLE_FUNCS,),
        )

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------
    @ETL.transform()
    def transform_accounts(self, ctx: ETLContext, extracted: dict) -> list:
        source = ctx.env[ctx.get_config("source_model")].browse(
            ctx.get_config("source_id")
        )
        aliases = self._tax_account_aliases()
        codes, values = {}, []
        for row in extracted["extract_accounts"]:
            sage_id = row["lId"]
            if sage_id in aliases:
                # Handled by pointing the alias at an existing localisation
                # account in the load phase; nothing to create.
                continue
            code = self._odoo_code(sage_id)
            # Trimming trailing zeros can collide (10400000 and 10405000 both
            # become "104"). Detect rather than silently overwrite one.
            if code in codes:
                raise ValueError(
                    f"Sage accounts {codes[code]} and {sage_id} both trim to "
                    f"the Odoo code {code}"
                )
            codes[code] = sage_id
            values.append({
                "sage_account_id": sage_id,
                "code": code,
                "name": source.sage_name(row, "sName", "sNameAlt"),
                "account_type": self._odoo_account_type(sage_id),
            })
        return values

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @ETL.load()
    def load_accounts(self, ctx: ETLContext, transformed: dict) -> None:
        Account = ctx.env["account.account"]
        company_id = ctx.get_config("company_id")
        existing = {
            account.sage_account_id: account
            for account in Account.search([
                ("company_ids", "in", company_id),
                ("sage_account_id", "!=", 0),
            ])
        }
        created = updated = 0
        for values in transformed["transform_accounts"]:
            account = existing.get(values["sage_account_id"])
            if account:
                account.write(values)
                updated += 1
            else:
                Account.create(dict(values, company_ids=[(4, company_id)]))
                created += 1
            ctx.report.success()
        _logger.info(
            "Sage accounts: %s created, %s updated.", created, updated
        )
        self._alias_tax_accounts(ctx)
        self._set_default_partner_accounts(ctx)

    def _alias_tax_accounts(self, ctx: ETLContext) -> None:
        """Point the Sage tax account numbers at the localisation's accounts.

        The alias is recorded on the localisation account itself, so every
        later step — which looks accounts up by `sage_account_id` — resolves
        a Sage tax account to the Odoo one without knowing anything about it.

        Several Sage numbers usually share one Odoo account: collected and
        paid GST both land on the localisation's single GST account. Only the
        first is recorded in the field, which is enough for a human tracing a
        record back, but is why nothing downstream may read the mapping *out*
        of the field — `_tax_account_aliases` stays the source of truth.
        """
        aliases = self._tax_account_aliases()
        if not aliases:
            return
        company_id = ctx.get_config("company_id")
        for sage_id, code in aliases.items():
            account = ctx.env["account.account"].search([
                ("code", "=", code), ("company_ids", "in", company_id),
            ], limit=1)
            if not account:
                ctx.report.warning(
                    f"No account {code} to alias Sage {sage_id} onto",
                    source_ref=str(sage_id),
                )
                continue
            if not account.sage_account_id:
                account.sage_account_id = sage_id

    def _set_default_partner_accounts(self, ctx: ETLContext) -> None:
        """Make the imported control accounts the company's partner defaults.

        The client recognises their own numbers; leaving the defaults on the
        localisation's own receivable and payable means every partner created
        afterwards lands somewhere the take-on never touched.
        """
        company_id = ctx.get_config("company_id")
        for account_type, field_name in (
            ("asset_receivable", "property_account_receivable_id"),
            ("liability_payable", "property_account_payable_id"),
        ):
            accounts = ctx.env["account.account"].search([
                ("company_ids", "in", company_id),
                ("account_type", "=", account_type),
                ("sage_account_id", "!=", 0),
            ])
            if len(accounts) != 1:
                # Zero means the chart has no override naming a control
                # account; more than one means the choice is not ours to make.
                ctx.report.warning(
                    f"{len(accounts)} imported {account_type} accounts — "
                    f"partner default left alone"
                )
                continue
            ctx.env["ir.default"].set(
                "res.partner", field_name, accounts.id, company_id=company_id
            )

    # ------------------------------------------------------------------
    # Shared lookup
    # ------------------------------------------------------------------
    def sage_account_map(self, ctx: ETLContext) -> dict:
        """Sage account number -> Odoo `account.account` id.

        Resolves the tax-account aliases too, so a Sage tax account number
        answers with the localisation's account rather than nothing.
        """
        company_id = ctx.get_config("company_id")
        Account = ctx.env["account.account"]
        mapping = {
            account.sage_account_id: account.id
            for account in Account.search([
                ("company_ids", "in", company_id),
                ("sage_account_id", "!=", 0),
            ])
        }
        aliases = self._tax_account_aliases()
        for sage_id, code in aliases.items():
            account = Account.search([
                ("code", "=", code), ("company_ids", "in", company_id),
            ], limit=1)
            if account:
                mapping[sage_id] = account.id
        return mapping
