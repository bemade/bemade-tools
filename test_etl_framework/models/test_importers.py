"""Test importer models for ETL Framework testing.

These models are registered in Odoo and can be used in tests to verify
the full ETL pipeline execution flow.
"""

import logging

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


# =============================================================================
# Simple Pipeline - No Multiprocessing
# =============================================================================


@ETL.pipeline(
    target_model="res.partner.category",
    importer_name="test.simple.importer",
    sap_source="test_source",
    allow_multiprocessing=False,
)
class TestSimpleImporter(models.AbstractModel):
    """Simple test importer that creates partner categories."""

    _name = "test.simple.importer"
    _description = "Test Simple Importer"

    # Class-level storage for test verification
    _last_extracted = None
    _last_transformed = None
    _last_loaded_ids = None

    @ETL.extract("test_source")
    def extract_data(self, ctx: ETLContext):
        """Extract test data (simulated - no actual external source)."""
        data = [
            {"code": "TEST1", "name": "Test Category 1"},
            {"code": "TEST2", "name": "Test Category 2"},
            {"code": "TEST3", "name": "Test Category 3"},
        ]
        TestSimpleImporter._last_extracted = data
        return data

    @ETL.transform()
    def transform_data(self, ctx: ETLContext, extracted: dict):
        """Transform extracted data to Odoo format."""
        raw_data = extracted.get("extract_data", [])
        transformed = [
            {"name": f"[{item['code']}] {item['name']}"} for item in raw_data
        ]
        TestSimpleImporter._last_transformed = transformed
        return transformed

    @ETL.load()
    def load_data(self, ctx: ETLContext, transformed: dict):
        """Load transformed data into Odoo."""
        vals_list = transformed.get("transform_data", [])
        if vals_list:
            records = ctx.env["res.partner.category"].create(vals_list)
            TestSimpleImporter._last_loaded_ids = records.ids
            _logger.info(f"Created {len(records)} test categories")


# =============================================================================
# Pipeline with Dependencies
# =============================================================================


@ETL.pipeline(
    target_model="res.partner",
    importer_name="test.dependent.importer",
    sap_source="test_partners",
    depends_on=["test.simple.importer"],
    allow_multiprocessing=False,
)
class TestDependentImporter(models.AbstractModel):
    """Test importer that depends on TestSimpleImporter."""

    _name = "test.dependent.importer"
    _description = "Test Dependent Importer"

    @ETL.extract("test_partners")
    def extract_partners(self, ctx: ETLContext):
        """Extract test partner data."""
        return [
            {"name": "Test Partner 1", "category_code": "TEST1"},
            {"name": "Test Partner 2", "category_code": "TEST2"},
        ]

    @ETL.extract("metadata")
    def extract_metadata(self, ctx: ETLContext):
        """Extract category lookup for transform."""
        categories = ctx.env["res.partner.category"].search([])
        return {cat.name: cat.id for cat in categories}

    @ETL.transform()
    def transform_partners(self, ctx: ETLContext, extracted: dict):
        """Transform partner data."""
        partners = extracted.get("extract_partners", [])
        category_map = extracted.get("extract_metadata", {})

        result = []
        for partner in partners:
            # Try to find matching category
            category_id = None
            for cat_name, cat_id in category_map.items():
                if partner["category_code"] in cat_name:
                    category_id = cat_id
                    break

            result.append(
                {
                    "name": partner["name"],
                    "category_id": [(6, 0, [category_id])] if category_id else False,
                }
            )
        return result

    @ETL.load()
    def load_partners(self, ctx: ETLContext, transformed: dict):
        """Load partners into Odoo."""
        vals_list = transformed.get("transform_partners", [])
        if vals_list:
            ctx.env["res.partner"].create(vals_list)


# =============================================================================
# Chunking Test Pipeline
# =============================================================================


@ETL.pipeline(
    target_model="res.partner.category",
    importer_name="test.chunking.importer",
    sap_source="test_bulk",
    multiprocessing_threshold=5,
    chunk_size=3,
    allow_multiprocessing=False,  # Disable MP for predictable testing
)
class TestChunkingImporter(models.AbstractModel):
    """Test importer for verifying chunking logic."""

    _name = "test.chunking.importer"
    _description = "Test Chunking Importer"

    _chunk_count = 0

    @ETL.extract("test_bulk")
    def extract_bulk(self, ctx: ETLContext):
        """Extract bulk test data."""
        return [{"name": f"Bulk Item {i}"} for i in range(10)]

    @ETL.transform()
    def transform_bulk(self, ctx: ETLContext, extracted: dict):
        """Transform bulk data."""
        items = extracted.get("extract_bulk", [])
        return [{"name": item["name"]} for item in items]

    @ETL.load()
    def load_bulk(self, ctx: ETLContext, transformed: dict):
        """Load bulk data."""
        vals_list = transformed.get("transform_bulk", [])
        if vals_list:
            ctx.env["res.partner.category"].create(vals_list)


# =============================================================================
# Failing Pipeline - For testing uncaught exception reporting
# =============================================================================


@ETL.pipeline(
    target_model="res.partner.category",
    importer_name="test.failing.importer",
    sap_source="test_fail",
    allow_multiprocessing=False,
)
class TestFailingImporter(models.AbstractModel):
    """Test importer that raises an exception during load."""

    _name = "test.failing.importer"
    _description = "Test Failing Importer"

    @ETL.extract("test_fail")
    def extract_data(self, ctx: ETLContext):
        """Extract test data."""
        return [{"name": "Will Fail"}]

    @ETL.transform()
    def transform_data(self, ctx: ETLContext, extracted: dict):
        """Transform test data."""
        return extracted.get("extract_data", [])

    @ETL.load()
    def load_data(self, ctx: ETLContext, transformed: dict):
        """Load that intentionally fails."""
        raise RuntimeError("Intentional test failure")


# =============================================================================
# Reporting Pipeline - For testing ctx.report usage
# =============================================================================


@ETL.pipeline(
    target_model="res.partner.category",
    importer_name="test.reporting.importer",
    sap_source="test_report",
    allow_multiprocessing=False,
)
class TestReportingImporter(models.AbstractModel):
    """Test importer that uses ctx.report to log successes/warnings/failures."""

    _name = "test.reporting.importer"
    _description = "Test Reporting Importer"

    @ETL.extract("test_report")
    def extract_data(self, ctx: ETLContext):
        """Extract test data with some bad records."""
        return [
            {"name": "Good 1"},
            {"name": "Good 2"},
            {"name": ""},  # Will trigger warning
            {"name": None},  # Will trigger failure
        ]

    @ETL.transform()
    def transform_data(self, ctx: ETLContext, extracted: dict):
        """Transform, filtering out bad records and reporting."""
        raw = extracted.get("extract_data", [])
        result = []
        for i, item in enumerate(raw):
            if item.get("name"):
                result.append({"name": f"[RPT] {item['name']}"})
            elif item.get("name") == "":
                ctx.report.warning(
                    message="Empty name, skipping",
                    source_ref=f"row-{i}",
                )
            else:
                ctx.report.failure(
                    message="Null name, cannot import",
                    source_ref=f"row-{i}",
                )
        return result

    @ETL.load()
    def load_data(self, ctx: ETLContext, transformed: dict):
        """Load good records and report successes."""
        vals_list = transformed.get("transform_data", [])
        if vals_list:
            records = ctx.env["res.partner.category"].create(vals_list)
            ctx.report.success(len(records))


# =============================================================================
# Logging Pipeline - For testing auto-capture of log messages
# =============================================================================


@ETL.pipeline(
    target_model="res.partner.category",
    importer_name="test.logging.importer",
    sap_source="test_log",
    allow_multiprocessing=False,
)
class TestLoggingImporter(models.AbstractModel):
    """Test importer that uses _logger.warning/error during execution."""

    _name = "test.logging.importer"
    _description = "Test Logging Importer"

    @ETL.extract("test_log")
    def extract_data(self, ctx: ETLContext):
        """Extract test data."""
        return [
            {"name": "Good"},
            {"name": "warn_me"},
            {"name": "error_me"},
        ]

    @ETL.transform()
    def transform_data(self, ctx: ETLContext, extracted: dict):
        """Transform with logged warnings and errors."""
        raw = extracted.get("extract_data", [])
        result = []
        for item in raw:
            if item["name"] == "warn_me":
                _logger.warning("Suspicious record: %s", item["name"])
            elif item["name"] == "error_me":
                _logger.error("Bad record: %s", item["name"])
            else:
                result.append({"name": f"[LOG] {item['name']}"})
        return result

    @ETL.load()
    def load_data(self, ctx: ETLContext, transformed: dict):
        """Load good records."""
        vals_list = transformed.get("transform_data", [])
        if vals_list:
            ctx.env["res.partner.category"].create(vals_list)


# =============================================================================
# Inherit Override Pipeline - For testing _inherit method resolution
# =============================================================================


class TestSimpleImporterOverride(models.AbstractModel):
    """Inherits test.simple.importer and overrides transform + load.

    Verifies that the ETL framework resolves methods via getattr on the
    Odoo model instance (respecting _inherit MRO) rather than calling
    the stored base-class function reference directly.
    """

    _inherit = "test.simple.importer"

    # Class-level flags set by the override methods
    _override_transform_called = False
    _override_load_called = False

    @ETL.transform()
    def transform_data(self, ctx: ETLContext, extracted: dict):
        """Override: add a marker to prove this ran instead of the base."""
        TestSimpleImporterOverride._override_transform_called = True
        raw_data = extracted.get("extract_data", [])
        transformed = [{"name": f"[OVERRIDE] {item['name']}"} for item in raw_data]
        TestSimpleImporter._last_transformed = transformed
        return transformed

    @ETL.load()
    def load_data(self, ctx: ETLContext, transformed: dict):
        """Override: set flag and delegate to create."""
        TestSimpleImporterOverride._override_load_called = True
        vals_list = transformed.get("transform_data", [])
        if vals_list:
            records = ctx.env["res.partner.category"].create(vals_list)
            # Store on the base class so existing test helpers still work
            from odoo.addons.test_etl_framework.models.test_importers import (
                TestSimpleImporter,
            )

            TestSimpleImporter._last_loaded_ids = records.ids
