#
#    Bemade Inc.
#
#    Copyright (C) 2023-June Bemade Inc. (<https://www.bemade.org>).
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
    "name": "SAP Business One to Odoo",
    "version": "19.0.0.0.2",
    "summary": "Convert a database from SAP Business One to Odoo",
    "category": "Technical",
    "author": "Bemade Inc.",
    "website": "http://www.bemade.org",
    "license": "LGPL-3",
    "depends": [
        "base",
        "contacts",
        "sale",
        "stock",
        "purchase",
        "stock_delivery",
        "mrp",
        "purchase_stock",
        "delivery_carrier_partner_account",
        "base_automation",
        "customer_product_code",
        "purchase_requisition",
        "purchase_customer_requisition",
        "purchase_delivery_carrier",
        "picking_policy_per_customer",
        "accountant",
        "stock_account",
        "etl_framework",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/res_config_settings.xml",
        "data/sap_database_setup.xml",
        "data/menus_actions.xml",
        "views/sap_database_views.xml",
        "views/sap_field_views.xml",
        "views/mrp_views.xml",
        "views/sap_migration_report_views.xml",
    ],
    "external_dependencies": {"python": ["fuzzywuzzy", "python-Levenshtein"]},
    "post_init_hook": "post_init_hook",
    "installable": True,
    "auto_install": False,
}
