"""
ETL Framework for SAP B1 to Odoo Data Migration

This module provides a declarative, self-optimizing ETL (Extract, Transform, Load)
framework for migrating data from SAP Business One to Odoo.

Key Features:
- Declarative pipeline definition using decorators
- Automatic multiprocessing based on data volume
- Dependency resolution between models
- Memory-efficient execution
- Clear separation of Extract, Transform, and Load phases

Usage Example:
    @ETL.pipeline(
        target_model='product.product',
        sap_source='oitm',
        depends_on=['product.category'],
        multiprocessing_threshold=1000,
    )
    class SapProductImporter(models.AbstractModel):
        _name = 'sap.product.importer'
        
        @ETL.extract('oitm')
        def extract_products(self, ctx: ETLContext):
            ctx.cr.execute("SELECT * FROM oitm")
            return ctx.cr.dictfetchall()
        
        @ETL.transform()
        def transform_products(self, ctx: ETLContext, sap_products):
            return [{"name": p["itemname"]} for p in sap_products]
        
        @ETL.load()
        def load_products(self, ctx: ETLContext, product_vals):
            ctx.env['product.product'].create(product_vals)
"""

import logging
import multiprocessing
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from odoo import api
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


# =============================================================================
# Core Data Structures
# =============================================================================


class ETLPhase(Enum):
    """Enumeration of ETL pipeline phases."""
    EXTRACT = "extract"
    TRANSFORM = "transform"
    LOAD = "load"


@dataclass
class ETLContext:
    """Lightweight context object passed to all ETL methods.
    
    This context contains only references to database cursors and environments,
    not actual data. This prevents memory overload when processing large datasets.
    
    Attributes:
        cr: SAP database cursor for querying source data.
        env: Odoo environment for creating/updating records.
    """
    cr: Any  # SAP database cursor
    env: Any  # Odoo environment


@dataclass
class MultiprocessingConfig:
    """Configuration for dynamic multiprocessing decisions.
    
    Attributes:
        enabled: Whether multiprocessing is allowed for this pipeline.
        threshold: Minimum number of records to trigger multiprocessing.
        chunk_size: Number of records to process per worker.
        max_workers: Maximum number of worker processes (None = cpu_count - 1).
    """
    enabled: bool = True
    threshold: int = 1000
    chunk_size: int = 500
    max_workers: Optional[int] = None
    
    def should_use_multiprocessing(self, record_count: int) -> bool:
        """Determine if multiprocessing should be used based on record count.
        
        Args:
            record_count: Number of records extracted.
            
        Returns:
            True if multiprocessing should be used, False otherwise.
        """
        return self.enabled and record_count >= self.threshold
    
    def get_workers(self) -> int:
        """Get the number of worker processes to use.
        
        Returns:
            Number of workers (defaults to cpu_count - 1).
        """
        if self.max_workers is not None:
            return self.max_workers
        cpu_count = os.cpu_count()
        return (cpu_count - 1) if cpu_count and cpu_count > 1 else 1


@dataclass
class ETLMethod:
    """Represents a single ETL method (extract, transform, or load).
    
    Attributes:
        phase: The ETL phase this method belongs to.
        func: The actual method to execute.
        source_table: SAP table name (for extract methods only).
    """
    phase: ETLPhase
    func: Callable
    source_table: Optional[str] = None


@dataclass
class ETLPipeline:
    """Declarative ETL pipeline definition for a single Odoo model.
    
    Attributes:
        target_model: Odoo model name (e.g., 'product.product').
        sap_source: Primary SAP table name (optional, for documentation).
        depends_on: List of Odoo model names this pipeline depends on.
        multiprocessing: Configuration for multiprocessing behavior.
        importer_model_name: Odoo model name of the importer (set by decorator).
        extract_methods: List of registered extraction methods.
        transform_methods: List of registered transformation methods.
        load_methods: List of registered loading methods.
    """
    target_model: str
    sap_source: str
    depends_on: List[str] = field(default_factory=list)
    multiprocessing: MultiprocessingConfig = field(default_factory=MultiprocessingConfig)
    importer_model_name: Optional[str] = None
    
    # Registered methods (populated by decorators)
    extract_methods: List[ETLMethod] = field(default_factory=list)
    transform_methods: List[ETLMethod] = field(default_factory=list)
    load_methods: List[ETLMethod] = field(default_factory=list)
    
    def get_methods_by_phase(self, phase: ETLPhase) -> List[ETLMethod]:
        """Get all methods for a specific phase.
        
        Args:
            phase: The ETL phase to filter by.
            
        Returns:
            List of ETL methods for the specified phase.
        """
        if phase == ETLPhase.EXTRACT:
            return self.extract_methods
        elif phase == ETLPhase.TRANSFORM:
            return self.transform_methods
        elif phase == ETLPhase.LOAD:
            return self.load_methods
        return []


# =============================================================================
# Decorator Classes
# =============================================================================


class ETL:
    """Decorator factory for registering ETL pipelines and methods.
    
    This class provides decorators for:
    - Defining pipelines at the class level
    - Registering extract, transform, and load methods
    
    All registered pipelines are stored in the class-level _pipelines dict.
    """
    
    _pipelines: Dict[str, ETLPipeline] = {}
    
    @classmethod
    def pipeline(
        cls,
        target_model: str,
        sap_source: Optional[str] = None,
        depends_on: Optional[List[str]] = None,
        multiprocessing_threshold: int = 1000,
        chunk_size: int = 500,
        max_workers: Optional[int] = None,
        allow_multiprocessing: bool = True,
    ):
        """Class decorator to define an ETL pipeline.
        
        Args:
            target_model: Odoo model name (e.g., 'product.product').
            sap_source: Primary SAP table name (optional, for documentation).
            depends_on: List of model names this pipeline depends on.
            multiprocessing_threshold: Min records to trigger multiprocessing.
            chunk_size: Records per chunk for parallel processing.
            max_workers: Max worker processes (None = cpu_count - 1).
            allow_multiprocessing: Whether multiprocessing is allowed.
            
        Returns:
            Decorator function that registers the pipeline.
            
        Example:
            @ETL.pipeline(
                target_model='product.product',
                sap_source='oitm',
                depends_on=['product.category'],
                multiprocessing_threshold=1000,
            )
            class SapProductImporter(models.AbstractModel):
                # _name will be auto-generated as 'product.product.importer'
                ...
        """
        def decorator(importer_class):
            # Auto-generate importer model name
            importer_name = f"{target_model}.importer"
            
            mp_config = MultiprocessingConfig(
                enabled=allow_multiprocessing,
                threshold=multiprocessing_threshold,
                chunk_size=chunk_size,
                max_workers=max_workers,
            )
            pipeline = ETLPipeline(
                target_model=target_model,
                sap_source=sap_source or '',
                depends_on=depends_on or [],
                multiprocessing=mp_config,
            )
            
            # Inject _name attribute
            importer_class._name = importer_name
            
            # Store importer model name for later lookup
            pipeline.importer_model_name = importer_name
            
            # Register pipeline
            cls._pipelines[target_model] = pipeline
            importer_class._etl_pipeline = pipeline
            
            # Scan class for decorated methods and register them
            for attr_name in dir(importer_class):
                attr = getattr(importer_class, attr_name)
                if hasattr(attr, '_etl_method'):
                    etl_method = attr._etl_method
                    if etl_method.phase == ETLPhase.EXTRACT:
                        pipeline.extract_methods.append(etl_method)
                    elif etl_method.phase == ETLPhase.TRANSFORM:
                        pipeline.transform_methods.append(etl_method)
                    elif etl_method.phase == ETLPhase.LOAD:
                        pipeline.load_methods.append(etl_method)
            
            return importer_class
        return decorator
    
    @classmethod
    def extract(cls, source_table: str):
        """Method decorator for extraction methods.
        
        Args:
            source_table: SAP table name being extracted from.
            
        Returns:
            Decorator function that marks the method as an extractor.
            
        Example:
            @ETL.extract('oitm')
            def extract_products(self, ctx: ETLContext):
                ctx.cr.execute("SELECT * FROM oitm")
                return ctx.cr.dictfetchall()
        """
        def decorator(func):
            func._etl_method = ETLMethod(
                phase=ETLPhase.EXTRACT,
                func=func,
                source_table=source_table,
            )
            return func
        return decorator
    
    @classmethod
    def transform(cls):
        """Method decorator for transformation methods.
        
        Returns:
            Decorator function that marks the method as a transformer.
            
        Example:
            @ETL.transform()
            def transform_products(self, ctx: ETLContext, sap_products):
                return [{"name": p["itemname"]} for p in sap_products]
        """
        def decorator(func):
            func._etl_method = ETLMethod(
                phase=ETLPhase.TRANSFORM,
                func=func,
            )
            return func
        return decorator
    
    @classmethod
    def load(cls):
        """Method decorator for loading methods.
        
        Returns:
            Decorator function that marks the method as a loader.
            
        Example:
            @ETL.load()
            def load_products(self, ctx: ETLContext, product_vals):
                ctx.env['product.product'].create(product_vals)
        """
        def decorator(func):
            func._etl_method = ETLMethod(
                phase=ETLPhase.LOAD,
                func=func,
            )
            return func
        return decorator
    
    @classmethod
    def get_pipeline(cls, target_model: str) -> Optional[ETLPipeline]:
        """Get a registered pipeline by target model name.
        
        Args:
            target_model: Odoo model name.
            
        Returns:
            ETLPipeline if found, None otherwise.
        """
        return cls._pipelines.get(target_model)
    
    @classmethod
    def get_all_pipelines(cls) -> Dict[str, ETLPipeline]:
        """Get all registered pipelines.
        
        Returns:
            Dictionary mapping model names to pipelines.
        """
        return cls._pipelines.copy()


# =============================================================================
# Execution Engine
# =============================================================================


class ETLExecutor:
    """Executes a single ETL pipeline with dynamic multiprocessing.
    
    The executor:
    1. Runs all extract methods
    2. Counts extracted records
    3. Decides whether to use multiprocessing based on count
    4. Executes transform and load (single-process or parallel)
    
    Attributes:
        pipeline: The ETL pipeline to execute.
        ctx: The ETL context with database connections.
        importer: The importer model instance.
    """
    
    def __init__(self, pipeline: ETLPipeline, ctx: ETLContext, importer: Any):
        """Initialize the executor.
        
        Args:
            pipeline: The ETL pipeline to execute.
            ctx: The ETL context.
            importer: The importer model instance.
        """
        self.pipeline = pipeline
        self.ctx = ctx
        self.importer = importer
    
    def execute(self) -> None:
        """Execute the complete ETL pipeline."""
        _logger.info(f"Starting ETL pipeline for {self.pipeline.target_model}")
        
        # Phase 1: Extract
        extracted_data = self._execute_extract()
        
        # Phase 2: Decide execution strategy
        record_count = self._get_record_count(extracted_data)
        use_multiprocessing = self.pipeline.multiprocessing.should_use_multiprocessing(
            record_count
        )
        
        _logger.info(
            f"Extracted {record_count} records. "
            f"Using {'multiprocessing' if use_multiprocessing else 'single-process'} mode."
        )
        
        # Phase 3 & 4: Transform and Load
        if use_multiprocessing:
            self._execute_parallel(extracted_data)
        else:
            self._execute_sequential(extracted_data)
        
        _logger.info(f"Completed ETL pipeline for {self.pipeline.target_model}")
    
    def _execute_extract(self) -> Dict[str, Any]:
        """Execute all extraction methods.
        
        Returns:
            Dictionary mapping method names to extracted data.
        """
        results = {}
        for method in self.pipeline.extract_methods:
            _logger.info(f"Extracting from {method.source_table}")
            result = method.func(self.importer, self.ctx)
            results[method.func.__name__] = result
        return results
    
    def _get_record_count(self, extracted_data: Dict[str, Any]) -> int:
        """Determine the number of records extracted.
        
        Args:
            extracted_data: Dictionary of extraction results.
            
        Returns:
            Total number of records extracted.
        """
        counts = []
        for data in extracted_data.values():
            if isinstance(data, (list, tuple)):
                # Handle (records, chunks) tuple from extraction
                if len(data) == 2 and isinstance(data[0], list):
                    counts.append(len(data[0]))
                else:
                    counts.append(len(data))
            elif isinstance(data, dict):
                # Handle dict of lists
                for value in data.values():
                    if isinstance(value, list):
                        counts.append(len(value))
        
        return max(counts) if counts else 0
    
    def _execute_sequential(self, extracted_data: Dict[str, Any]) -> None:
        """Execute transform and load in single process.
        
        Args:
            extracted_data: Dictionary of extraction results.
        """
        # Transform
        transformed_data = {}
        for method in self.pipeline.transform_methods:
            _logger.info(f"Transforming with {method.func.__name__}")
            result = method.func(self.importer, self.ctx, extracted_data)
            transformed_data[method.func.__name__] = result
        
        # Load
        for method in self.pipeline.load_methods:
            _logger.info(f"Loading with {method.func.__name__}")
            method.func(self.importer, self.ctx, transformed_data)
    
    def _execute_parallel(self, extracted_data: Dict[str, Any]) -> None:
        """Execute transform and load using multiprocessing.
        
        Args:
            extracted_data: Dictionary of extraction results.
        """
        # Create chunks
        chunks = self._create_chunks(extracted_data)
        
        mp_config = self.pipeline.multiprocessing
        workers = mp_config.get_workers()
        
        _logger.info(
            f"Processing {len(chunks)} chunks with {workers} workers."
        )
        
        start_method = multiprocessing.get_start_method()
        multiprocessing.set_start_method("fork", force=True)
        
        try:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        self._process_chunk_static,
                        self.ctx.env.cr.dbname,
                        self.ctx.env.uid,
                        dict(self.ctx.env.context),
                        self.importer._name,
                        chunk,
                        self.pipeline.target_model,
                    )
                    for chunk in chunks
                ]
                
                for i, future in enumerate(futures, 1):
                    future.result()
                    _logger.info(f"Completed chunk {i}/{len(chunks)}")
        except Exception:
            _logger.error("Multiprocessing execution failed", exc_info=True)
            raise
        finally:
            multiprocessing.set_start_method(start_method, force=True)
    
    def _create_chunks(self, extracted_data: Dict[str, Any]) -> List[Any]:
        """Split extracted data into chunks for parallel processing.
        
        Args:
            extracted_data: Dictionary of extraction results.
            
        Returns:
            List of data chunks.
        """
        chunk_size = self.pipeline.multiprocessing.chunk_size
        
        # Handle (records, chunks) tuple from extraction
        for data in extracted_data.values():
            if isinstance(data, tuple) and len(data) == 2:
                # Already chunked by extraction method
                return data[1]
        
        # Otherwise, chunk the first list we find
        for data in extracted_data.values():
            if isinstance(data, list):
                return [
                    data[i:i + chunk_size]
                    for i in range(0, len(data), chunk_size)
                ]
        
        return [extracted_data]
    
    @staticmethod
    def _process_chunk_static(
        dbname: str,
        uid: int,
        context: dict,
        importer_name: str,
        chunk: Any,
        target_model: str,
    ) -> None:
        """Process a single chunk in a subprocess (static method for pickling).
        
        Args:
            dbname: Odoo database name.
            uid: User ID.
            context: Odoo context dict.
            importer_name: Name of the importer model.
            chunk: Data chunk to process.
            target_model: Target Odoo model name.
        """
        try:
            with Registry(dbname).cursor() as cr:
                env = api.Environment(cr, uid, context)
                importer = env[importer_name]
                pipeline = importer._etl_pipeline  # type: ignore[attr-defined]
                
                # Create context (note: cr here is Odoo cursor, not SAP cursor)
                # In multiprocessing, we need to handle this differently
                # For now, this is a placeholder for the actual implementation
                
                # Transform
                transformed_data = {}
                for method in pipeline.transform_methods:
                    result = method.func(importer, None, chunk)
                    transformed_data[method.func.__name__] = result
                
                # Load
                for method in pipeline.load_methods:
                    method.func(importer, env, transformed_data)
                
                cr.commit()
        except Exception:
            _logger.error("Chunk processing failed", exc_info=True)
            raise


# =============================================================================
# Pipeline Orchestrator
# =============================================================================


class PipelineOrchestrator:
    """Orchestrates execution of multiple ETL pipelines with dependency resolution.
    
    The orchestrator:
    1. Resolves dependencies between pipelines (topological sort)
    2. Executes pipelines in the correct order
    3. Manages database commits between pipelines
    
    Attributes:
        env: Odoo environment.
        pipelines: Dictionary of all registered pipelines.
    """
    
    def __init__(self, env: Any):
        """Initialize the orchestrator.
        
        Args:
            env: Odoo environment.
        """
        self.env = env
        self.pipelines = ETL.get_all_pipelines()
    
    def execute_all(self, cr: Any) -> None:
        """Execute all registered pipelines in dependency order.
        
        Args:
            cr: SAP database cursor.
        """
        _logger.info("Starting ETL orchestration for all pipelines")
        
        # Resolve execution order
        execution_order = self._resolve_dependencies()
        _logger.info(f"Execution order: {execution_order}")
        
        # Execute each pipeline
        ctx = ETLContext(cr=cr, env=self.env)
        
        for model_name in execution_order:
            pipeline = self.pipelines.get(model_name)
            if not pipeline:
                _logger.warning(f"Pipeline for {model_name} not found, skipping")
                continue
            
            # Get importer instance using the model name from the decorator
            if not pipeline.importer_model_name:
                _logger.error(
                    f"Pipeline for {model_name} has no importer_model_name. "
                    f"This should be set by the @ETL.pipeline decorator."
                )
                continue
            
            try:
                importer = self.env[pipeline.importer_model_name]
            except KeyError:
                _logger.error(
                    f"Importer model '{pipeline.importer_model_name}' not found "
                    f"for target model '{model_name}'. Make sure the model is registered."
                )
                continue
            
            # Execute pipeline
            executor = ETLExecutor(pipeline, ctx, importer)
            executor.execute()
            
            # Commit after each pipeline
            self.env.cr.commit()
        
        _logger.info("Completed ETL orchestration for all pipelines")
    
    def _resolve_dependencies(self) -> List[str]:
        """Resolve pipeline dependencies using topological sort.
        
        Returns:
            List of model names in execution order.
            
        Raises:
            ValueError: If circular dependencies are detected.
        """
        # Build dependency graph
        graph = {model: pipeline.depends_on for model, pipeline in self.pipelines.items()}
        
        # Topological sort (Kahn's algorithm)
        in_degree = {model: 0 for model in graph}
        for model, deps in graph.items():
            for dep in deps:
                if dep in in_degree:
                    in_degree[dep] += 1
        
        queue = [model for model, degree in in_degree.items() if degree == 0]
        result = []
        
        while queue:
            model = queue.pop(0)
            result.append(model)
            
            for other_model, deps in graph.items():
                if model in deps:
                    in_degree[other_model] -= 1
                    if in_degree[other_model] == 0:
                        queue.append(other_model)
        
        if len(result) != len(graph):
            raise ValueError("Circular dependency detected in ETL pipelines")
        
        return result
