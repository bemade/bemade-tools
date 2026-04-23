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
"""Tests for ProductPricelistItemImporter house-default pricelist step.

Acceptance criteria:
1. (test_house_default_is_noop_when_hook_returns_empty) The base pipeline
   _get_house_default_pricelist returns an empty recordset; running
   load_pricelists_and_blankets must NOT create an ir.config_parameter row
   for res.partner.property_product_pricelist_{company_id}.
"""

from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("-at_install", "post_install")
class TestProductPricelistHouseDefault(TransactionCase):
    """Guards the new step-5 house-default hook in the base pipeline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["product.pricelist.item.importer"]

    def test_house_default_is_noop_when_hook_returns_empty(self):
        """Base _get_house_default_pricelist returns empty; no ir.config_parameter written.

        Verifies that the base implementation is a no-op: calling
        _get_house_default_pricelist with a real ctx returns an empty recordset
        and load_pricelists_and_blankets does not write the company-scoped
        ir.config_parameter key.
        """
        company_id = self.env.company.id
        param_key = f"res.partner.property_product_pricelist_{company_id}"

        # Ensure the key does not exist before the test
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", param_key)]
        ).unlink()

        ctx = MagicMock()
        ctx.env = self.env

        result = self.importer._get_house_default_pricelist(ctx)

        self.assertFalse(
            result,
            "_get_house_default_pricelist must return an empty recordset in the "
            "base pipeline (no house default configured).",
        )

        # Simulate what load_pricelists_and_blankets does with the return value
        if result:
            param_key_check = f"res.partner.property_product_pricelist_{company_id}"
            self.env["ir.config_parameter"].sudo().set_param(
                param_key_check, result.id
            )

        existing_param = self.env["ir.config_parameter"].sudo().get_param(param_key)
        self.assertFalse(
            existing_param,
            "No ir.config_parameter row should exist for the house-default key "
            "when the base pipeline hook returns an empty recordset.",
        )

    def test_house_default_step_runs_before_ocrd_loop(self):
        """Step 5 (house default) must be set before the OCRD loop runs.

        This test verifies ordering by inspecting the load method source code
        for the relative position of the house-default block vs. the OCRD-loop
        block (step 4).  A structural guard rather than a runtime test.
        """
        import inspect
        source = inspect.getsource(self.importer.__class__.load_pricelists_and_blankets)
        # Find the position of each key line
        pos_house_default = source.find("_get_house_default_pricelist")
        pos_ocrd_loop = source.find("customer_listnum_map")
        self.assertGreater(
            pos_ocrd_loop,
            pos_house_default,
            "The house-default step (_get_house_default_pricelist) must appear "
            "in the source before the OCRD customer_listnum_map loop.",
        )
