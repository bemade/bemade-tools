# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class IrModuleModule(models.Model):
    _inherit = "ir.module.module"

    def _register_hook(self):
        """Prevent chart template auto-installation after SAP import.

        The SAP import creates its own CoA and journals, so we don't want
        Odoo's chart template to try installing afterwards and creating duplicates.
        """
        # Check if we're in a SAP-imported company
        company = self.env.company
        if company:
            # Check if SAP accounts exist (indicates SAP import has run)
            sap_accounts = self.env["account.account"].search(
                [("company_ids", "in", [company.id]), ("sap_acct_code", "!=", False)],
                limit=1,
            )

            if sap_accounts:
                _logger.info(
                    "SAP CoA detected - skipping chart template auto-installation "
                    "to avoid duplicate journals/accounts"
                )
                # Mark chart template as installed by setting the flag
                company.chart_template = "skip"
                return

        # Otherwise, proceed with normal chart template installation
        return super()._register_hook()
