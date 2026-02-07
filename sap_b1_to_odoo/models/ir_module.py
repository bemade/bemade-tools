# -*- coding: utf-8 -*-
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class IrModuleModule(models.Model):
    _inherit = "ir.module.module"

    def _register_hook(self):
        """Prevent chart template auto-installation after SAP import."""
        company = self.env.company
        if company:
            sap_accounts = self.env["account.account"].search(
                [("company_ids", "in", [company.id]), ("sap_acct_code", "!=", False)],
                limit=1,
            )

            if sap_accounts and not company.chart_template:
                _logger.info(
                    "SAP CoA detected - chart template already configured or will be set by ETL"
                )

        return super()._register_hook()
