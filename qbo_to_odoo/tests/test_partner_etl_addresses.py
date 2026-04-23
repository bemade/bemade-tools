"""Tests for QBO partner ETL address backfill and country-code resolution.

Acceptance criteria:
1. QboCustomerLinker backfills all six address fields on an empty partner and sets
   qbo_customer_id.
2. QboCustomerLinker does NOT overwrite address fields already set on the partner;
   only qbo_customer_id (and payment-term if present) are written.
3. QboCustomerImporter (create path) resolves BillAddr.Country="USA" to country_id
   with code=="US", not "UM" or any other wrong match.
4. QboVendorLinker backfills address on an empty partner (analogous to AC1).
5. Re-running the linker on a partner that now has a full address is a no-op on
   address fields (idempotent backfill).
"""

from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


def _stub_qbo_customer(
    qbo_id="1",
    name="Test Corp",
    line1="1 Main St",
    line2=None,
    city="Austin",
    state="TX",
    country="USA",
    postal="78701",
):
    return {
        "Id": qbo_id,
        "DisplayName": name,
        "CompanyName": name,
        "Active": True,
        "BillAddr": {
            "Line1": line1,
            "Line2": line2,
            "City": city,
            "CountrySubDivisionCode": state,
            "Country": country,
            "PostalCode": postal,
        },
    }


def _stub_qbo_vendor(
    qbo_id="10",
    name="Test Vendor LLC",
    line1="99 Supplier Ave",
    line2=None,
    city="Dallas",
    state="TX",
    country="USA",
    postal="75201",
):
    return {
        "Id": qbo_id,
        "DisplayName": name,
        "CompanyName": name,
        "Active": True,
        "BillAddr": {
            "Line1": line1,
            "Line2": line2,
            "City": city,
            "CountrySubDivisionCode": state,
            "Country": country,
            "PostalCode": postal,
        },
    }


@tagged("post_install", "-at_install")
class TestQboCustomerLinkerAddressBackfill(TransactionCase):
    """AC1 & AC2: customer linker backfills empty address, skips populated address."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.us_country = cls.env.ref("base.us")
        cls.tx_state = cls.env["res.country.state"].search(
            [("code", "=", "TX"), ("country_id", "=", cls.us_country.id)], limit=1
        )

    def _run_linker_transform_load(self, partner, qbo_customer):
        """Run the QboCustomerLinker transform+load steps against a single QBO record."""
        linker = self.env["qbo.customer.linker"]
        ctx = _make_ctx(self.env)

        # Simulate extracted data: one customer with _partner_id pre-filled
        qbo_customer["_partner_id"] = partner.id
        qbo_customer["_already_linked"] = False

        extracted = {"extract_customers_for_linking": [qbo_customer]}
        transformed_list = linker.transform_customers_for_linking(ctx, extracted)
        transformed = {"transform_customers_for_linking": transformed_list}
        linker.load_customer_links(ctx, transformed)

    def test_ac1_linker_backfills_empty_address(self):
        """AC1: linker writes all six address fields when partner has none."""
        partner = self.env["res.partner"].create({"name": "ACME Corp", "is_company": True})
        self.assertFalse(partner.street)
        self.assertFalse(partner.country_id)

        customer = _stub_qbo_customer(name="ACME Corp")
        self._run_linker_transform_load(partner, customer)

        partner.invalidate_recordset()
        self.assertEqual(partner.street, "1 Main St")
        self.assertEqual(partner.city, "Austin")
        self.assertEqual(partner.zip, "78701")
        self.assertEqual(partner.country_id.code, "US",
                         "Country code must be US (not UM) when QBO supplies 'USA'")
        self.assertEqual(partner.state_id.code, "TX")
        self.assertEqual(partner.qbo_customer_id, 1)

    def test_ac2_linker_does_not_clobber_existing_address(self):
        """AC2: linker must not overwrite fields that are already set."""
        partner = self.env["res.partner"].create({
            "name": "Existing Corp",
            "is_company": True,
            "street": "Existing St",
            "city": "Existing City",
            "country_id": self.us_country.id,
        })

        customer = _stub_qbo_customer(
            name="Existing Corp",
            line1="Different St",
            city="Different City",
            country="CAN",  # different country
        )
        self._run_linker_transform_load(partner, customer)

        partner.invalidate_recordset()
        self.assertEqual(partner.street, "Existing St",
                         "street must not be overwritten")
        self.assertEqual(partner.city, "Existing City",
                         "city must not be overwritten")
        self.assertEqual(partner.country_id.id, self.us_country.id,
                         "country_id must not be overwritten")
        self.assertEqual(partner.qbo_customer_id, 1,
                         "qbo_customer_id must still be linked")

    def test_ac5_rerun_is_noop_on_address(self):
        """AC5: re-running linker on a fully-addressed partner writes no address keys."""
        partner = self.env["res.partner"].create({
            "name": "Full Addr Corp",
            "is_company": True,
            "street": "123 Real Rd",
            "city": "Houston",
            "zip": "77001",
            "country_id": self.us_country.id,
            "state_id": self.tx_state.id,
        })

        customer = _stub_qbo_customer(name="Full Addr Corp")
        self._run_linker_transform_load(partner, customer)
        partner.invalidate_recordset()

        # Address must be unchanged
        self.assertEqual(partner.street, "123 Real Rd")
        self.assertEqual(partner.city, "Houston")
        self.assertEqual(partner.zip, "77001")
        self.assertEqual(partner.state_id.id, self.tx_state.id)

        # Second run (idempotent)
        self._run_linker_transform_load(partner, customer)
        partner.invalidate_recordset()
        self.assertEqual(partner.street, "123 Real Rd",
                         "Street must not change on second run")


@tagged("post_install", "-at_install")
class TestQboCountryAliasResolution(TransactionCase):
    """AC3: create path resolves 'USA' → country_id.code == 'US'."""

    def test_ac3_usa_resolves_to_us_not_um(self):
        """AC3: _get_country_id('USA') returns the US country, not UM."""
        importer = self.env["qbo.customer.importer"]
        ctx = _make_ctx(self.env)
        us_country = self.env.ref("base.us")

        country_id = importer._get_country_id(ctx, "USA")
        self.assertEqual(country_id, us_country.id,
                         "USA must resolve to United States (code=US), not Minor Outlying Islands (UM)")

    def test_can_resolves_to_ca(self):
        """CAN must resolve to Canada (CA)."""
        importer = self.env["qbo.customer.importer"]
        ctx = _make_ctx(self.env)
        ca_country = self.env.ref("base.ca")

        country_id = importer._get_country_id(ctx, "CAN")
        self.assertEqual(country_id, ca_country.id)

    def test_iso2_passthrough(self):
        """Two-letter ISO codes must pass through unchanged."""
        importer = self.env["qbo.customer.importer"]
        ctx = _make_ctx(self.env)
        us_country = self.env.ref("base.us")

        country_id = importer._get_country_id(ctx, "US")
        self.assertEqual(country_id, us_country.id)

    def test_vendor_importer_also_resolves_usa(self):
        """QboVendorImporter._get_country_id must also resolve 'USA' correctly."""
        importer = self.env["qbo.vendor.importer"]
        ctx = _make_ctx(self.env)
        us_country = self.env.ref("base.us")

        country_id = importer._get_country_id(ctx, "USA")
        self.assertEqual(country_id, us_country.id)


@tagged("post_install", "-at_install")
class TestQboVendorLinkerAddressBackfill(TransactionCase):
    """AC4: vendor linker backfills address on empty partner."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.us_country = cls.env.ref("base.us")

    def _run_vendor_linker(self, partner, qbo_vendor):
        linker = self.env["qbo.vendor.linker"]
        ctx = _make_ctx(self.env)

        # Build the update dict the same way the transform would
        bill_addr = qbo_vendor.get("BillAddr", {}) or {}
        update = {
            "partner_id": partner.id,
            "qbo_vendor_id": int(qbo_vendor["Id"]),
            "name": qbo_vendor.get("DisplayName", ""),
            "addr_street": bill_addr.get("Line1"),
            "addr_street2": bill_addr.get("Line2"),
            "addr_city": bill_addr.get("City"),
            "addr_zip": bill_addr.get("PostalCode"),
            "addr_country_code": bill_addr.get("Country"),
            "addr_state_code": bill_addr.get("CountrySubDivisionCode"),
        }
        transformed = {"transform_vendors_for_linking": [update]}
        linker.load_vendor_links(ctx, transformed)

    def test_ac4_vendor_linker_backfills_empty_address(self):
        """AC4: vendor linker writes address fields to a partner with none."""
        partner = self.env["res.partner"].create(
            {"name": "Test Vendor LLC", "is_company": True}
        )
        self.assertFalse(partner.street)

        vendor = _stub_qbo_vendor(name="Test Vendor LLC")
        self._run_vendor_linker(partner, vendor)

        partner.invalidate_recordset()
        self.assertEqual(partner.street, "99 Supplier Ave")
        self.assertEqual(partner.city, "Dallas")
        self.assertEqual(partner.zip, "75201")
        self.assertEqual(partner.country_id.code, "US")
        self.assertEqual(partner.qbo_vendor_id, 10)
