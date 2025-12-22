# -*- coding: utf-8 -*-
import logging
import os

from odoo import models

_logger = logging.getLogger(__name__)


class IrModuleModule(models.Model):
    _inherit = "ir.module.module"

    def _register_hook(self):
        """Handle SAP import module registration.

        1. Prevent chart template auto-installation after SAP import
        2. Run SAP import if SAP_AUTO_IMPORT is enabled (works on both install and update)
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

        # Run SAP import if SAP_AUTO_IMPORT is enabled
        # This works on both install and update (-u)
        auto_import = os.getenv("SAP_AUTO_IMPORT", "").lower() in ("1", "true")
        if auto_import:
            _logger.info("SAP_AUTO_IMPORT is enabled, running import_all()")
            sap_db = self.env["sap.database"].search([], limit=1)
            if sap_db:
                try:
                    sap_db._import_all()
                    _logger.info("Successfully completed SAP import")
                except Exception as e:
                    _logger.error(f"Error during SAP import: {e}", exc_info=True)
            else:
                _logger.warning("No sap.database record found, skipping auto-import")

        # Proceed with normal hook chain
        return super()._register_hook()
