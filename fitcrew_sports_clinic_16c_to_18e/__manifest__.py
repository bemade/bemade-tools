#
#    Bemade Inc.
#
#    Copyright (C) 2025 Bemade Inc. (<https://www.bemade.org>).
#    Author: Denis Durepos (Contact : d@bemade.org)
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
    "name": "FitCrew Sports Clinic 16C to 18E Migration",
    "version": "18.0.1.0.0",
    "summary": "Migrate FitCrew Sports Clinic data from Odoo 16 Community to Odoo 18 Enterprise",
    "description": """
    Migrate FitCrew Sports Clinic data from Odoo 16 Community to Odoo 18 Enterprise including:

    **Core Sports Clinic Data:**
    * Teams and team staff relationships
    * Patients (players) and their profiles
    * Patient injuries and treatment records
    * Treatment notes and medical history
    * Emergency contacts and relationships

    **Standard Odoo Data:**
    * Users and access rights
    * Partners and contacts
    * Companies and organizational structure
    * Mail activities and communication history
    * Attachments and documents
    * Calendar events transformed into project tasks (with attendees as assignees)

    **Migration Features:**
    * Direct database connection to Odoo 16 Community source
    * Data validation and transformation for Odoo 18 Enterprise
    * Data merging and deconfliction with existing data in target production database
    * Batch processing with progress tracking
    * Error handling and rollback capabilities
    * Mapping of user groups and permissions
    * Preservation of relationships and references

    **Technical Requirements:**
    * Source: Odoo 16 Community with bemade_sports_clinic (16.0 branch)
    * Target: Odoo 18 Enterprise with bemade_sports_clinic (18.0 branch)
    * PostgreSQL database connectivity
    * Data integrity validation
    
    This module provides a comprehensive migration path for upgrading FitCrew Sports Clinic
    installations from Odoo 16 Community to Odoo 18 Enterprise while preserving all
    critical business data and relationships.
    """,
    "category": "Data Migration",
    "author": "Bemade Inc.",
    "website": "http://www.bemade.org",
    "license": "LGPL-3",
    "depends": [
        "base",
        "mail",
        "contacts",
        "project",
        "bemade_sports_clinic",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/odoo16_database_views.xml",
    ],
    "assets": {},
    "installable": True,
    "auto_install": False,
    "application": False,
}
