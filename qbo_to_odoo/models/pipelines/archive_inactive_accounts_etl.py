"""QuickBooks Online Inactive Account Archive Pipeline

This pipeline archives QBO-imported accounts that were inactive in QBO
(Active=false). It runs after all other pipelines so that archived accounts
remain findable via with_context(active_test=False) without interfering with
any preceding pipeline that may need to look up accounts by QBO ID.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.account",
    importer_name="qbo.inactive.account.archiver",
    sap_source="Account",
    depends_on=[
        "qbo.customer.linker",
        "qbo.deposit.importer",
        "qbo.employee.importer",
        "qbo.expense.importer",
        "qbo.journal.entry.importer",
        "qbo.payment.importer",
        "qbo.product.category.fixer",
        "qbo.refund.receipt.importer",
        "qbo.sales.receipt.importer",
        "qbo.transfer.importer",
        "qbo.vendor.linker",
    ],
)
class QboInactiveAccountArchiver(models.AbstractModel):
    """Post-processing pipeline to archive accounts that were inactive in QBO.

    Accounts are imported (active=True) by the account pipeline so that all
    subsequent pipelines can reference them by QBO ID without needing
    active_test=False.  Once all data has been migrated this pipeline sets
    active=False on any account whose QBO source record had Active=false.
    """

    _name = "qbo.inactive.account.archiver"
    _description = "QBO Inactive Account Archiver"

    @ETL.extract("Account")
    def extract_inactive_accounts(self, ctx: ETLContext) -> List[Dict]:
        """Fetch QBO accounts that were inactive and find their Odoo counterparts."""
        from .utils import get_api_client

        api_client = get_api_client(ctx)

        inactive_qbo_accounts = api_client.query_all(
            entity="Account",
            where="Active = false",
            order_by="Id",
        )

        inactive_qbo_ids = [int(a["Id"]) for a in inactive_qbo_accounts]
        _logger.info(
            f"Found {len(inactive_qbo_ids)} inactive accounts in QBO"
        )
        return {"inactive_qbo_ids": inactive_qbo_ids}

    @ETL.transform()
    def transform_inactive_accounts(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[int]:
        """Resolve QBO IDs to Odoo account IDs."""
        inactive_qbo_ids = extracted.get("extract_inactive_accounts", {}).get(
            "inactive_qbo_ids", []
        )

        if not inactive_qbo_ids:
            _logger.info("No inactive QBO accounts found — nothing to archive")
            return []

        company = ctx.env.company
        odoo_accounts = (
            ctx.env["account.account"]
            .with_context(active_test=False)
            .search(
                [
                    ("qbo_id", "in", inactive_qbo_ids),
                    ("company_ids", "in", [company.id]),
                    ("active", "=", True),
                ]
            )
        )

        _logger.info(
            f"Matched {len(odoo_accounts)} active Odoo accounts to archive "
            f"({len(inactive_qbo_ids)} inactive QBO IDs)"
        )
        return odoo_accounts.ids

    @ETL.load()
    def load_archive(self, ctx: ETLContext, transformed: Dict) -> None:
        """Archive the inactive accounts."""
        account_ids = transformed.get("transform_inactive_accounts", [])

        if not account_ids:
            _logger.info("No accounts to archive")
            return

        accounts = (
            ctx.env["account.account"]
            .with_context(active_test=False)
            .browse(account_ids)
        )
        accounts.write({"active": False})
        _logger.info(
            f"Archived {len(accounts)} inactive QBO accounts: "
            f"{', '.join(accounts.mapped('code'))}"
        )
