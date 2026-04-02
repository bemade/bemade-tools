#
#    Bemade Inc.
#
#    Copyright (C) 2023-January Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
#    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
#    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
#    ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#    DEALINGS IN THE SOFTWARE.
#
{
    "name": "QuickBooks Online to Odoo",
    "version": "19.0.0.0.1",
    "summary": "Migrate QuickBooks Online data to Odoo using ETL Framework",
    "description": """
Migrate QuickBooks Online data to Odoo including:

* Chart of Accounts
* Customers
* Vendors
* Products/Services
* Invoices
* Bills
* Payments
* Journal Entries

Features:

* OAuth2 authentication with QBO API
* Rate limiting to respect QBO API limits
* Incremental imports based on last sync date
* ETL framework integration for reliable data migration
""",
    "category": "Data Migration",
    "author": "Bemade Inc.",
    "website": "http://www.bemade.org",
    "license": "LGPL-3",
    "depends": [
        "base",
        "contacts",
        "account",
        "product",
        "stock",
        "sale",
        "purchase",
        "hr",
        "hr_expense",
        "etl_framework",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/qbo_connection_setup.xml",
        "views/qbo_oauth_templates.xml",
        "views/qbo_connection_views.xml",
        "views/qbo_migration_report_views.xml",
    ],
    "external_dependencies": {
        "python": ["requests", "requests_oauthlib"],
    },
    "assets": {},
    "installable": True,
    "auto_install": False,
}
