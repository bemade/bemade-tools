"""Regression coverage for #4091: QBO vendor currency mapping.

The QboVendorImporter (create path) and QboVendorLinker (name-match update
path) already map QBO's ``CurrencyRef`` onto
``res.partner.property_purchase_currency_id``. This module locks that
existing, previously-untested behavior so it can't silently regress while
the sibling xTuple pipeline is given the same mapping (see
xtuple_to_odoo/tests/test_partner_import.py::TestVendorCurrencyMapping).

Acceptance criteria:
1. QboVendorImporter.transform_vendors sets property_purchase_currency_id
   to the matching res.currency when the vendor carries a CurrencyRef.
2. A vendor without CurrencyRef omits the key entirely (no forced write).
3. QboVendorLinker.transform_vendors_for_linking + load_vendor_links applies
   the same mapping on the existing-partner (backfill) path.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


def _stub_qbo_vendor(
    qbo_id="20",
    name="Currency Test Vendor",
    currency_code=None,
):
    vendor = {
        "Id": qbo_id,
        "DisplayName": name,
        "CompanyName": name,
        "Active": True,
        "BillAddr": {},
    }
    if currency_code:
        vendor["CurrencyRef"] = {"value": currency_code}
    return vendor


@tagged("post_install", "-at_install", "partner_currency_etl")
class TestQboVendorImporterCurrency(TransactionCase):
    """AC1 & AC2: QboVendorImporter create path maps CurrencyRef."""

    def test_ac1_currency_ref_maps_to_property_purchase_currency(self):
        usd = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", "USD")], limit=1
        )
        self.assertTrue(usd, "USD currency must exist for this test")
        # The production QboVendorImporter/QboVendorLinker currency lookups do
        # NOT pass active_test=False (unlike the xTuple side), so the target
        # currency must be genuinely active for the mapping to take effect --
        # ensure that explicitly rather than depending on which currencies
        # happen to be active-by-default in the test company's chart.
        if not usd.active:
            usd.active = True

        importer = self.env["qbo.vendor.importer"]
        ctx = _make_ctx(self.env)
        vendor = _stub_qbo_vendor(currency_code="USD")

        partner_vals = importer.transform_vendors(
            ctx, {"extract_vendors": [vendor]}
        )

        self.assertEqual(len(partner_vals), 1)
        self.assertEqual(
            partner_vals[0]["property_purchase_currency_id"], usd.id
        )

    def test_ac2_vendor_without_currency_ref_omits_key(self):
        importer = self.env["qbo.vendor.importer"]
        ctx = _make_ctx(self.env)
        vendor = _stub_qbo_vendor(currency_code=None)

        partner_vals = importer.transform_vendors(
            ctx, {"extract_vendors": [vendor]}
        )

        self.assertEqual(len(partner_vals), 1)
        self.assertNotIn("property_purchase_currency_id", partner_vals[0])


@tagged("post_install", "-at_install", "partner_currency_etl")
class TestQboVendorLinkerCurrency(TransactionCase):
    """AC3: QboVendorLinker (existing-partner/backfill path) maps CurrencyRef."""

    def _run_vendor_linker(self, partner, qbo_vendor):
        linker = self.env["qbo.vendor.linker"]
        ctx = _make_ctx(self.env)

        extracted = {"extract_vendors_for_linking": [qbo_vendor]}
        # transform_vendors_for_linking builds its own partner-by-name map
        # (partners with qbo_vendor_id IS NULL) from ctx.env.cr, so the
        # caller only needs to ensure `partner`'s name matches `qbo_vendor`.
        transformed_list = linker.transform_vendors_for_linking(ctx, extracted)
        transformed = {"transform_vendors_for_linking": transformed_list}
        linker.load_vendor_links(ctx, transformed)

    def test_ac3_linker_backfills_currency_on_empty_partner(self):
        cad = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", "CAD")], limit=1
        )
        self.assertTrue(cad, "CAD currency must exist for this test")
        # See note in test_ac1: the production lookup requires the currency
        # to be active (no active_test=False there), and CAD is inactive by
        # default in the demo chart of accounts used by this test company.
        if not cad.active:
            cad.active = True

        partner = self.env["res.partner"].create(
            {"name": "Linker Currency Vendor", "is_company": True}
        )
        self.assertFalse(partner.property_purchase_currency_id)

        vendor = _stub_qbo_vendor(name="Linker Currency Vendor", currency_code="CAD")
        self._run_vendor_linker(partner, vendor)

        partner.invalidate_recordset()
        self.assertEqual(partner.property_purchase_currency_id.id, cad.id)
        self.assertEqual(partner.qbo_vendor_id, 20)
