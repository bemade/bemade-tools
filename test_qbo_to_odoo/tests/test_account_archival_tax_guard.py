"""Tests for the tax-referenced account archival guard.

Acceptance criteria:
1. QboAccountFinalizer does NOT archive an account.account that is referenced
   by an account.tax.repartition.line (active tax).  A control account (no tax
   reference) in the same to-archive list IS archived — the guard is selective,
   not a blanket skip.
2. Inactive-tax repartition lines still protect the account.  When the parent
   account.tax is archived (active=False), the guard must still exclude the
   referenced account because the search runs with active_test=False.
3. The Odoo-default archival path (QboAccountImporter.transform_accounts, line ~154)
   is also guarded.  Because invoking the full transform in a unit test is
   impractical (it calls the QBO API), this is tested directly via the shared
   helper _filter_tax_referenced — the same function both call sites delegate to.
4. Integration: a customer invoice with a QBO sale tax that references the
   guarded account can be posted without a "The account … is archived." UserError.
   If a full COA fixture is too heavy, this test is skipped with a clear message;
   AC1–AC3 cover the guard logic as fast unit guards.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


@tagged("post_install", "-at_install", "account_archival")
class TestAccountArchivalTaxGuard(TransactionCase):
    """Unit tests for _filter_tax_referenced and its two call sites."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.finalizer = cls.env["qbo.account.finalizer"]

        # Create two test accounts; one will be tax-referenced, one will not.
        cls.tax_ref_account = cls.env["account.account"].create({
            "name": "QBO Tax Payable Test",
            "code": "TEST.TAX.REF.001",
            "account_type": "liability_current",
        })
        cls.control_account = cls.env["account.account"].create({
            "name": "QBO Control Archive Test",
            "code": "TEST.CTRL.001",
            "account_type": "expense",
        })

        # Assign a QBO-style qbo_id so the finalizer can find them
        cls.tax_ref_account.write({"qbo_id": 99901})
        cls.control_account.write({"qbo_id": 99902})

        # Create a sale tax with one tax repartition line pointing at tax_ref_account
        cls.sale_tax = cls.env["account.tax"].create({
            "name": "QBO Sale Tax Guard Test",
            "type_tax_use": "sale",
            "amount": 10.0,
            "amount_type": "percent",
        })
        # Find/update the 'tax' type invoice repartition line to use our account
        invoice_tax_line = cls.sale_tax.invoice_repartition_line_ids.filtered(
            lambda l: l.repartition_type == "tax"
        )
        if invoice_tax_line:
            invoice_tax_line.write({"account_id": cls.tax_ref_account.id})
        else:
            cls.env["account.tax.repartition.line"].create({
                "repartition_type": "tax",
                "account_id": cls.tax_ref_account.id,
                "tax_id": cls.sale_tax.id,
                "document_type": "invoice",
                "factor_percent": 100.0,
            })

    def test_ac1_guard_is_selective_not_blanket(self):
        """AC1: tax-referenced account is kept active; non-referenced control is archived."""
        ctx = _make_ctx(self.env)
        both = self.tax_ref_account | self.control_account

        # Ensure both start active
        self.assertTrue(self.tax_ref_account.active)
        self.assertTrue(self.control_account.active)
        self.assertTrue(self.sale_tax.active)

        # Simulate the finalizer call: run load_archive_accounts with our qbo_ids
        self.finalizer.load_archive_accounts(
            ctx,
            {"transform_inactive_accounts": [99901, 99902]},
        )

        self.tax_ref_account.invalidate_recordset()
        self.control_account.invalidate_recordset()

        self.assertTrue(
            self.tax_ref_account.active,
            "tax-referenced account must NOT be archived",
        )
        self.assertFalse(
            self.control_account.active,
            "non-referenced control account MUST be archived",
        )

    def test_ac2_inactive_tax_still_protects_account(self):
        """AC2: account stays active even when the parent tax is archived (active=False)."""
        ctx = _make_ctx(self.env)

        # Reset state: reactivate both accounts
        self.tax_ref_account.with_context(active_test=False).write({"active": True})
        self.control_account.with_context(active_test=False).write({"active": True})

        # Archive the tax itself — the repartition line reference must still be found
        self.sale_tax.write({"active": False})

        try:
            self.finalizer.load_archive_accounts(
                ctx,
                {"transform_inactive_accounts": [99901, 99902]},
            )

            self.tax_ref_account.invalidate_recordset()
            self.control_account.invalidate_recordset()

            self.assertTrue(
                self.tax_ref_account.with_context(active_test=False).active,
                "account must stay active even when its referencing tax is archived",
            )
            self.assertFalse(
                self.control_account.with_context(active_test=False).active,
                "non-referenced control account must still be archived",
            )
        finally:
            # Restore tax for later tests
            self.sale_tax.with_context(active_test=False).write({"active": True})

    def test_ac3_filter_helper_directly(self):
        """AC3: _filter_tax_referenced correctly excludes the referenced account.

        The Odoo-default archival path (QboAccountImporter.transform_accounts)
        also calls _filter_tax_referenced.  Because that path requires the QBO
        API client (impractical in a unit test), we test the shared helper directly
        — which covers the same logic both call sites delegate to.
        """
        ctx = _make_ctx(self.env)

        # Reset: ensure tax_ref_account is active
        self.tax_ref_account.with_context(active_test=False).write({"active": True})
        self.sale_tax.with_context(active_test=False).write({"active": True})

        # Create a non-referenced account to serve as the "default Odoo account"
        default_like = self.env["account.account"].create({
            "name": "Default Odoo Account (no QBO id)",
            "code": "TEST.DEFAULT.001",
            "account_type": "expense",
        })

        both = self.tax_ref_account | default_like
        result = self.finalizer._filter_tax_referenced(ctx, both)

        # tax_ref_account must be filtered out; default_like must remain
        self.assertNotIn(
            self.tax_ref_account.id,
            result.ids,
            "_filter_tax_referenced must exclude the tax-referenced account",
        )
        self.assertIn(
            default_like.id,
            result.ids,
            "_filter_tax_referenced must keep the non-referenced account",
        )

    def test_ac4_invoice_posts_with_guarded_account(self):
        """AC4: customer invoice using a sale tax with the guarded account can be posted.

        This test verifies end-to-end that keeping the account active (as the guard
        does) allows invoice posting without the 'The account … is archived.' UserError.

        Note: This test requires a working chart of accounts (AR account, revenue
        account, suspense account, etc.).  If the test environment does not have a
        fully-configured COA it will be skipped with a clear message rather than
        failing — AC1–AC3 cover the guard logic as fast unit guards.
        """
        ctx = _make_ctx(self.env)

        # Reset: ensure tax_ref_account is active (as the guard would leave it)
        self.tax_ref_account.with_context(active_test=False).write({"active": True})
        self.sale_tax.with_context(active_test=False).write({"active": True})

        # Find a receivable account for the customer line
        ar_account = self.env["account.account"].search(
            [
                ("account_type", "=", "asset_receivable"),
                ("company_ids", "in", [self.env.company.id]),
            ],
            limit=1,
        )
        # Find a revenue / income account for the product line
        income_account = self.env["account.account"].search(
            [
                ("account_type", "in", ["income", "income_other"]),
                ("company_ids", "in", [self.env.company.id]),
            ],
            limit=1,
        )

        if not ar_account or not income_account:
            self.skipTest(
                "AC4 skipped: no AR or income account available in this test env. "
                "Run against a freshly re-migrated verajet-staging DB for full "
                "integration coverage."
            )

        # Create a minimal customer partner
        partner = self.env["res.partner"].create({
            "name": "AC4 Test Customer",
            "customer_rank": 1,
        })

        move = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "invoice_line_ids": [
                (0, 0, {
                    "name": "AC4 Test Service",
                    "quantity": 1.0,
                    "price_unit": 100.0,
                    "account_id": income_account.id,
                    "tax_ids": [(4, self.sale_tax.id)],
                })
            ],
        })

        try:
            move.action_post()
        except Exception as e:
            self.fail(
                f"AC4: action_post() raised an unexpected exception: {e!r}\n"
                "The tax-referenced account may have been archived."
            )

        self.assertEqual(
            move.state,
            "posted",
            "AC4: invoice must reach 'posted' state without the archived-account error",
        )
