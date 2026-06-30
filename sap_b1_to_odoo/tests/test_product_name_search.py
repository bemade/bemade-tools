#
#    Bemade Inc.
#
#    Copyright (C) 2026 Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
# Acceptance criteria:
#   1. product.product.name_search accepts 'domain' as a keyword argument (no TypeError).
#   2. product.template.name_search accepts 'domain' as a keyword argument (no TypeError).
#   3. When a domain=[('id','in',[subset])] is passed, returned ids are a subset of that domain.
#   4. A product whose sap_item_code matches the search name is returned.
#   5. name_search with NO domain on product.product still returns the SAP-coded product
#      (default / no-domain code path preserved).

from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestProductNameSearchDomainKwarg(TransactionCase):
    """Tests for the Odoo-19 'domain' keyword argument in sap_b1_to_odoo name_search
    overrides on product.template and product.product.

    Each test creates an isolated set of products so the results are predictable
    even on a database that already has other products installed.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Template 1: the SAP-coded product we expect to find.
        cls.tmpl_sap = cls.env["product.template"].create(
            {
                "name": "SAP Coded Widget",
                "sap_item_code": "TST-SAP-001",
            }
        )
        # Template 2: a control product with no SAP code and a completely
        # different name — it should NOT appear when we filter by domain.
        cls.tmpl_other = cls.env["product.template"].create(
            {
                "name": "Unrelated Control Product",
                "sap_item_code": False,
            }
        )

        # Grab the default product.product variants created for each template.
        cls.prod_sap = cls.tmpl_sap.product_variant_ids[0]
        cls.prod_other = cls.tmpl_other.product_variant_ids[0]

    # ------------------------------------------------------------------
    # product.product tests
    # ------------------------------------------------------------------

    def test_product_product_name_search_domain_kwarg_no_type_error(self):
        """name_search on product.product accepts 'domain' as a keyword arg and returns a list.

        Regression guard for: TypeError: name_search() got an unexpected keyword
        argument 'domain' (Odoo 19 web client passes domain= by keyword when
        opening the Search More dialog on a Many2one field).
        """
        domain = [("id", "in", [self.prod_sap.id, self.prod_other.id])]
        # Must not raise TypeError
        result = self.env["product.product"].name_search(
            name="TST-SAP-001",
            domain=domain,
            operator="ilike",
        )
        self.assertIsInstance(result, list)

    def test_product_product_name_search_domain_restricts_results(self):
        """Returned ids from product.product.name_search are a subset of the supplied domain ids."""
        allowed_ids = [self.prod_sap.id]
        domain = [("id", "in", allowed_ids)]
        result = self.env["product.product"].name_search(
            name="TST-SAP-001",
            domain=domain,
            operator="ilike",
        )
        returned_ids = [r[0] for r in result]
        self.assertTrue(
            all(rid in allowed_ids for rid in returned_ids),
            f"Returned ids {returned_ids} are not all within the supplied domain {allowed_ids}",
        )

    def test_product_product_name_search_sap_code_found_with_domain(self):
        """SAP-coded product.product surfaces when searching by its sap_item_code with a domain."""
        # Domain includes both products so filtering itself is not what finds the SAP one.
        domain = [("id", "in", [self.prod_sap.id, self.prod_other.id])]
        result = self.env["product.product"].name_search(
            name="TST-SAP-001",
            domain=domain,
            operator="ilike",
        )
        returned_ids = [r[0] for r in result]
        self.assertIn(
            self.prod_sap.id,
            returned_ids,
            "The SAP-coded product.product was not returned when searching by its sap_item_code.",
        )

    def test_product_product_name_search_domain_excludes_out_of_domain(self):
        """product.product.name_search respects the domain: out-of-domain products must not appear."""
        # Only allow the 'other' product; the SAP-coded one is excluded by domain.
        domain = [("id", "in", [self.prod_other.id])]
        result = self.env["product.product"].name_search(
            name="TST-SAP-001",
            domain=domain,
            operator="ilike",
        )
        returned_ids = [r[0] for r in result]
        self.assertNotIn(
            self.prod_sap.id,
            returned_ids,
            "prod_sap should not be returned when excluded from the domain.",
        )

    def test_product_product_name_search_no_domain(self):
        """name_search on product.product with NO domain still returns the SAP-coded product."""
        result = self.env["product.product"].name_search(
            name="TST-SAP-001",
            operator="ilike",
        )
        returned_ids = [r[0] for r in result]
        self.assertIn(
            self.prod_sap.id,
            returned_ids,
            "The SAP-coded product.product was not returned when calling name_search without a domain.",
        )

    # ------------------------------------------------------------------
    # product.template tests
    # ------------------------------------------------------------------

    def test_product_template_name_search_domain_kwarg_no_type_error(self):
        """name_search on product.template accepts 'domain' as a keyword arg and returns a list.

        Regression guard for: TypeError: name_search() got an unexpected keyword
        argument 'domain'.
        """
        domain = [("id", "in", [self.tmpl_sap.id, self.tmpl_other.id])]
        result = self.env["product.template"].name_search(
            name="TST-SAP-001",
            domain=domain,
            operator="ilike",
        )
        self.assertIsInstance(result, list)

    def test_product_template_name_search_domain_restricts_results(self):
        """Returned ids from product.template.name_search are a subset of the supplied domain ids."""
        allowed_ids = [self.tmpl_sap.id]
        domain = [("id", "in", allowed_ids)]
        result = self.env["product.template"].name_search(
            name="TST-SAP-001",
            domain=domain,
            operator="ilike",
        )
        returned_ids = [r[0] for r in result]
        self.assertTrue(
            all(rid in allowed_ids for rid in returned_ids),
            f"Returned ids {returned_ids} are not all within the supplied domain {allowed_ids}",
        )

    def test_product_template_name_search_sap_code_found_with_domain(self):
        """SAP-coded product.template surfaces when searching by its sap_item_code with a domain."""
        domain = [("id", "in", [self.tmpl_sap.id, self.tmpl_other.id])]
        result = self.env["product.template"].name_search(
            name="TST-SAP-001",
            domain=domain,
            operator="ilike",
        )
        returned_ids = [r[0] for r in result]
        self.assertIn(
            self.tmpl_sap.id,
            returned_ids,
            "The SAP-coded product.template was not returned when searching by its sap_item_code.",
        )

    def test_product_template_name_search_domain_excludes_out_of_domain(self):
        """product.template.name_search respects the domain: out-of-domain templates must not appear."""
        domain = [("id", "in", [self.tmpl_other.id])]
        result = self.env["product.template"].name_search(
            name="TST-SAP-001",
            domain=domain,
            operator="ilike",
        )
        returned_ids = [r[0] for r in result]
        self.assertNotIn(
            self.tmpl_sap.id,
            returned_ids,
            "tmpl_sap should not be returned when excluded from the domain.",
        )
