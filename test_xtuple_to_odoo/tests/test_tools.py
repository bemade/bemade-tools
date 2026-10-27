from odoo.tests.common import TransactionCase
from odoo.addons.xtuple_to_odoo.tools import normalize_country_code


class TestTools(TransactionCase):
    def test_normalize_country_code(self):
        """Test the normalize_country_code function"""
        # Test full country names
        self.assertEqual(normalize_country_code("United States"), "US")
        self.assertEqual(normalize_country_code("Canada"), "CA")
        self.assertEqual(normalize_country_code("Mexico"), "MX")
        self.assertEqual(normalize_country_code("United Kingdom"), "GB")

        # Test ISO codes (should remain unchanged)
        self.assertEqual(normalize_country_code("US"), "US")
        self.assertEqual(normalize_country_code("CA"), "CA")
        self.assertEqual(normalize_country_code("mx"), "MX")  # Should be uppercase

        # Test empty or invalid values
        self.assertFalse(normalize_country_code(None))
        self.assertFalse(normalize_country_code(""))
        self.assertEqual(
            normalize_country_code("Unknown Country"), "Unknown Country"
        )  # No mapping

        # Test case sensitivity
        self.assertEqual(
            normalize_country_code("CANADA"), "CANADA"
        )  # No mapping for uppercase
        self.assertEqual(
            normalize_country_code("canada"), "canada"
        )  # No mapping for lowercase
