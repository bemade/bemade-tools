import logging
import re

_logger = logging.getLogger(__name__)


def normalize_country_code(country_code: str):
    """
    Normalize country code from xTuple to Odoo format.

    Args:
        country_code (str): The country code or name from xTuple

    Returns:
        str: The normalized ISO country code (2 letters) or the original value if no mapping found
    """
    if not country_code:
        return False

    # Map common full country names to ISO codes
    country_mapping = {
        "United States": "US",
        "Canada": "CA",
        "Mexico": "MX",
        "United Kingdom": "GB",
        "France": "FR",
        "Germany": "DE",
        "Italy": "IT",
        "Spain": "ES",
        "China": "CN",
        "Japan": "JP",
        "Australia": "AU",
        "Brazil": "BR",
        "India": "IN",
        "Russia": "RU",
        "South Africa": "ZA",
        # Additional countries found in the data
        "Switzerland": "CH",
        "Netherlands": "NL",
        "Belgium": "BE",
        "Sweden": "SE",
        "Norway": "NO",
        "Denmark": "DK",
        "Finland": "FI",
        "Austria": "AT",
        "Portugal": "PT",
        "Greece": "GR",
        "Ireland": "IE",
        "New Zealand": "NZ",
        "Singapore": "SG",
        "Hong Kong": "HK",
        "Taiwan": "TW",
        "South Korea": "KR",
        "Thailand": "TH",
        "Malaysia": "MY",
        "Indonesia": "ID",
        "Philippines": "PH",
        "Vietnam": "VN",
        "Turkey": "TR",
        "Israel": "IL",
        "United Arab Emirates": "AE",
        "Saudi Arabia": "SA",
    }

    # If the country code is already in ISO format (2 letters), return it as is
    if (
        isinstance(country_code, str)
        and len(country_code) == 2
        and country_code.isalpha()
    ):
        return country_code.upper()

    # Try to map the full country name to ISO code
    return country_mapping.get(country_code, country_code)
