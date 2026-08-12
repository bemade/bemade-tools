"""QBO Year-End Closing Entries Finalizer.

Posts one balanced closing entry per already-closed fiscal year, sweeping
that year's net P&L result from the Current-Year-Earnings account (322000
in this migration, resolved by ``account_type`` rather than hardcoded) to
the configured Retained Earnings account (3200 by default) — the same
"close the books" sweep a real year-end performs. This makes Odoo's
report-only "Undistributed Profits/Losses" line read 0.00 for every closed
fiscal year, leaving only the currently-open FY unallocated.

Config-gated (default disabled) via the
``qbo_to_odoo.year_end_close.enabled`` ``ir.config_parameter`` — every
migration must opt in explicitly. Idempotent via the
``QBO_YE_CLOSE:<FY>`` ``account.move.ref`` stamp, mirroring the
``QBO_EXCH:``/``QBO_CENT_TRUEUP`` conventions used elsewhere in this
module.
"""

import logging
from typing import Dict, List, Tuple

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .gl_helpers import (
    YEAR_END_CLOSE_ENABLED_PARAM,
    build_year_end_close_move_vals,
    compute_fiscal_year_close_rows,
    get_year_end_close_config,
    get_year_end_close_journal,
    validate_year_end_close_accounts,
)

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.year.end.close",
    depends_on=["qbo.account.finalizer"],
)
class QboYearEndClose(models.AbstractModel):
    """Post year-end closing entries for every closed fiscal year.

    Runs terminal — AFTER ``qbo.account.finalizer`` (reconciliation retry,
    realized-FX true-up, and cent-scale rounding parity) — so each fiscal
    year is closed off a *final* posted ledger. The heavy computation
    (enumerating closed FYs off ``res.company`` config and summing each
    year's signed P&L balance) lives in the side-effect-free
    ``compute_fiscal_year_close_rows`` helper; the balanced move vals come
    from the pure ``build_year_end_close_move_vals`` builder. This class
    only wires those together and performs the two mutations: unarchiving
    the CYE account (``qbo.account.finalizer`` archives every account QBO
    marked inactive, and the CYE account may be one of them even though it
    must stay postable) and posting/skipping idempotently.
    """

    _name = "qbo.year.end.close"
    _description = "QBO Year-End Close Finalizer"

    @ETL.extract()
    def extract_year_end_close_rows(
        self, ctx: ETLContext
    ) -> List[Tuple]:
        """Compute one closing row per already-closed fiscal year.

        Gated on ``enabled`` purely to avoid wasted computation and the
        "duplicate CYE account" ``UserError`` (see
        ``get_unaffected_earnings_account``) on every migration that
        hasn't opted into year-end close — this pipeline is registered
        unconditionally, so extract would otherwise run for every
        company on every migration. The pure
        ``compute_fiscal_year_close_rows`` helper itself remains
        gate-free and directly unit-testable; the loud
        misconfiguration *validation* (does the CYE/RE account resolve
        and is RE the right type) still lives entirely in
        ``load_year_end_close``, alongside the mutations it guards.
        """
        env = ctx.env
        config = get_year_end_close_config(env)
        if not config["enabled"]:
            return []
        rows = compute_fiscal_year_close_rows(env, env.company, config)
        _logger.info(
            "Year-end close: %d fiscal year(s) with a non-zero result",
            len(rows),
        )
        return rows

    @ETL.transform()
    def transform_year_end_close(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Build a balanced move-vals dict per row via the pure builder."""
        rows = extracted.get("extract_year_end_close_rows", [])
        if not rows:
            return []

        journal = get_year_end_close_journal(ctx.env, ctx.env.company)
        if not journal:
            _logger.warning(
                "Year-end close: no miscellaneous journal found — "
                "nothing to post"
            )
            return []

        return [
            build_year_end_close_move_vals(
                fy_label,
                date_to,
                signed_result,
                cye_account_id,
                re_account_id,
                journal.id,
            )
            for fy_label, date_to, signed_result, cye_account_id, re_account_id
            in rows
        ]

    @ETL.load()
    def load_year_end_close(self, ctx: ETLContext, transformed: Dict) -> None:
        """Config-gate, unarchive the CYE account, post/skip idempotently."""
        env = ctx.env
        config = get_year_end_close_config(env)
        if not config["enabled"]:
            _logger.info(
                "Year-end close skipped: %s is disabled",
                YEAR_END_CLOSE_ENABLED_PARAM,
            )
            return

        # RE account validation happens as a side effect of this call; the
        # RE account itself is already baked into transformed move_vals.
        cye_account, _re_account = validate_year_end_close_accounts(
            env, env.company, config
        )
        if not cye_account.active:
            cye_account.write({"active": True})
            _logger.info(
                "Year-end close: unarchived CYE account %s (%s)",
                cye_account.code, cye_account.name,
            )

        move_vals_list = transformed.get("transform_year_end_close", [])
        if not move_vals_list:
            _logger.info("Year-end close: nothing to post")
            return

        AccountMove = env["account.move"]
        posted = 0
        for vals in move_vals_list:
            ref = vals["ref"]
            if AccountMove.search_count([("ref", "=", ref)]):
                continue
            with ctx.skippable(ref):
                move = AccountMove.create(vals)
                move.action_post()
                posted += 1

        _logger.info(
            "Year-end close: posted %d closing entry(ies) (%d already "
            "present)",
            posted, len(move_vals_list) - posted,
        )
