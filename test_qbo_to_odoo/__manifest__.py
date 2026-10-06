{
    "name": "QuickBooks Online to Odoo Tests",
    "version": "19.0.1.0.0",
    "category": "Hidden/Tests",
    "summary": "Generic test suite for qbo_to_odoo",
    "description": """
QuickBooks Online to Odoo Tests
===============================

Test-only module, in the shape of Odoo's own ``test_mail``: it ships no
business logic, only the generic test suite for ``qbo_to_odoo``.

Why the tests live here
-----------------------
``qbo_to_odoo`` is designed to be extended by client-specific modules that
deliberately change its behaviour. Its generic tests assert the generic
behaviour, so when they live inside ``qbo_to_odoo`` they run in every client
project that uses it, against the client's overrides, and fail by design.

Keeping them in a separate module means a client project vendors
``qbo_to_odoo`` without this module, and its own override module tests the
behaviour that project actually ships. Install this module only in a
database used to test ``qbo_to_odoo`` itself.
    """,
    "author": "Bemade Inc.",
    "maintainers": ["mdurepos"],
    "website": "https://www.bemade.org",
    "license": "LGPL-3",
    "depends": ["qbo_to_odoo"],
    "data": [],
    "installable": True,
    "auto_install": False,
}
