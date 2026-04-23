"""Integration tests for the ITR reconciliation pipeline.

Acceptance criteria (from task design, steps 8-11):

8.  Single group, single account: two posted moves (one DR $100, one CR $100)
    + one synthetic ITR1 group -> one partial.reconcile + one full.reconcile,
    both AMLs reconciled=True, amount_residual=0.

9.  Cross-currency: USD invoice + USD payment (company CAD) at a known rate
    -> one partial, one exchange-diff move, full reconcile links them all.

10. Idempotency / re-run: running load twice on the same group creates zero
    new partials on the second run; log contains "already reconciled".

11. Regression fixture for invoice 453: seed moves for SAP docentries 453,
    pay 343, CM 32 with real SAP amounts; run load; assert two expected
    partials (3658.69 and 2465.24) exist and none touch other invoices.

All tests run in a rolled-back transaction (TransactionCase).
"""

import logging
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.fields import Command

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_journal(env, name, code, jtype="general"):
    journal = env["account.journal"].search(
        [("code", "=", code)], limit=1
    )
    if journal:
        return journal
    return env["account.journal"].create(
        {"name": name, "code": code, "type": jtype}
    )


def _ar_account(env):
    """Return (or create) an AR account for tests."""
    account = env["account.account"].search(
        [("account_type", "=", "asset_receivable")], limit=1
    )
    if account:
        return account
    return env["account.account"].create({
        "name": "Test AR",
        "code": "TESTAR",
        "account_type": "asset_receivable",
        "reconcile": True,
    })


def _make_posted_move(env, journal, ar_account, debit, credit, ref, partner=None):
    """Create and post a journal entry with one AR line and one income line."""
    income_account = env["account.account"].search(
        [("account_type", "=", "income")], limit=1
    )
    if not income_account:
        income_account = env["account.account"].create({
            "name": "Test Income",
            "code": "TESTINC",
            "account_type": "income",
        })

    vals = {
        "journal_id": journal.id,
        "ref": ref,
        "move_type": "entry",
        "line_ids": [
            Command.create({
                "account_id": ar_account.id,
                "debit": debit,
                "credit": credit,
                "name": ref,
                **({"partner_id": partner.id} if partner else {}),
            }),
            Command.create({
                "account_id": income_account.id,
                "debit": credit,   # balancing line
                "credit": debit,
                "name": f"contra {ref}",
                **({"partner_id": partner.id} if partner else {}),
            }),
        ],
    }
    move = env["account.move"].create(vals)
    move.action_post()
    return move


def _make_itr_context(env, groups):
    """Build a fake ETLContext-like object sufficient for the load phase."""
    class FakeCtx:
        def __init__(self, env_):
            self.env = env_

        class _savepoint:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class skippable_cm:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                if exc_type:
                    _logger.warning(
                        "skippable suppressed: %s: %s", exc_type.__name__, exc_val
                    )
                    return True  # suppress
                return False

        def skippable(self, ref=""):
            return FakeCtx.skippable_cm()

    return FakeCtx(env)


# ---------------------------------------------------------------------------
# Test: single group, single account (AC step 8)
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "itr_pipeline")
class TestItrSingleGroup(TransactionCase):
    """Step 8 -- single group, single AR account."""

    def setUp(self):
        super().setUp()
        self.journal = _make_journal(self.env, "Test ITR Journal", "ITRTEST")
        self.ar = _ar_account(self.env)

    def test_single_group_creates_partial_and_full(self):
        # Seed: one DR $100 move, one CR $100 move
        move_d = _make_posted_move(
            self.env, self.journal, self.ar,
            debit=100.0, credit=0.0, ref="DR move"
        )
        move_c = _make_posted_move(
            self.env, self.journal, self.ar,
            debit=0.0, credit=100.0, ref="CR move"
        )

        # Get the AR lines
        aml_d = move_d.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        )
        aml_c = move_c.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        )
        self.assertEqual(len(aml_d), 1)
        self.assertEqual(len(aml_c), 1)

        # Build synthetic group in the format produced by transform phase
        groups = [{
            "reconnum": 9999,
            "members": [
                {
                    "move_id": move_d.id,
                    "lineseq": 0,
                    "reconsum": 100.0,
                    "iscredit": "D",
                    "account": "TESTAR",
                },
                {
                    "move_id": move_c.id,
                    "lineseq": 1,
                    "reconsum": 100.0,
                    "iscredit": "C",
                    "account": "TESTAR",
                },
            ],
        }]

        # Run the load phase directly
        from odoo.addons.sap_b1_to_odoo.models.pipelines.account_internal_reconciliation_etl import (  # noqa: E501
            AccountInternalReconciliation,
        )
        ctx = _make_itr_context(self.env, groups)

        model_instance = self.env["account.internal.reconciliation"]
        model_instance.load_internal_reconciliations(
            ctx, {"transform_internal_reconciliations": groups}
        )

        # Check partial created
        aml_d.invalidate_recordset()
        aml_c.invalidate_recordset()
        partial = self.env["account.partial.reconcile"].search([
            ("debit_move_id", "=", aml_d.id),
            ("credit_move_id", "=", aml_c.id),
        ])
        self.assertEqual(len(partial), 1, "Expected exactly one partial")
        self.assertAlmostEqual(
            partial.amount, 100.0, places=2,
            msg="Partial amount should be $100",
        )

        # Check full reconcile created
        full = self.env["account.full.reconcile"].search([
            ("reconciled_line_ids", "in", aml_d.ids),
        ])
        self.assertEqual(len(full), 1, "Expected exactly one full reconcile")

        # AMLs should be fully reconciled
        self.assertTrue(aml_d.reconciled, "Debit AML should be reconciled")
        self.assertTrue(aml_c.reconciled, "Credit AML should be reconciled")
        self.assertAlmostEqual(aml_d.amount_residual, 0.0, places=2)
        self.assertAlmostEqual(aml_c.amount_residual, 0.0, places=2)


# ---------------------------------------------------------------------------
# Test: idempotency / re-run (AC step 10)
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "itr_pipeline")
class TestItrIdempotency(TransactionCase):
    """Step 10 -- running load twice creates no duplicate partials."""

    def setUp(self):
        super().setUp()
        self.journal = _make_journal(self.env, "Test ITR Journal", "ITRTEST2")
        self.ar = _ar_account(self.env)

    def test_second_run_skips_group(self):
        move_d = _make_posted_move(
            self.env, self.journal, self.ar,
            debit=200.0, credit=0.0, ref="DR idem"
        )
        move_c = _make_posted_move(
            self.env, self.journal, self.ar,
            debit=0.0, credit=200.0, ref="CR idem"
        )

        groups = [{
            "reconnum": 8888,
            "members": [
                {
                    "move_id": move_d.id,
                    "lineseq": 0,
                    "reconsum": 200.0,
                    "iscredit": "D",
                    "account": "TESTAR",
                },
                {
                    "move_id": move_c.id,
                    "lineseq": 1,
                    "reconsum": 200.0,
                    "iscredit": "C",
                    "account": "TESTAR",
                },
            ],
        }]

        ctx = _make_itr_context(self.env, groups)
        model_instance = self.env["account.internal.reconciliation"]

        # First run
        model_instance.load_internal_reconciliations(
            ctx, {"transform_internal_reconciliations": groups}
        )

        partials_after_first = self.env["account.partial.reconcile"].search_count([
            ("debit_move_id", "in", move_d.line_ids.ids),
        ])
        self.assertGreater(partials_after_first, 0)

        # Second run
        with self.assertLogs(
            "odoo.addons.sap_b1_to_odoo.models.pipelines"
            ".account_internal_reconciliation_etl",
            level=logging.DEBUG,
        ) as cm:
            model_instance.load_internal_reconciliations(
                ctx, {"transform_internal_reconciliations": groups}
            )

        partials_after_second = self.env["account.partial.reconcile"].search_count([
            ("debit_move_id", "in", move_d.line_ids.ids),
        ])
        self.assertEqual(
            partials_after_first, partials_after_second,
            "Second run must not create additional partials",
        )

        # Check for skip log message
        skip_msgs = [
            m for m in cm.output
            if "already reconciled" in m and "8888" in m
        ]
        self.assertTrue(
            skip_msgs,
            "Expected 'already reconciled' log for reconnum 8888",
        )


# ---------------------------------------------------------------------------
# Test: invoice 453 regression fixture (AC step 11)
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "itr_pipeline")
class TestItrInvoice453(TransactionCase):
    """Step 11 -- regression fixture for invoice 453.

    Seeds moves for SAP docentries 453, pay 343, CM 32 with the real SAP
    amounts and verifies the two expected partials exist.
    """

    def setUp(self):
        super().setUp()
        self.journal = _make_journal(self.env, "Test ITR Journal", "ITRTEST3")
        self.ar = _ar_account(self.env)

    def test_inv453_two_partials(self):
        # Seed three moves with the SAP amounts
        move_inv453 = _make_posted_move(
            self.env, self.journal, self.ar,
            debit=6123.93, credit=0.0, ref="inv453"
        )
        move_pay343 = _make_posted_move(
            self.env, self.journal, self.ar,
            debit=0.0, credit=3658.69, ref="pay343"
        )
        move_cm32 = _make_posted_move(
            self.env, self.journal, self.ar,
            debit=0.0, credit=2465.24, ref="cm32"
        )

        # SAP lineseq order: pay343 (seq=2) < cm32 (seq=17) < inv453 (seq=21)
        groups = [{
            "reconnum": 453,
            "members": [
                {
                    "move_id": move_inv453.id,
                    "lineseq": 21,
                    "reconsum": 6123.93,
                    "iscredit": "D",
                    "account": "TESTAR",
                },
                {
                    "move_id": move_pay343.id,
                    "lineseq": 2,
                    "reconsum": 3658.69,
                    "iscredit": "C",
                    "account": "TESTAR",
                },
                {
                    "move_id": move_cm32.id,
                    "lineseq": 17,
                    "reconsum": 2465.24,
                    "iscredit": "C",
                    "account": "TESTAR",
                },
            ],
        }]

        ctx = _make_itr_context(self.env, groups)
        model_instance = self.env["account.internal.reconciliation"]
        model_instance.load_internal_reconciliations(
            ctx, {"transform_internal_reconciliations": groups}
        )

        aml_inv = move_inv453.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        )
        aml_pay = move_pay343.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        )
        aml_cm = move_cm32.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        )

        # Two partials off inv453
        partials = self.env["account.partial.reconcile"].search([
            ("debit_move_id", "=", aml_inv.id),
        ])
        self.assertEqual(len(partials), 2, "Expected exactly 2 partials off inv453")

        amounts = sorted(partials.mapped("amount"))
        self.assertAlmostEqual(amounts[0], 2465.24, places=2)
        self.assertAlmostEqual(amounts[1], 3658.69, places=2)

        # pay343 partial
        partial_pay = partials.filtered(
            lambda p: p.credit_move_id.id == aml_pay.id
        )
        self.assertEqual(len(partial_pay), 1)
        self.assertAlmostEqual(partial_pay.amount, 3658.69, places=2)

        # cm32 partial
        partial_cm = partials.filtered(
            lambda p: p.credit_move_id.id == aml_cm.id
        )
        self.assertEqual(len(partial_cm), 1)
        self.assertAlmostEqual(partial_cm.amount, 2465.24, places=2)

        # Invoice is fully reconciled
        aml_inv.invalidate_recordset()
        self.assertAlmostEqual(aml_inv.amount_residual, 0.0, places=2)
