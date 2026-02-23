"""ETL Framework for Odoo Data Migration.

This module provides a declarative, self-optimizing ETL (Extract, Transform, Load)
framework for migrating data from external sources to Odoo.

Key Features:

* Declarative pipeline definition using decorators
* Automatic multiprocessing based on data volume
* Dependency resolution between models
* Memory-efficient execution
* Clear separation of Extract, Transform, and Load phases
* Generic source configuration for any external database

See README.md for usage examples and source configuration details.
"""

import logging
import multiprocessing
import os
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import psycopg2
import psycopg2.errors
import psycopg2.extensions
from contextlib import contextmanager
from enum import Enum

from typing import Any, Callable, Dict, List, Optional

# DB errors that must propagate for chunk-level retry.
# Everything else inside a savepoint is safe to skip.
RETRYABLE_DB_ERRORS = (
    psycopg2.errors.SerializationFailure,
    psycopg2.errors.DeadlockDetected,
    psycopg2.extensions.TransactionRollbackError,
    psycopg2.OperationalError,
    psycopg2.InternalError,
)


@dataclass
class ChunkableData:
    """Data structure for extract methods returning chunkable records with metadata.

    Use this when an extract method needs to return both records for chunking
    and additional lookup data (mappings, etc.) that should be available to
    all chunks during transform/load.

    The records list will be split across worker processes based on chunk_size,
    while context is passed unchanged to each chunk.

    In transform, access via extracted.get('extract_lines').records for the
    chunked records and .context for the lookup data.

    :param records: The list of records to be chunked for parallel processing.
    :param context: Additional data (mappings, lookups) available to all chunks.
    """

    records: List[Dict[str, Any]]
    context: Dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        """Allow dict-like access for backward compatibility."""
        if key == "records":
            return self.records
        return self.context.get(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Allow dict-like get() for backward compatibility."""
        if key == "records":
            return self.records
        return self.context.get(key, default)


from odoo import api
from .reporter import ETLReporter, PipelineReport, ReportLogHandler


@contextmanager
def mute_retryable_errors():
    """Context manager that only mutes logging for retryable DB errors.

    Non-retryable errors (constraint violations, data errors, etc.) will
    still be logged normally so we can see the actual root cause.
    """
    import logging

    class RetryableErrorFilter(logging.Filter):
        """Filter that suppresses only retryable error messages."""

        def filter(self, record):
            # Check if this is a retryable error message
            msg = record.getMessage().lower()
            retryable_keywords = [
                "serialization",
                "deadlock",
                "could not serialize",
                "concurrent update",
            ]
            return not any(kw in msg for kw in retryable_keywords)

    sql_logger = logging.getLogger("odoo.sql_db")
    error_filter = RetryableErrorFilter()
    sql_logger.addFilter(error_filter)
    try:
        yield
    finally:
       sql_logger.removeFilter(error_filter)


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
        cr: Source database cursor for querying source data.
        env: Odoo environment for creating/updating records.
        source_config: Optional dictionary of source-specific configuration.
            This can contain any key-value pairs needed by pipelines.
            Common keys: 'filestore_path', 'source_model', 'source_id'.
    """

    cr: Any  # Source database cursor
    env: Any  # Odoo environment
    source_config: Optional[Dict[str, Any]] = None  # Source-specific config
    _reporter: Optional[Any] = field(default=None, repr=False)  # ETLReporter

    @property
    def report(self) -> PipelineReport:
        """Get the current pipeline report for logging successes/warnings/failures.

        Returns a no-op PipelineReport if no reporter is active, so callers
        can always safely call ctx.report.success() etc.
        """
        if self._reporter and self._reporter.current:
            return self._reporter.current
        # Return a detached no-op report so pipeline code never has to
        # check for None.
        return PipelineReport(pipeline_name="_detached")

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a configuration value from source_config.

        Args:
            key: Configuration key to retrieve.
            default: Default value if key not found.

        Returns:
            The configuration value or default.
        """
        if self.source_config:
            return self.source_config.get(key, default)
        return default

    @contextmanager
    def skippable(self, source_ref: str = ""):
        """Context manager for per-record operations that may fail.

        Wraps the block in a DB savepoint. If a retryable DB error occurs
        (serialization, deadlock), it propagates so the framework can retry
        the whole chunk. Any other exception is caught, logged as a warning
        on the ETL report, and execution continues to the next record.

        Usage::

            for record in records:
                with ctx.skippable(f"record {record.id}"):
                    record.action_post()
                    (line_a + line_b).reconcile()

        Args:
            source_ref: Human-readable reference for the report detail
                (e.g. SAP document number).
        """
        try:
            with self.env.cr.savepoint():
                yield
        except RETRYABLE_DB_ERRORS:
            raise
        except Exception as e:
            _logger.warning("Skipping %s: %s", source_ref, e)
            self.report.failure(message=str(e), source_ref=source_ref)


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
            Number of workers (defaults to cpu_count - 2).
        """
        if self.max_workers is not None:
            return self.max_workers
        cpu_count = os.cpu_count()
        return max(1, cpu_count - 2 if cpu_count else 0)


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
        source_module: Odoo addon module name (e.g., 'xtuple_to_odoo').
        extract_methods: List of registered extraction methods.
        transform_methods: List of registered transformation methods.
        load_methods: List of registered loading methods.
    """

    target_model: str
    sap_source: str
    depends_on: List[str] = field(default_factory=list)
    multiprocessing: MultiprocessingConfig = field(
        default_factory=MultiprocessingConfig
    )
    importer_model_name: Optional[str] = None
    source_module: Optional[str] = None

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
        importer_name: str,
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
            importer_name: Unique Odoo model name for the importer (e.g., 'res.users.importer').
                          Must be unique across all pipelines.
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
                importer_name='sap.product.importer',
                sap_source='oitm',
                depends_on=['product.category'],
                multiprocessing_threshold=1000,
            )
            class SapProductImporter(models.AbstractModel):
                _name = 'sap.product.importer'  # Must match importer_name
                ...
        """

        def decorator(importer_class):
            # Check for duplicate importer names
            for existing_pipeline in cls._pipelines.values():
                if existing_pipeline.importer_model_name == importer_name:
                    raise ValueError(
                        f"Duplicate importer name '{importer_name}' detected. "
                        f"Each pipeline must have a unique importer_name. "
                        f"Existing pipeline: target_model='{existing_pipeline.target_model}'"
                    )

            mp_config = MultiprocessingConfig(
                enabled=allow_multiprocessing,
                threshold=multiprocessing_threshold,
                chunk_size=chunk_size,
                max_workers=max_workers,
            )

            # Extract Odoo addon module name from class's __module__
            # e.g., 'odoo.addons.xtuple_to_odoo.models.pipelines.product_etl'
            #       -> 'xtuple_to_odoo'
            class_module_attr = getattr(importer_class, "__module__", None)
            class_module = (
                class_module_attr if isinstance(class_module_attr, str) else ""
            )
            source_module = None
            if class_module and ".addons." in class_module:
                # Extract the addon name (part after 'addons.')
                parts = class_module.split(".addons.")
                if len(parts) > 1:
                    source_module = parts[1].split(".")[0]

            pipeline = ETLPipeline(
                target_model=target_model,
                sap_source=sap_source or "",
                depends_on=depends_on or [],
                multiprocessing=mp_config,
                source_module=source_module,
            )

            # Inject _name attribute
            importer_class._name = importer_name

            # Store importer model name for later lookup
            pipeline.importer_model_name = importer_name

            # Register pipeline by importer name (not target model)
            cls._pipelines[importer_name] = pipeline
            importer_class._etl_pipeline = pipeline

            # Scan class for decorated methods and register them
            for attr_name in dir(importer_class):
                attr = getattr(importer_class, attr_name)
                if hasattr(attr, "_etl_method"):
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
    def extract(cls, source_table: str = ""):
        """Method decorator for extraction methods.

        Args:
            source_table: SAP table name being extracted from (optional).

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
    def get_pipeline(cls, importer_name: str) -> Optional[ETLPipeline]:
        """Get a registered pipeline by importer name.

        Args:
            importer_name: Importer model name (e.g., 'res.users.importer').

        Returns:
            ETLPipeline if found, None otherwise.
        """
        return cls._pipelines.get(importer_name)

    @classmethod
    def get_all_pipelines(cls) -> Dict[str, ETLPipeline]:
        """Get all registered pipelines.

        Returns:
            Dictionary mapping model names to pipelines.
        """
        return cls._pipelines.copy()

    @classmethod
    def get_pipelines_by_module(cls, module_name: str) -> Dict[str, ETLPipeline]:
        """Get all pipelines registered by a specific Odoo addon module.

        Args:
            module_name: Odoo addon module name (e.g., 'xtuple_to_odoo').

        Returns:
            Dictionary mapping importer names to pipelines for the given module.
        """
        return {
            name: pipeline
            for name, pipeline in cls._pipelines.items()
            if pipeline.source_module == module_name
        }


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
        pipeline_name = self.pipeline.importer_model_name
        reporter = self.ctx._reporter

        _logger.info(
            f"Starting ETL pipeline for {self.pipeline.target_model} "
            f"(importer: {pipeline_name})"
        )

        # Start reporter tracking for this pipeline
        if reporter:
            reporter.start_pipeline(
                pipeline_name=pipeline_name,
                target_model=self.pipeline.target_model,
            )

        # Install log handler to auto-capture WARNING+ messages
        log_handler = None
        if reporter and reporter.current:
            log_handler = ReportLogHandler(reporter.current)
            logging.getLogger().addHandler(log_handler)

        state = "done"
        try:
            # Phase 1: Extract
            extracted_data = self._execute_extract()

            # Phase 2: Decide execution strategy
            record_count = self._get_record_count(extracted_data)

            # Auto-track extracted count
            if reporter and reporter.current:
                reporter.current.extracted_count = record_count

            use_multiprocessing = (
                self.pipeline.multiprocessing.should_use_multiprocessing(record_count)
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

            _logger.info(f"[{pipeline_name}] Completed ETL pipeline")
        except Exception as e:
            state = "failed"
            # Record the uncaught exception as a failure detail
            if reporter and reporter.current:
                reporter.current.failure(message=str(e), source_ref="(uncaught)")
            raise
        finally:
            if log_handler:
                logging.getLogger().removeHandler(log_handler)
            if reporter:
                reporter.end_pipeline(state=state)

    def _execute_extract(self) -> Dict[str, Any]:
        """Execute all extraction methods.

        Methods are discovered by scanning the resolved Odoo model class at
        runtime (via ``type(self.importer)``) rather than using the list built
        at decoration time.  This means extract methods added to a child class
        via Odoo's ``_inherit`` mechanism are included automatically, without
        requiring the child to re-apply the ``@ETL.pipeline()`` decorator.

        Returns:
            Dictionary mapping method names to extracted data.
        """
        results = {}
        importer_class = type(self.importer)
        for attr_name in dir(importer_class):
            attr = getattr(importer_class, attr_name, None)
            if attr is None:
                continue
            etl_method = getattr(attr, "_etl_method", None)
            if etl_method is None or etl_method.phase != ETLPhase.EXTRACT:
                continue
            _logger.info(
                f"[{self.pipeline.importer_model_name}] Extracting from {etl_method.source_table or attr_name}"
            )
            bound = getattr(self.importer, attr_name)
            result = bound(self.ctx)
            results[attr_name] = result
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
            _logger.info(
                f"[{self.pipeline.importer_model_name}] Transforming with {method.func.__name__}"
            )
            bound = getattr(self.importer, method.func.__name__)
            result = bound(self.ctx, extracted_data)
            transformed_data[method.func.__name__] = result

        # Load
        for method in self.pipeline.load_methods:
            _logger.info(
                f"[{self.pipeline.importer_model_name}] Loading with {method.func.__name__}"
            )
            bound = getattr(self.importer, method.func.__name__)
            bound(self.ctx, transformed_data)

    def _execute_parallel(self, extracted_data: Dict[str, Any]) -> None:
        """Execute transform and load using multiprocessing.

        Args:
            extracted_data: Dictionary of extraction results.
        """
        # Create chunks
        chunks = self._create_chunks(extracted_data)

        mp_config = self.pipeline.multiprocessing
        workers = min(mp_config.get_workers(), len(chunks))

        _logger.info(
            f"[{self.pipeline.importer_model_name}] Processing {len(chunks)} chunks with {workers} workers."
        )

        start_method = multiprocessing.get_start_method()

        # Suppress fork warnings from debugpy and other tools
        # These warnings occur when forking in a multi-threaded process
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            warnings.filterwarnings("ignore", message=".*multi-threaded.*fork.*")
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

                    for i, (future, chunk) in enumerate(zip(futures, chunks), 1):
                        # Retry on serialization failures (concurrent updates in multiprocessing)
                        max_retries = 5
                        current_future = future

                        for attempt in range(max_retries):
                            try:
                                current_future.result()
                                _logger.info(
                                    f"[{self.pipeline.importer_model_name}] Completed chunk {i}/{len(chunks)}"
                                )
                                break
                            except Exception as e:
                                # Locate a retryable database error anywhere in the exception chain
                                retryable_exc = self._find_retryable_db_error(e)

                                _logger.warning(
                                    "Chunk %s caught exception=%s, retryable=%s, chain=%s",
                                    i,
                                    type(e).__name__,
                                    (
                                        type(retryable_exc).__name__
                                        if retryable_exc
                                        else "None"
                                    ),
                                    self._summarize_exception_chain(e),
                                )

                                if not retryable_exc:
                                    # Not a retryable error, crash immediately
                                    raise
                                if attempt < max_retries - 1:
                                    wait_time = 2**attempt  # Exponential backoff
                                    _logger.warning(
                                        f"Chunk {i}/{len(chunks)} hit {type(e).__name__}, "
                                        f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                                    )
                                    time.sleep(wait_time)
                                    # Resubmit the chunk
                                    current_future = executor.submit(
                                        self._process_chunk_static,
                                        self.ctx.env.cr.dbname,
                                        self.ctx.env.uid,
                                        dict(self.ctx.env.context),
                                        self.importer._name,
                                        chunk,
                                        self.pipeline.target_model,
                                    )
                                else:
                                    # Max retries exceeded
                                    raise
            except Exception:
                _logger.error("Multiprocessing execution failed", exc_info=True)
                raise
            finally:
                multiprocessing.set_start_method(start_method, force=True)

    @staticmethod
    def _iter_exception_chain(exc: BaseException):
        """Yield an exception and its chained causes/contexts without looping."""

        visited = set()
        current: Optional[BaseException] = exc
        while current and id(current) not in visited:
            visited.add(id(current))
            yield current
            current = current.__cause__ or current.__context__

    def _find_retryable_db_error(self, exc: BaseException):
        """Return the first retryable psycopg2 error in the exception chain."""

        retryable = (
            psycopg2.errors.SerializationFailure,
            psycopg2.errors.DeadlockDetected,
            psycopg2.errors.InFailedSqlTransaction,
            psycopg2.errors.ActiveSqlTransaction,
            psycopg2.extensions.TransactionRollbackError,
            psycopg2.OperationalError,
            psycopg2.InternalError,
            psycopg2.DatabaseError,
        )
        for chained_exc in self._iter_exception_chain(exc):
            if isinstance(chained_exc, retryable):
                return chained_exc
        return None

    def _summarize_exception_chain(self, exc: BaseException) -> str:
        """Return a short string describing the exception chain."""

        parts = [type(chained).__name__ for chained in self._iter_exception_chain(exc)]
        return " -> ".join(parts)

    def _create_chunks(self, extracted_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Split extracted data into chunks for parallel processing.

        Args:
            extracted_data: Dictionary of extraction results.

        Returns:
            List of extracted_data dicts, each with chunked primary data.
        """
        chunk_size = self.pipeline.multiprocessing.chunk_size

        # Handle (records, chunks) tuple from extraction
        for key, data in extracted_data.items():
            if isinstance(data, tuple) and len(data) == 2:
                # Already chunked by extraction method
                # Return full extracted_data dict for each chunk
                return [
                    {
                        key: chunk,
                        **{k: v for k, v in extracted_data.items() if k != key},
                    }
                    for chunk in data[1]
                ]

        # Handle ChunkableData dataclass (preferred API)
        for key, data in extracted_data.items():
            if isinstance(data, ChunkableData):
                records = data.records
                chunks = []
                for i in range(0, len(records), chunk_size):
                    chunk_dict = extracted_data.copy()
                    chunk_dict[key] = ChunkableData(
                        records=records[i : i + chunk_size],
                        context=data.context,
                    )
                    chunks.append(chunk_dict)
                return chunks

        # Look for chunkable keys in nested dicts (deprecated, use ChunkableData)
        # Priority: 'records' key, 'headers' (backward compat)
        for key, data in extracted_data.items():
            if isinstance(data, dict):
                # Check for 'records' key
                if "records" in data and isinstance(data["records"], list):
                    records = data["records"]
                    chunks = []
                    for i in range(0, len(records), chunk_size):
                        chunk_dict = extracted_data.copy()
                        chunk_dict[key] = {
                            **data,
                            "records": records[i : i + chunk_size],
                        }
                        chunks.append(chunk_dict)
                    return chunks
                # Check for 'headers' key (backward compatibility)
                if "headers" in data and isinstance(data["headers"], list):
                    headers = data["headers"]
                    chunks = []
                    for i in range(0, len(headers), chunk_size):
                        chunk_dict = extracted_data.copy()
                        chunk_dict[key] = {
                            **data,
                            "headers": headers[i : i + chunk_size],
                        }
                        chunks.append(chunk_dict)
                    return chunks

        # Otherwise, chunk the first list we find
        for key, data in extracted_data.items():
            if isinstance(data, list):
                # Create chunks with full extracted_data, but chunk this specific list
                chunks = []
                for i in range(0, len(data), chunk_size):
                    chunk_dict = extracted_data.copy()
                    chunk_dict[key] = data[i : i + chunk_size]
                    chunks.append(chunk_dict)
                return chunks

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
        from odoo.modules.registry import Registry
        from odoo.tools import mute_logger

        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, uid, context)
            importer = env[importer_name]
            pipeline = importer._etl_pipeline  # type: ignore[attr-defined]

            ctx = ETLContext(cr=None, env=env)

            # Transform
            extracted_dict = chunk
            transformed_data = {}
            for method in pipeline.transform_methods:
                bound = getattr(importer, method.func.__name__)
                result = bound(ctx, extracted_dict)
                transformed_data[method.func.__name__] = result

            # Load
            for method in pipeline.load_methods:
                bound = getattr(importer, method.func.__name__)
                bound(ctx, transformed_data)

            cr.commit()


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
        source_config: Optional source-specific configuration dictionary.
        module_filter: Optional Odoo addon module name to filter pipelines.
    """

    def __init__(
        self,
        env: Any,
        source_config: Optional[Dict[str, Any]] = None,
        module_filter: Optional[str] = None,
    ):
        """Initialize the orchestrator.

        Args:
            env: Odoo environment.
            source_config: Optional dictionary of source-specific configuration.
            module_filter: Optional Odoo addon module name to filter pipelines.
                          If provided, only pipelines from this module will be executed.
        """
        self.env = env
        self.source_config = source_config
        self.module_filter = module_filter
        if module_filter:
            self.pipelines = ETL.get_pipelines_by_module(module_filter)
        else:
            self.pipelines = ETL.get_all_pipelines()

    def execute_all(self, cr: Any) -> None:
        """Execute all registered pipelines in dependency order.

        Args:
            cr: Source database cursor.
        """
        _logger.info("Starting ETL orchestration for all pipelines")

        # Resolve execution order
        execution_order = self._resolve_dependencies()
        _logger.info(f"Execution order: {execution_order}")

        # Create reporter and context
        reporter = ETLReporter(self.env)
        reporter.start_run()
        ctx = ETLContext(
            cr=cr,
            env=self.env,
            source_config=self.source_config,
            _reporter=reporter,
        )

        run_state = "done"
        try:
            self._run_pipelines(execution_order, ctx)
        except Exception:
            run_state = "failed"
            raise
        finally:
            reporter.end_run(state=run_state)

        _logger.info("Completed ETL orchestration for all pipelines")

    def execute_pipelines(self, cr: Any, pipeline_names: List[str]) -> None:
        """Execute specific pipelines in dependency order.

        Args:
            cr: Source database cursor.
            pipeline_names: List of importer names to execute.
        """
        _logger.info(f"Starting ETL orchestration for pipelines: {pipeline_names}")

        # Filter to only requested pipelines and resolve their dependencies
        execution_order = self._resolve_dependencies_for(pipeline_names)
        _logger.info(f"Execution order: {execution_order}")

        # Create reporter and context
        reporter = ETLReporter(self.env)
        reporter.start_run()
        ctx = ETLContext(
            cr=cr,
            env=self.env,
            source_config=self.source_config,
            _reporter=reporter,
        )

        run_state = "done"
        try:
            self._run_pipelines(execution_order, ctx)
        except Exception:
            run_state = "failed"
            raise
        finally:
            reporter.end_run(state=run_state)

        _logger.info(
            f"Completed ETL orchestration for {len(execution_order)} pipelines"
        )

    def _run_pipelines(self, execution_order: List[str], ctx: ETLContext) -> None:
        """Execute pipelines in the given order.

        Args:
            execution_order: List of importer names in execution order.
            ctx: ETL context (with reporter attached).
        """
        for importer_name in execution_order:
            pipeline = self.pipelines.get(importer_name)
            if not pipeline:
                _logger.warning(f"Pipeline for {importer_name} not found, skipping")
                continue

            # Get importer instance using the importer name
            importer = self.env[importer_name]

            _logger.info(
                f"Starting ETL pipeline for {pipeline.target_model} "
                f"(importer: {importer_name})"
            )

            # Execute pipeline
            executor = ETLExecutor(pipeline, ctx, importer)
            executor.execute()

            # Commit after each pipeline
            self.env.cr.commit()

    def _resolve_dependencies(self) -> List[str]:
        """Resolve pipeline dependencies using topological sort.

        Returns:
            List of importer names in execution order.

        Raises:
            ValueError: If circular dependencies are detected.
        """
        # Build dependency graph (importer_name -> depends_on list)
        graph = {
            importer_name: pipeline.depends_on
            for importer_name, pipeline in self.pipelines.items()
        }

        # Topological sort (Kahn's algorithm)
        # in_degree[X] = number of dependencies X has (how many nodes must run before X)
        in_degree = {importer_name: len(deps) for importer_name, deps in graph.items()}

        # Start with nodes that have no dependencies
        queue = [
            importer_name for importer_name, degree in in_degree.items() if degree == 0
        ]
        result = []

        while queue:
            importer_name = queue.pop(0)
            result.append(importer_name)

            # For each node that depends on the current node, decrement its in-degree
            for other_importer, deps in graph.items():
                if importer_name in deps:
                    in_degree[other_importer] -= 1
                    if in_degree[other_importer] == 0:
                        queue.append(other_importer)

        if len(result) != len(graph):
            # Find which pipelines are involved in the cycle
            unresolved = set(graph.keys()) - set(result)
            cycle_info = []
            for pipeline in unresolved:
                deps = graph.get(pipeline, set())
                cycle_info.append(f"  {pipeline} depends on: {', '.join(sorted(deps))}")

            error_msg = (
                f"Circular dependency detected in ETL pipelines.\n"
                f"Unresolved pipelines ({len(unresolved)}):\n" + "\n".join(cycle_info)
            )
            raise ValueError(error_msg)

        return result

    def _resolve_dependencies_for(self, pipeline_names: List[str]) -> List[str]:
        """Resolve dependencies for specific pipelines using topological sort.

        Only includes the requested pipelines and their dependencies in the result.

        Args:
            pipeline_names: List of importer names to resolve dependencies for.

        Returns:
            List of importer names in execution order (including dependencies).

        Raises:
            ValueError: If circular dependencies are detected.
        """
        # First, collect all required pipelines (requested + their dependencies)
        required = set(pipeline_names)
        to_check = list(pipeline_names)

        while to_check:
            name = to_check.pop()
            pipeline = self.pipelines.get(name)
            if pipeline:
                for dep in pipeline.depends_on:
                    if dep not in required:
                        required.add(dep)
                        to_check.append(dep)

        # Build dependency graph for only required pipelines
        graph = {}
        for importer_name in required:
            pipeline = self.pipelines.get(importer_name)
            if pipeline:
                # Only include dependencies that are in our required set
                graph[importer_name] = [d for d in pipeline.depends_on if d in required]
            else:
                graph[importer_name] = []

        # Topological sort (Kahn's algorithm)
        in_degree = {name: len(deps) for name, deps in graph.items()}
        queue = [name for name, degree in in_degree.items() if degree == 0]
        result = []

        while queue:
            importer_name = queue.pop(0)
            result.append(importer_name)

            for other_importer, deps in graph.items():
                if importer_name in deps:
                    in_degree[other_importer] -= 1
                    if in_degree[other_importer] == 0:
                        queue.append(other_importer)

        if len(result) != len(graph):
            unresolved = set(graph.keys()) - set(result)
            cycle_info = [
                f"  {p} depends on: {', '.join(sorted(graph.get(p, [])))}"
                for p in unresolved
            ]
            raise ValueError(
                f"Circular dependency detected.\n"
                f"Unresolved: {len(unresolved)}:\n" + "\n".join(cycle_info)
            )

        return result
