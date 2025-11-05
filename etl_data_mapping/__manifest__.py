#
#    Bemade Inc.
#
#    Copyright (C) 2025-January Bemade Inc. (<https://www.bemade.org>).
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
    "name": "ETL Data Mapping",
    "version": "18.0.1.0.0",
    "summary": "Define and document ETL data mappings between source and target systems",
    "description": """
        ETL Data Mapping Framework
        ==========================
        
        This module provides a framework for defining, documenting, and managing
        data mappings between source systems (e.g., SAP B1, legacy databases) and
        Odoo models.
        
        Features:
        ---------
        * Define source tables and their target Odoo models
        * Map individual fields with transformation rules
        * Document mapping status (mapped, missing, partial, etc.)
        * Track gaps and migration requirements
        * Support for custom transformations and validation rules
        * Export mappings for documentation and review
        * Future: Generate ETL pipelines from mapping definitions
        
        Use Cases:
        ----------
        * Document existing migration scripts
        * Plan new data migration projects
        * Gap analysis between source and target systems
        * Generate migration code from declarative mappings
    """,
    "category": "Technical",
    "author": "Bemade Inc.",
    "website": "https://www.bemade.org",
    "license": "LGPL-3",
    "depends": [
        "base",
    ],
    "data": [],
    "installable": True,
    "auto_install": False,
    "application": False,
}
