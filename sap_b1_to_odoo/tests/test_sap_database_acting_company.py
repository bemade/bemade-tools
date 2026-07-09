#
#    Bemade Inc.
#
#    Copyright (C) 2024-June Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for sap.database._acting_company() — the company pinned on the
execution environment for the whole import.

Acceptance criteria:

1. (test_defaults_to_main_company) When no target company is configured, the
   acting company resolves to the main company — deterministically, i.e. it does
   NOT depend on the running user's own company. This is what makes models with a
   ``company_id`` default resolve correctly whether the import runs from the UI,
   ``odoo-bin shell`` (SUPERUSER), or a cron/hook.
2. (test_uses_configured_company) When a target company IS configured on the
   sap.database record, that company is used.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("-at_install", "post_install", "sap_db_acting_company")
class TestSapDatabaseActingCompany(TransactionCase):
    """Guards the deterministic acting-company resolution used by the importer."""

    def _make_db(self, **overrides):
        vals = {
            "database_host": "sap.example.com",
            "database_name": "sapdb",
            "database_username": "sa",
            "database_port": 5432,
            "database_schema": "dbo",
        }
        vals.update(overrides)
        return self.env["sap.database"].create(vals)

    def test_defaults_to_main_company(self):
        """No configured target → falls back to the main company."""
        main_company = self.env.ref("base.main_company")
        db = self._make_db(company_id=False)
        self.assertEqual(db._acting_company(), main_company)

    def test_uses_configured_company(self):
        """A configured target company is honoured over the fallback."""
        other = self.env["res.company"].create({"name": "SAP Target Co"})
        db = self._make_db(company_id=other.id)
        self.assertEqual(db._acting_company(), other)
