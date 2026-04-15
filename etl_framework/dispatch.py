"""HTTP chunk dispatcher for ETL parallel execution.

Sends extracted data chunks to Odoo HTTP workers via POST requests to
``/etl/process_chunk``.  The orchestrator (running in one worker) is
I/O-bound while waiting for responses, so we use a ThreadPoolExecutor
for concurrency (GIL is irrelevant for I/O waits).
"""

import base64
import json
import logging
import pickle
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

_logger = logging.getLogger(__name__)

# Serialization failures and deadlocks are transient — the chunk can be
# retried and will likely succeed.  We detect these from the structured
# error response (error_type field), not from psycopg2 exception types.
_RETRYABLE_ERROR_TYPES = frozenset(
    {
        "SerializationFailure",
        "DeadlockDetected",
        "TransactionRollbackError",
    }
)

_MAX_RETRIES = 10


@dataclass
class ChunkResult:
    """Result from processing a single chunk."""

    status: str  # "ok" or "error"
    success_count: int = 0
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    traceback: Optional[str] = None


class ChunkDispatcher:
    """Dispatches ETL chunks to Odoo HTTP workers for parallel processing.

    Args:
        base_url: Base URL of the Odoo instance (e.g. "http://localhost:8069").
        api_key: Bearer token for authentication.
        timeout: HTTP request timeout in seconds per chunk.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 600,
        pool_size: int = 64,
        dbname: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.dbname = dbname

        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        # Size the connection pool to handle all concurrent chunk requests
        # without "Connection pool is full" warnings from urllib3.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_size, pool_maxsize=pool_size
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def _post_chunk(
        self,
        importer_name: str,
        chunk: Dict[str, Any],
        source_config: Optional[Dict[str, Any]],
        chunk_index: int,
        total_chunks: int,
    ) -> ChunkResult:
        """Send a single chunk to the endpoint, with retry on transient errors."""
        tag = f"[{importer_name}] Chunk {chunk_index + 1}/{total_chunks}"
        url = f"{self.base_url}/etl/process_chunk"
        if self.dbname:
            url += f"?db={self.dbname}"
        payload = {
            "importer_name": importer_name,
            "chunk": base64.b64encode(pickle.dumps(chunk, protocol=5)).decode(),
        }
        if source_config:
            payload["source_config"] = source_config

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.post(
                    url,
                    data=json.dumps(payload),
                    timeout=self.timeout,
                )
            except requests.ConnectionError as e:
                _logger.error("%s: connection error: %s", tag, e)
                return ChunkResult(
                    status="error",
                    error_type="ConnectionError",
                    error_message=str(e),
                )
            except requests.Timeout as e:
                _logger.error("%s: request timed out: %s", tag, e)
                return ChunkResult(
                    status="error",
                    error_type="Timeout",
                    error_message=str(e),
                )

            if resp.status_code == 200:
                body = resp.json()
                return ChunkResult(
                    status=body.get("status", "ok"),
                    success_count=body.get("success_count", 0),
                    warnings=body.get("warnings", []),
                    failures=body.get("failures", []),
                )

            # Error response — check if retryable
            try:
                body = resp.json()
            except ValueError:
                body = {
                    "error_type": "HTTPError",
                    "error_message": resp.text[:500],
                }

            error_type = body.get("error_type", "")
            if error_type in _RETRYABLE_ERROR_TYPES and attempt < _MAX_RETRIES - 1:
                wait_time = min(2**attempt, 30)
                _logger.warning(
                    "%s: %s (retrying in %ds, attempt %d/%d)",
                    tag,
                    error_type,
                    wait_time,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait_time)
                continue

            # Non-retryable or final attempt
            return ChunkResult(
                status="error",
                error_type=body.get("error_type", f"HTTP {resp.status_code}"),
                error_message=body.get("error_message", resp.text[:500]),
                traceback=body.get("traceback"),
            )

        # Should not be reached, but just in case
        return ChunkResult(status="error", error_message="Max retries exceeded")

    def dispatch_chunks(
        self,
        chunks: List[Dict[str, Any]],
        importer_name: str,
        source_config: Optional[Dict[str, Any]] = None,
        max_workers: int = 3,
    ) -> List[ChunkResult]:
        """Dispatch all chunks in parallel and return aggregated results.

        Args:
            chunks: List of extracted data chunks.
            importer_name: Odoo model name of the ETL importer.
            source_config: Optional source configuration dict.
            max_workers: Max concurrent HTTP requests.

        Returns:
            List of ChunkResult, one per chunk (in order).
        """
        results: list[ChunkResult] = [
            ChunkResult(status="pending") for _ in chunks
        ]

        _logger.info(
            "Dispatching %d chunks to %d workers for %s",
            len(chunks),
            max_workers,
            importer_name,
        )

        total = len(chunks)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_index = {
                pool.submit(
                    self._post_chunk,
                    importer_name,
                    chunk,
                    source_config,
                    i,
                    total,
                ): i
                for i, chunk in enumerate(chunks)
            }

            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                tag = f"[{importer_name}] Chunk {idx + 1}/{total}"
                try:
                    results[idx] = future.result()
                except Exception as e:
                    _logger.error("%s raised unexpected error: %s", tag, e)
                    results[idx] = ChunkResult(
                        status="error",
                        error_type=type(e).__name__,
                        error_message=str(e),
                    )
                _logger.info("%s completed (%s)", tag, results[idx].status)

        return results

    def close(self):
        """Close the underlying HTTP session."""
        self._session.close()
