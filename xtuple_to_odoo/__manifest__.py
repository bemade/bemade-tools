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
    "name": "xTuple to Odoo",
    "version": "19.0.0.0.1",
    "summary": "Migrate xTuple data to Odoo",
    "description": """
Migrate xTuple data to Odoo including:

* Users
* Vendors
* Customers
* Products
* Bills of Materials
* Manufacturing Orders
""",
    "category": "Data Migration",
    "author": "Bemade Inc.",
    "website": "http://www.bemade.org",
    "license": "LGPL-3",
    "depends": ["mrp", "base", "contacts", "account", "purchase", "etl_framework"],
    "data": [
        "security/ir.model.access.csv",
        "views/xtuple_database_views.xml",
        "data/xtuple_database_setup.xml",
    ],
    "post_init_hook": "post_init_hook",
    "assets": {},
    "installable": True,
    "auto_install": False,
}
