"""QuickBooks Online Employee ETL Pipeline

This module handles the migration of Employees from QBO to Odoo hr.employee
using the ETL framework.
"""

import logging
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="hr.employee",
    importer_name="qbo.employee.importer",
    sap_source="Employee",
    depends_on=[],
)
class QboEmployeeImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Employees."""

    _name = "qbo.employee.importer"
    _description = "QBO Employee Importer"

    @ETL.extract("Employee")
    def extract_employees(self, ctx: ETLContext) -> List[Dict]:
        """Extract employees from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO employee IDs
        ctx.env.cr.execute(
            "SELECT qbo_employee_id FROM hr_employee WHERE qbo_employee_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing employees in Odoo")

        # Fetch all employees from QBO
        employees = api_client.query_all(entity="Employee", order_by="Id")

        # Filter out already imported
        new_employees = [
            emp for emp in employees if str(emp.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(employees)} employees from QBO, "
            f"{len(new_employees)} are new"
        )
        return new_employees

    @ETL.transform()
    def transform_employees(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO employees into Odoo hr.employee values."""
        employees = extracted.get("extract_employees", [])

        company = ctx.env.company

        employee_vals = []
        skipped = 0

        for emp in employees:
            try:
                # Get name parts
                given_name = emp.get("GivenName", "") or ""
                family_name = emp.get("FamilyName", "") or ""
                display_name = emp.get("DisplayName", "")

                # Build full name
                if given_name or family_name:
                    name = f"{given_name} {family_name}".strip()
                else:
                    name = display_name or f"Employee {emp.get('Id')}"

                # Get email
                email = None
                primary_email = emp.get("PrimaryEmailAddr", {})
                if primary_email:
                    email = primary_email.get("Address")

                # Get phone
                phone = None
                primary_phone = emp.get("PrimaryPhone", {})
                if primary_phone:
                    phone = primary_phone.get("FreeFormNumber")

                mobile = None
                mobile_phone = emp.get("Mobile", {})
                if mobile_phone:
                    mobile = mobile_phone.get("FreeFormNumber")

                # Get address
                address = emp.get("PrimaryAddr", {})
                street = address.get("Line1", "") if address else ""
                city = address.get("City", "") if address else ""

                # Build employee values
                vals = {
                    "name": name,
                    "qbo_employee_id": int(emp.get("Id", 0)),
                    "company_id": company.id,
                }

                # Add optional fields if present
                if email:
                    vals["work_email"] = email
                if phone:
                    vals["work_phone"] = phone
                if mobile:
                    vals["mobile_phone"] = mobile

                # Set private address info if available
                if street or city:
                    vals["private_street"] = street
                    vals["private_city"] = city

                employee_vals.append(vals)

            except Exception as e:
                _logger.error(f"Error transforming employee {emp.get('Id')}: {e}")
                skipped += 1

        _logger.info(f"Transformed {len(employee_vals)} employees, skipped {skipped}")
        return employee_vals

    @ETL.load()
    def load_employees(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load employees into Odoo."""
        employee_vals = transformed.get("transform_employees", [])

        if not employee_vals:
            _logger.info("No new employees to create")
            return

        created = 0
        errors = 0

        for vals in employee_vals:
            try:
                # Remove private address fields that don't exist on hr.employee
                private_street = vals.pop("private_street", None)
                private_city = vals.pop("private_city", None)

                employee = ctx.env["hr.employee"].create(vals)
                created += 1

                # If we have address info, try to set it on the private address
                if private_street or private_city:
                    try:
                        if not employee.address_home_id:
                            # Create a private address partner
                            partner_vals = {
                                "name": employee.name,
                                "type": "private",
                                "street": private_street,
                                "city": private_city,
                            }
                            partner = ctx.env["res.partner"].create(partner_vals)
                            employee.address_home_id = partner.id
                    except Exception:
                        pass  # Address is optional

                _logger.debug(f"Created employee {employee.name}")

            except Exception as e:
                _logger.error(f"Failed to create employee {vals.get('name')}: {e}")
                errors += 1

        _logger.info(f"Created {created} employees, {errors} errors")
