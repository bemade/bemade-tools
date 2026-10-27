"""Tests for xTuple partner ETL address backfill on the update path.

Acceptance criteria:
5. xTuple customer update-path backfills address fields when partner has none.
6. xTuple customer update-path does NOT overwrite address fields already present.
7. xTuple vendor linker (update path) backfills address on empty partner.
8. Re-running the pipeline on an already-complete partner is a no-op on address fields.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


def _make_ctx(env):
    """Build a minimal ETLContext backed by the Odoo test env."""
    return ETLContext(cr=None, env=env)


def _make_xtuple_customer(cust_id=1, name="Beta Ltd", **addr_kwargs):
    """Build a minimal xTuple custinfo dict with address fields."""
    defaults = {
        "cust_id": cust_id,
        "cust_number": f"CUST-{cust_id:04d}",
        "cust_name": name,
        "cust_active": True,
        "cust_cntct_id": None,
        "crmacct_id": None,
        "cntct_first_name": None,
        "cntct_last_name": None,
        "cntct_honorific": None,
        "cntct_initials": None,
        "cntct_phone": None,
        "cntct_phone2": None,
        "cntct_fax": None,
        "cntct_email": None,
        "cntct_webaddr": None,
        "cntct_notes": None,
        "cntct_active": True,
        "addr_line1": "42 xTuple Blvd",
        "addr_line2": None,
        "addr_line3": None,
        "addr_city": "Boston",
        "addr_state": "MA",
        "addr_postalcode": "02101",
        "addr_country": "US",
        "addr_notes": None,
        "crmacct_parent_id": None,
    }
    defaults.update(addr_kwargs)
    return defaults


def _make_xtuple_vendor(vend_id=2, name="Gamma Supplies", **addr_kwargs):
    """Build a minimal xTuple vendinfo dict with address fields."""
    defaults = {
        "vend_id": vend_id,
        "vend_number": f"VEND-{vend_id:04d}",
        "vend_name": name,
        "vend_active": True,
        "vend_cntct_id": None,
        "crmacct_id": None,
        "vend_crmacct_id": None,
        "cntct_first_name": None,
        "cntct_last_name": None,
        "cntct_honorific": None,
        "cntct_initials": None,
        "cntct_phone": None,
        "cntct_phone2": None,
        "cntct_fax": None,
        "cntct_email": None,
        "cntct_webaddr": None,
        "cntct_notes": None,
        "cntct_active": True,
        "addr_line1": "7 Vendor Lane",
        "addr_line2": None,
        "addr_line3": None,
        "addr_city": "Chicago",
        "addr_state": "IL",
        "addr_postalcode": "60601",
        "addr_country": "US",
        "addr_notes": None,
        "crmacct_parent_id": None,
    }
    defaults.update(addr_kwargs)
    return defaults


@tagged("-at_install", "xtuple")
class TestXtupleCustomerUpdatePathAddressBackfill(TransactionCase):
    """AC5, AC6, AC8: xTuple customer update-path address backfill."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.us_country = cls.env.ref("base.us")
        cls.ma_state = cls.env["res.country.state"].search(
            [("code", "=", "MA"), ("country_id", "=", cls.us_country.id)], limit=1
        )

    def _run_customer_update(self, partner, xtuple_row):
        """Drive the transform+load update path for a single customer row."""
        importer = self.env["xtuple.partner.customer.importer"]
        ctx = _make_ctx(self.env)

        # The transform path: inject a single row into customers_to_update
        extracted = {
            "extract_new_customers": [],
            "extract_customers_to_update": [xtuple_row],
        }
        transformed_data = importer.transform_customers(ctx, extracted)
        transformed = {"transform_customers": transformed_data}
        importer.load_customers(ctx, transformed)

    def test_ac5_update_path_backfills_empty_address(self):
        """AC5: update path fills all address fields when partner has none."""
        partner = self.env["res.partner"].create(
            {"name": "Beta Ltd", "is_company": True}
        )
        self.assertFalse(partner.street)

        row = _make_xtuple_customer(name="Beta Ltd")
        self._run_customer_update(partner, row)

        partner.invalidate_recordset()
        self.assertEqual(partner.street, "42 xTuple Blvd")
        self.assertEqual(partner.city, "Boston")
        self.assertEqual(partner.zip, "02101")
        self.assertEqual(partner.country_id.id, self.us_country.id)
        self.assertEqual(partner.state_id.id, self.ma_state.id)
        self.assertEqual(partner.xtuple_cust_id, 1)

    def test_ac6_update_path_does_not_clobber_existing_address(self):
        """AC6: update path must not overwrite existing address fields."""
        partner = self.env["res.partner"].create({
            "name": "Delta Inc",
            "is_company": True,
            "street": "Existing Avenue",
            "city": "Existing City",
            "country_id": self.us_country.id,
        })

        row = _make_xtuple_customer(
            name="Delta Inc",
            addr_line1="Different Street",
            addr_city="Different City",
        )
        self._run_customer_update(partner, row)

        partner.invalidate_recordset()
        self.assertEqual(partner.street, "Existing Avenue",
                         "street must not be overwritten")
        self.assertEqual(partner.city, "Existing City",
                         "city must not be overwritten")
        self.assertEqual(partner.country_id.id, self.us_country.id,
                         "country_id must not be overwritten")
        self.assertEqual(partner.xtuple_cust_id, 1,
                         "xtuple_cust_id must still be set")

    def test_ac8_rerun_is_noop_on_address(self):
        """AC8: re-running the pipeline on a complete partner does not modify address."""
        partner = self.env["res.partner"].create({
            "name": "Epsilon LLC",
            "is_company": True,
            "street": "Fixed St",
            "city": "Fixed City",
            "zip": "11111",
            "country_id": self.us_country.id,
            "state_id": self.ma_state.id,
        })

        row = _make_xtuple_customer(name="Epsilon LLC")
        self._run_customer_update(partner, row)
        partner.invalidate_recordset()
        write_date_after_first = partner.write_date

        # Run again — address write_date should not advance further
        self._run_customer_update(partner, row)
        partner.invalidate_recordset()

        self.assertEqual(partner.street, "Fixed St",
                         "street must be unchanged on second run")
        self.assertEqual(partner.city, "Fixed City",
                         "city must be unchanged on second run")


@tagged("-at_install", "xtuple")
class TestXtupleVendorUpdatePathAddressBackfill(TransactionCase):
    """AC7: xTuple vendor update-path address backfill."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.us_country = cls.env.ref("base.us")
        cls.il_state = cls.env["res.country.state"].search(
            [("code", "=", "IL"), ("country_id", "=", cls.us_country.id)], limit=1
        )

    def _run_vendor_update(self, partner, xtuple_row):
        """Drive the transform+load update path for a single vendor row."""
        importer = self.env["xtuple.partner.vendor.importer"]
        ctx = _make_ctx(self.env)

        extracted = {
            "extract_new_vendors": [],
            "extract_vendors_to_update": [xtuple_row],
        }
        transformed_data = importer.transform_vendors(ctx, extracted)
        transformed = {"transform_vendors": transformed_data}
        importer.load_vendors(ctx, transformed)

    def test_ac7_vendor_update_path_backfills_empty_address(self):
        """AC7: vendor update path fills address fields when partner has none."""
        partner = self.env["res.partner"].create(
            {"name": "Gamma Supplies", "is_company": True}
        )
        self.assertFalse(partner.street)

        row = _make_xtuple_vendor(name="Gamma Supplies")
        self._run_vendor_update(partner, row)

        partner.invalidate_recordset()
        self.assertEqual(partner.street, "7 Vendor Lane")
        self.assertEqual(partner.city, "Chicago")
        self.assertEqual(partner.zip, "60601")
        self.assertEqual(partner.country_id.id, self.us_country.id)
        self.assertEqual(partner.state_id.id, self.il_state.id)
        self.assertEqual(partner.xtuple_vend_id, 2)

    def test_vendor_update_does_not_clobber_existing_address(self):
        """Vendor update path must not overwrite existing address fields."""
        partner = self.env["res.partner"].create({
            "name": "Zeta Trading",
            "is_company": True,
            "street": "Old Vendor St",
            "city": "Old City",
            "country_id": self.us_country.id,
        })

        row = _make_xtuple_vendor(
            name="Zeta Trading",
            addr_line1="New Vendor St",
            addr_city="New City",
        )
        self._run_vendor_update(partner, row)

        partner.invalidate_recordset()
        self.assertEqual(partner.street, "Old Vendor St",
                         "street must not be overwritten")
        self.assertEqual(partner.city, "Old City",
                         "city must not be overwritten")
