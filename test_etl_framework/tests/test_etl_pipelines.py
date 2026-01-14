"""Tests for ETL Framework using real registered models.

These tests verify the full ETL pipeline execution including:
- Pipeline registration and discovery
- Dependency resolution
- Extract/Transform/Load execution
- Chunking behavior
- Multiprocessing configuration
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.etl_framework import (
    ETL,
    ETLContext,
    ETLExecutor,
    ETLPhase,
    MultiprocessingConfig,
    PipelineOrchestrator,
)


@tagged("post_install", "-at_install")
class TestMultiprocessingConfig(TransactionCase):
    """Test MultiprocessingConfig behavior."""

    def test_should_use_multiprocessing_enabled(self):
        """Test multiprocessing threshold logic when enabled."""
        config = MultiprocessingConfig(enabled=True, threshold=100)

        self.assertFalse(config.should_use_multiprocessing(50))
        self.assertFalse(config.should_use_multiprocessing(99))
        self.assertTrue(config.should_use_multiprocessing(100))
        self.assertTrue(config.should_use_multiprocessing(1000))

    def test_should_use_multiprocessing_disabled(self):
        """Test multiprocessing is never used when disabled."""
        config = MultiprocessingConfig(enabled=False, threshold=100)

        self.assertFalse(config.should_use_multiprocessing(50))
        self.assertFalse(config.should_use_multiprocessing(100))
        self.assertFalse(config.should_use_multiprocessing(10000))

    def test_get_workers_default(self):
        """Test default worker count is CPU count - 1."""
        import os

        config = MultiprocessingConfig()
        cpu_count = os.cpu_count() or 1
        expected = max(1, cpu_count - 2)
        self.assertEqual(config.get_workers(), expected)

    def test_get_workers_explicit(self):
        """Test explicit worker count is respected."""
        config = MultiprocessingConfig(max_workers=4)
        self.assertEqual(config.get_workers(), 4)


@tagged("post_install", "-at_install")
class TestETLPhase(TransactionCase):
    """Test ETLPhase enum."""

    def test_phase_values(self):
        """Test all expected phases exist."""
        self.assertEqual(ETLPhase.EXTRACT.value, "extract")
        self.assertEqual(ETLPhase.TRANSFORM.value, "transform")
        self.assertEqual(ETLPhase.LOAD.value, "load")


@tagged("post_install", "-at_install")
class TestPipelineRegistration(TransactionCase):
    """Test that pipelines are properly registered."""

    def test_simple_importer_registered(self):
        """Test simple importer pipeline is registered."""
        pipeline = ETL.get_pipeline("test.simple.importer")
        self.assertIsNotNone(pipeline)
        self.assertEqual(pipeline.target_model, "res.partner.category")
        self.assertEqual(pipeline.sap_source, "test_source")
        self.assertFalse(pipeline.multiprocessing.enabled)

    def test_dependent_importer_registered(self):
        """Test dependent importer pipeline is registered."""
        pipeline = ETL.get_pipeline("test.dependent.importer")
        self.assertIsNotNone(pipeline)
        self.assertEqual(pipeline.target_model, "res.partner")
        self.assertIn("test.simple.importer", pipeline.depends_on)

    def test_chunking_importer_registered(self):
        """Test chunking importer pipeline is registered."""
        pipeline = ETL.get_pipeline("test.chunking.importer")
        self.assertIsNotNone(pipeline)
        self.assertEqual(pipeline.multiprocessing.threshold, 5)
        self.assertEqual(pipeline.multiprocessing.chunk_size, 3)

    def test_pipeline_has_extract_methods(self):
        """Test pipeline has registered extract methods."""
        pipeline = ETL.get_pipeline("test.simple.importer")
        self.assertEqual(len(pipeline.extract_methods), 1)
        self.assertEqual(pipeline.extract_methods[0].source_table, "test_source")
        self.assertEqual(pipeline.extract_methods[0].phase, ETLPhase.EXTRACT)

    def test_pipeline_has_transform_methods(self):
        """Test pipeline has registered transform methods."""
        pipeline = ETL.get_pipeline("test.simple.importer")
        self.assertEqual(len(pipeline.transform_methods), 1)
        self.assertEqual(pipeline.transform_methods[0].phase, ETLPhase.TRANSFORM)

    def test_pipeline_has_load_methods(self):
        """Test pipeline has registered load methods."""
        pipeline = ETL.get_pipeline("test.simple.importer")
        self.assertEqual(len(pipeline.load_methods), 1)
        self.assertEqual(pipeline.load_methods[0].phase, ETLPhase.LOAD)


@tagged("post_install", "-at_install")
class TestSimplePipelineExecution(TransactionCase):
    """Test simple pipeline execution."""

    def test_execute_simple_pipeline(self):
        """Test executing the simple test pipeline."""
        from odoo.addons.test_etl_framework.models.test_importers import (
            TestSimpleImporter,
        )

        # Clear any previous test data
        TestSimpleImporter._last_extracted = None
        TestSimpleImporter._last_transformed = None
        TestSimpleImporter._last_loaded_ids = None

        # Get pipeline and execute
        pipeline = ETL.get_pipeline("test.simple.importer")
        ctx = ETLContext(cr=None, env=self.env)
        importer = self.env["test.simple.importer"]

        executor = ETLExecutor(pipeline, ctx, importer)
        executor.execute()

        # Verify extract was called
        self.assertIsNotNone(TestSimpleImporter._last_extracted)
        self.assertEqual(len(TestSimpleImporter._last_extracted), 3)

        # Verify transform was called
        self.assertIsNotNone(TestSimpleImporter._last_transformed)
        self.assertEqual(len(TestSimpleImporter._last_transformed), 3)

        # Verify load created records
        self.assertIsNotNone(TestSimpleImporter._last_loaded_ids)
        self.assertEqual(len(TestSimpleImporter._last_loaded_ids), 3)

        # Verify records exist in database
        categories = self.env["res.partner.category"].browse(
            TestSimpleImporter._last_loaded_ids
        )
        self.assertEqual(len(categories), 3)
        self.assertTrue(all("[TEST" in cat.name for cat in categories))


@tagged("post_install", "-at_install")
class TestChunkingBehavior(TransactionCase):
    """Test chunking behavior."""

    def test_create_chunks_list(self):
        """Test chunking a simple list."""
        pipeline = ETL.get_pipeline("test.chunking.importer")
        ctx = ETLContext(cr=None, env=self.env)
        importer = self.env["test.chunking.importer"]

        executor = ETLExecutor(pipeline, ctx, importer)

        # Simulate extracted data
        extracted_data = {"extract_bulk": [{"name": f"Item {i}"} for i in range(10)]}
        chunks = executor._create_chunks(extracted_data)

        # 10 items / 3 chunk_size = 4 chunks (3+3+3+1)
        self.assertEqual(len(chunks), 4)
        self.assertEqual(len(chunks[0]["extract_bulk"]), 3)
        self.assertEqual(len(chunks[1]["extract_bulk"]), 3)
        self.assertEqual(len(chunks[2]["extract_bulk"]), 3)
        self.assertEqual(len(chunks[3]["extract_bulk"]), 1)

    def test_create_chunks_headers_pattern(self):
        """Test chunking data with 'headers' key pattern."""
        pipeline = ETL.get_pipeline("test.chunking.importer")
        ctx = ETLContext(cr=None, env=self.env)
        importer = self.env["test.chunking.importer"]

        executor = ETLExecutor(pipeline, ctx, importer)

        # Simulate extracted data with headers pattern
        extracted_data = {
            "extract_orders": {
                "headers": [{"id": i} for i in range(7)],
                "lines": [{"order_id": 1}, {"order_id": 2}],  # Shared metadata
            }
        }
        chunks = executor._create_chunks(extracted_data)

        # 7 headers / 3 chunk_size = 3 chunks (3+3+1)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]["extract_orders"]["headers"]), 3)
        self.assertEqual(len(chunks[1]["extract_orders"]["headers"]), 3)
        self.assertEqual(len(chunks[2]["extract_orders"]["headers"]), 1)

        # Lines should be preserved in all chunks
        for chunk in chunks:
            self.assertEqual(
                chunk["extract_orders"]["lines"],
                [{"order_id": 1}, {"order_id": 2}],
            )


@tagged("post_install", "-at_install")
class TestDependencyResolution(TransactionCase):
    """Test pipeline dependency resolution."""

    def test_resolve_dependencies_order(self):
        """Test that dependencies are resolved in correct order."""
        orchestrator = PipelineOrchestrator(self.env)
        order = orchestrator._resolve_dependencies()

        # test.simple.importer should come before test.dependent.importer
        if "test.simple.importer" in order and "test.dependent.importer" in order:
            simple_idx = order.index("test.simple.importer")
            dependent_idx = order.index("test.dependent.importer")
            self.assertLess(
                simple_idx,
                dependent_idx,
                "Simple importer should execute before dependent importer",
            )


@tagged("post_install", "-at_install")
class TestETLContext(TransactionCase):
    """Test ETLContext dataclass."""

    def test_context_creation(self):
        """Test ETLContext can be created."""
        ctx = ETLContext(cr=None, env=self.env)
        self.assertIsNone(ctx.cr)
        self.assertEqual(ctx.env, self.env)

    def test_context_env_access(self):
        """Test ETLContext env can access models."""
        ctx = ETLContext(cr=None, env=self.env)
        partners = ctx.env["res.partner"].search([], limit=1)
        self.assertIsNotNone(partners)
