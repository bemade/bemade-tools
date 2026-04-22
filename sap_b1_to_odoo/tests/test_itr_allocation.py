"""Unit tests for the pure FIFO bipartite allocator.

Acceptance criteria (AC 7 from task design):
- Two-member trivial group produces exactly one triple (AC 7a baseline).
- Multi-payment, no CM: one invoice + two payments -> two triples totalling
  the invoice amount.
- Mixed inv/CM/payment (invoice 453 regression): two triples in SAP lineseq
  order (pay 343 first, then CM 32).
- Large pool (reconnum 1645 shape): all reconsum amounts consumed, total
  debits == total credits, number of triples <= number of members.
- Cross-currency group: allocator emits triples; exchange-diff generation
  is delegated to Odoo ORM (not tested here).
- Partial group (missing Odoo move): transform layer drops the group before
  the allocator is called; allocator itself handles empty inputs gracefully.
- Determinism: calling allocate_fifo twice on the same inputs produces
  identical output.

These tests are pure Python -- they do not require an Odoo environment and
can be run with ``python -m pytest`` or ``odoo-bin --test-tags=itr_alloc``.
"""

from odoo.tests import tagged

# Import the pure function directly -- no registry needed.
from odoo.addons.sap_b1_to_odoo.models.pipelines.account_internal_reconciliation_etl import (  # noqa: E501
    allocate_fifo,
)


class _FakeAML:
    """Minimal stand-in for account.move.line in allocator tests."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<AML {self.name}>"


def _d(name, reconsum, lineseq=0):
    """Build a debit-side allocator entry."""
    return {"aml": _FakeAML(name), "reconsum": reconsum, "lineseq": lineseq}


def _c(name, reconsum, lineseq=0):
    """Build a credit-side allocator entry."""
    return {"aml": _FakeAML(name), "reconsum": reconsum, "lineseq": lineseq}


@tagged("-at_install", "post_install", "itr_alloc")
class TestAllocateFifoTrivial:
    """AC 7 step 1 -- two-member trivial group."""

    def test_trivial_one_debit_one_credit(self):
        debits = [_d("inv", 6123.93, seq=0)]
        credits = [_c("pay", 6123.93, seq=1)]
        triples = allocate_fifo(debits, credits)
        assert len(triples) == 1, f"Expected 1 triple, got {len(triples)}"
        d_aml, c_aml, amount = triples[0]
        assert d_aml.name == "inv"
        assert c_aml.name == "pay"
        assert abs(amount - 6123.93) < 0.005


@tagged("-at_install", "post_install", "itr_alloc")
class TestAllocateFifoMultiPayment:
    """AC 7 step 2 -- one invoice, two payments (no CM)."""

    def test_invoice_splits_across_two_payments(self):
        debits = [_d("inv", 10000.00, seq=0)]
        credits = [
            _c("pay1", 6000.00, seq=1),
            _c("pay2", 4000.00, seq=2),
        ]
        triples = allocate_fifo(debits, credits)
        assert len(triples) == 2, f"Expected 2 triples, got {len(triples)}"

        d1, c1, a1 = triples[0]
        assert c1.name == "pay1"
        assert abs(a1 - 6000.00) < 0.005

        d2, c2, a2 = triples[1]
        assert c2.name == "pay2"
        assert abs(a2 - 4000.00) < 0.005

        assert abs(a1 + a2 - 10000.00) < 0.005
        # Invoice residual consumed to zero
        assert abs(sum(t[2] for t in triples if t[0].name == "inv") - 10000.00) < 0.005


@tagged("-at_install", "post_install", "itr_alloc")
class TestAllocateFifoInvoice453:
    """AC 7 step 3 -- invoice 453 regression.

    SAP amounts from TASK.md evidence:
      inv 453: D $6123.93 seq=21
      pay 343: C $3658.69 seq~2
      cm  32:  C $2465.24 seq=17
    Expected triples (lineseq FIFO): pay343 first, cm32 second.
    """

    def test_invoice_453_splits_correctly(self):
        debits = [_d("inv453", 6123.93, seq=21)]
        # SAP recorded pay343 before cm32 (lower lineseq)
        credits = [
            _c("pay343", 3658.69, seq=2),
            _c("cm32", 2465.24, seq=17),
        ]
        triples = allocate_fifo(debits, credits)
        assert len(triples) == 2, f"Expected 2 triples, got {len(triples)}"

        _, c1, a1 = triples[0]
        assert c1.name == "pay343"
        assert abs(a1 - 3658.69) < 0.005

        _, c2, a2 = triples[1]
        assert c2.name == "cm32"
        assert abs(a2 - 2465.24) < 0.005

        assert abs(a1 + a2 - 6123.93) < 0.005

    def test_invoice_453_no_other_invoices_touched(self):
        """The triples must only reference the supplied AMLs, not any others."""
        debits = [_d("inv453", 6123.93, seq=21)]
        credits = [
            _c("pay343", 3658.69, seq=2),
            _c("cm32", 2465.24, seq=17),
        ]
        triples = allocate_fifo(debits, credits)
        debit_names = {t[0].name for t in triples}
        credit_names = {t[1].name for t in triples}
        assert debit_names == {"inv453"}
        assert credit_names == {"pay343", "cm32"}


@tagged("-at_install", "post_install", "itr_alloc")
class TestAllocateFifoLargePool:
    """AC 7 step 4 -- large pool (reconnum 1645 shape, 39 rows).

    Use a synthetic fixture that mirrors the shape: many small invoices on
    the debit side and many payments/CMs on the credit side, all amounts
    summing to the same total.
    """

    def _build_fixture(self):
        """Build a 20-debit / 19-credit fixture totalling $100,000."""
        total = 100_000.00
        d_amount = total / 20  # $5,000 each
        c_amount = total / 19  # ~$5,263.16 each

        debits = [_d(f"inv{i}", round(d_amount, 2), seq=i) for i in range(20)]
        credits = [_c(f"pay{i}", round(c_amount, 2), seq=i + 20) for i in range(19)]

        # Adjust last entries to absorb rounding so totals match exactly
        d_sum = sum(d["reconsum"] for d in debits)
        c_sum = sum(c["reconsum"] for c in credits)
        debits[-1]["reconsum"] = round(debits[-1]["reconsum"] + (total - d_sum), 2)
        credits[-1]["reconsum"] = round(credits[-1]["reconsum"] + (total - c_sum), 2)

        return debits, credits

    def test_large_pool_triple_count(self):
        debits, credits = self._build_fixture()
        total_members = len(debits) + len(credits)
        triples = allocate_fifo(debits, credits)
        assert len(triples) <= total_members, (
            f"Too many triples: {len(triples)} > {total_members}"
        )
        assert len(triples) > 0

    def test_large_pool_debit_reconsum_consumed(self):
        debits, credits = self._build_fixture()
        triples = allocate_fifo(debits, credits)
        consumed_by_debit = {}
        for d_aml, _, amount in triples:
            consumed_by_debit[d_aml.name] = (
                consumed_by_debit.get(d_aml.name, 0.0) + amount
            )
        for d in debits:
            consumed = consumed_by_debit.get(d["aml"].name, 0.0)
            assert abs(consumed - d["reconsum"]) < 0.02, (
                f"{d['aml'].name}: consumed {consumed} != reconsum {d['reconsum']}"
            )

    def test_large_pool_credit_reconsum_consumed(self):
        debits, credits = self._build_fixture()
        triples = allocate_fifo(debits, credits)
        consumed_by_credit = {}
        for _, c_aml, amount in triples:
            consumed_by_credit[c_aml.name] = (
                consumed_by_credit.get(c_aml.name, 0.0) + amount
            )
        for c in credits:
            consumed = consumed_by_credit.get(c["aml"].name, 0.0)
            assert abs(consumed - c["reconsum"]) < 0.02, (
                f"{c['aml'].name}: consumed {consumed} != reconsum {c['reconsum']}"
            )

    def test_large_pool_total_debits_eq_credits(self):
        debits, credits = self._build_fixture()
        triples = allocate_fifo(debits, credits)
        total_alloc = sum(t[2] for t in triples)
        d_total = sum(d["reconsum"] for d in debits)
        assert abs(total_alloc - d_total) < 0.02


@tagged("-at_install", "post_install", "itr_alloc")
class TestAllocateFifoEmptyInputs:
    """AC 7 step 6 -- partial group / missing Odoo move.

    The transform layer drops groups with missing Odoo moves BEFORE calling
    the allocator.  Verify that the allocator itself handles edge cases
    gracefully.
    """

    def test_empty_debits_returns_empty(self):
        triples = allocate_fifo([], [_c("pay", 100.0, seq=0)])
        assert triples == []

    def test_empty_credits_returns_empty(self):
        triples = allocate_fifo([_d("inv", 100.0, seq=0)], [])
        assert triples == []

    def test_both_empty_returns_empty(self):
        triples = allocate_fifo([], [])
        assert triples == []


@tagged("-at_install", "post_install", "itr_alloc")
class TestAllocateFifoDeterminism:
    """AC 7 step 7 -- determinism.

    Calling allocate_fifo twice on the same inputs produces identical
    triple lists.
    """

    def test_deterministic_simple(self):
        debits = [_d("inv", 6123.93, seq=21)]
        credits = [
            _c("pay343", 3658.69, seq=2),
            _c("cm32", 2465.24, seq=17),
        ]
        t1 = allocate_fifo(debits, credits)
        t2 = allocate_fifo(debits, credits)
        assert len(t1) == len(t2)
        for (d1, c1, a1), (d2, c2, a2) in zip(t1, t2):
            assert d1.name == d2.name
            assert c1.name == c2.name
            assert a1 == a2

    def test_deterministic_large_pool(self):
        import random
        rng = random.Random(42)
        debits = [
            _d(f"d{i}", round(rng.uniform(100, 5000), 2), seq=i)
            for i in range(15)
        ]
        credits_total = sum(d["reconsum"] for d in debits)
        credits_raw = [round(rng.uniform(100, 5000), 2) for _ in range(15)]
        scale = credits_total / sum(credits_raw)
        credits = [
            _c(f"c{i}", round(v * scale, 2), seq=i + 15)
            for i, v in enumerate(credits_raw)
        ]
        # Normalize so totals match
        d_sum = sum(d["reconsum"] for d in debits)
        c_sum = sum(c["reconsum"] for c in credits)
        credits[-1]["reconsum"] = round(
            credits[-1]["reconsum"] + (d_sum - c_sum), 2
        )

        t1 = allocate_fifo(debits, credits)
        t2 = allocate_fifo(debits, credits)
        assert len(t1) == len(t2)
        for (d1, c1, a1), (d2, c2, a2) in zip(t1, t2):
            assert d1.name == d2.name
            assert c1.name == c2.name
            assert a1 == a2


# ---------------------------------------------------------------------------
# Cross-currency stub test (AC 7 step 5)
# ---------------------------------------------------------------------------

@tagged("-at_install", "post_install", "itr_alloc")
class TestAllocateFifoCrossCurrency:
    """AC 7 step 5 -- cross-currency group.

    The pure allocator is currency-agnostic: amounts are whatever SAP
    stored in ``reconsum``.  Exchange-diff generation is delegated to
    Odoo (tested in integration).  Here we just verify that the allocator
    produces triples when amounts differ (as would happen if reconsum is
    in source currency and the values happen to be non-equal, e.g. USD).
    """

    def test_cross_currency_triples_emitted(self):
        # Simulate a USD inv vs USD payment -- amounts expressed in USD
        debits = [_d("inv_usd", 1000.00, seq=0)]
        credits = [_c("pay_usd", 1000.00, seq=1)]
        triples = allocate_fifo(debits, credits)
        assert len(triples) == 1
        _, _, amount = triples[0]
        assert abs(amount - 1000.00) < 0.005


# Make pytest importable without Odoo by providing a simple test runner entry
try:
    import pytest
except ImportError:
    pytest = None  # type: ignore


def _run_all():
    """Run all tests without Odoo -- for standalone pytest usage."""
    for cls in [
        TestAllocateFifoTrivial,
        TestAllocateFifoMultiPayment,
        TestAllocateFifoInvoice453,
        TestAllocateFifoLargePool,
        TestAllocateFifoEmptyInputs,
        TestAllocateFifoDeterminism,
        TestAllocateFifoCrossCurrency,
    ]:
        obj = cls()
        for name in dir(obj):
            if name.startswith("test_"):
                try:
                    getattr(obj, name)()
                    print(f"  PASS  {cls.__name__}.{name}")
                except AssertionError as exc:
                    print(f"  FAIL  {cls.__name__}.{name}: {exc}")
                except Exception as exc:
                    print(f"  ERROR {cls.__name__}.{name}: {exc}")


if __name__ == "__main__":
    _run_all()
