#
#    Bemade Inc.
#
#    Copyright (C) 2026-July Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for the purchase order header ETL `carrier_id` field-existence guard.

Acceptance criteria (task #3950):

1. (test_transform_headers_vals_are_valid_fields) Direct regression: every key
   in each vals dict produced by `transform_headers` (run against the real
   `purchase.order` model in this test DB) must be a member of
   `env["purchase.order"]._fields`. Fails if an unconditional/invalid key
   (like an unguarded `carrier_id`) is reintroduced.
2. (test_load_headers_creates_purchase_order) The transformed vals load
   cleanly through `load_headers` / `purchase.order.create` with no
   `ValueError`, and the header fields (`partner_id`, `date_order`, `note`,
   `sap_docentry`) come through intact.
3. (test_transform_headers_omits_carrier_id_when_field_absent) Guard branch:
   when `"carrier_id" not in purchase.order._fields` (simulated via a fake
   env/model stub, independent of what's actually installed in this DB),
   `carrier_id` must not appear in the vals dict at all.
4. (test_transform_headers_includes_carrier_id_when_field_present) Guard
   branch: when `"carrier_id" in purchase.order._fields` (simulated the same
   way), the vals dict must include `carrier_id` mapped from
   `cache["carriers_map"]` via the header's `trnspcode`, verbatim.
"""

import datetime
from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.etl_framework import ChunkableData


class _FakeModel:
    """Stand-in for `env["purchase.order"]` exposing only `_fields`.

    `transform_headers` reads exactly one attribute off `ctx.env["purchase.order"]`
    during transform: `._fields`. This stub lets the guard-branch tests force
    that membership check to True/False deterministically, independent of
    whatever modules happen to be installed in the running test DB.
    """

    def __init__(self, fields):
        self._fields = fields


class _FakeEnv(dict):
    """Dict-backed stand-in for `ctx.env` supporting `env["model_name"]`."""


def _make_header(**overrides):
    header = {
        "docnum": 1001,
        "docentry": 5001,
        "atcentry": 9001,
        "cardcode": "C001",
        "cntctcode": None,
        "groupnum": None,
        "docdate": datetime.datetime(2026, 1, 15),
        "docduedate": datetime.datetime(2026, 2, 15),
        "numatcard": "PO-1001",
        "trnspcode": 42,
    }
    header.update(overrides)
    return header


def _make_cache(partner_id, terms_map=None, carriers_map=None, company_id=1):
    return {
        "partners_map": {"C001": partner_id},
        "partner_addresses_map": {},
        "contacts_map": {},
        "terms_map": terms_map or {},
        "carriers_map": carriers_map or {},
        "company_id": company_id,
    }


@tagged("-at_install", "post_install", "purchase_order_etl")
class TestPurchaseOrderHeaderEtl(TransactionCase):
    """Guards the `carrier_id` field-existence check in `transform_headers`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["purchase.order.header.importer"]
        cls.partner = cls.env["res.partner"].create({"name": "Test SAP Vendor"})

    def _run_transform(self, ctx, header, cache):
        extracted = {
            "extract_headers": ChunkableData(records=[header], context=cache)
        }
        return self.importer.transform_headers(ctx, extracted)

    def test_transform_headers_vals_are_valid_fields(self):
        """Every produced vals key must be a real `purchase.order` field."""
        ctx = MagicMock()
        ctx.env = self.env

        header = _make_header()
        cache = _make_cache(self.partner.id)

        vals_list = self._run_transform(ctx, header, cache)
        self.assertEqual(len(vals_list), 1, "One header should produce one vals dict")

        real_fields = set(self.env["purchase.order"]._fields)
        for vals in vals_list:
            self.assertLessEqual(
                set(vals),
                real_fields,
                "Every key in the transformed vals dict must be a valid "
                "purchase.order field (guards against an unconditional/"
                "invalid key such as an unguarded carrier_id).",
            )

    def test_load_headers_creates_purchase_order(self):
        """Transformed vals must load cleanly and carry the header fields."""
        ctx = MagicMock()
        ctx.env = self.env

        header = _make_header(docentry=5002)
        cache = _make_cache(self.partner.id)

        vals_list = self._run_transform(ctx, header, cache)
        self.importer.load_headers(ctx, {"transform_headers": vals_list})
        self.env.flush_all()

        order = self.env["purchase.order"].search([("sap_docentry", "=", 5002)])
        self.assertTrue(order, "load_headers must create a purchase.order")
        self.assertEqual(order.partner_id, self.partner)
        self.assertIn("SAP Order PO-1001", order.note or "")
        self.assertTrue(order.date_order, "date_order must be set")

    def test_transform_headers_omits_carrier_id_when_field_absent(self):
        """When purchase.order has no carrier_id field, the key is dropped."""
        fake_env = _FakeEnv(
            {
                "purchase.order": _FakeModel(
                    {"partner_id", "date_order", "note", "sap_docentry"}
                )
            }
        )
        ctx = MagicMock()
        ctx.env = fake_env

        header = _make_header()
        cache = _make_cache(999, carriers_map={42: 77})

        vals_list = self._run_transform(ctx, header, cache)
        self.assertEqual(len(vals_list), 1)
        self.assertNotIn(
            "carrier_id",
            vals_list[0],
            "carrier_id must be omitted when the field does not exist on "
            "purchase.order.",
        )

    def test_transform_headers_includes_carrier_id_when_field_present(self):
        """When purchase.order has a carrier_id field, it is populated verbatim."""
        fake_env = _FakeEnv(
            {
                "purchase.order": _FakeModel(
                    {"partner_id", "date_order", "note", "sap_docentry", "carrier_id"}
                )
            }
        )
        ctx = MagicMock()
        ctx.env = fake_env

        header = _make_header()
        cache = _make_cache(999, carriers_map={42: 77})

        vals_list = self._run_transform(ctx, header, cache)
        self.assertEqual(len(vals_list), 1)
        self.assertIn(
            "carrier_id",
            vals_list[0],
            "carrier_id must be present when the field exists on purchase.order.",
        )
        self.assertEqual(
            vals_list[0]["carrier_id"],
            77,
            "carrier_id must come from cache['carriers_map'][trnspcode] verbatim.",
        )
