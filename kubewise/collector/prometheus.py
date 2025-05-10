# -*- coding: utf-8 -*-
import asyncio
import datetime
import math
import time
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Set, Tuple, TypeVar

import httpx
from loguru import logger

from kubewise.config import settings
from kubewise.models import MetricPoint

# Import utility decorators after they are defined or imported elsewhere
from kubewise.utils.retry import with_exponential_backoff, with_circuit_breaker

T = TypeVar("T")

# HTTP Client Configuration
DEFAULT_TIMEOUT = 30.0  # seconds
DEFAULT_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=30)
RESPONSE_BODY_LIMIT = 100 * 1024 * 1024  # 100MB Max response size

# Default Polling Intervals
METRICS_POLL_INTERVAL = 60.0  # seconds
DEFAULT_HEALTH_CHECK_INTERVAL = 300.0  # seconds


class PrometheusFetcher:
    """
    Handles fetching metrics from a Prometheus instance periodically.

    Connects to the Prometheus API, executes configured PromQL queries,
    parses the results into MetricPoint objects, and places them onto
    an asyncio Queue for further processing. Includes health checking and
    basic query failure management.
    """

    def __init__(
        self,
        metrics_queue: asyncio.Queue[MetricPoint],
        http_client: httpx.AsyncClient,
        prometheus_url: Optional[str] = None,
        metrics_queries: Optional[Dict[str, str]] = None,
        poll_interval: float = METRICS_POLL_INTERVAL,
        health_check_interval: float = DEFAULT_HEALTH_CHECK_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        Initializes the PrometheusFetcher.

        Args:
            metrics_queue: asyncio Queue to publish fetched MetricPoints.
            http_client: An httpx.AsyncClient instance for making requests.
            prometheus_url: URL of the Prometheus server API. Uses settings if None.
            metrics_queries: Dictionary mapping metric names to PromQL queries. Uses settings if None.
            poll_interval: Interval between metric polling cycles in seconds.
            health_check_interval: Interval between Prometheus health checks in seconds.
            timeout: Request timeout for individual Prometheus API calls.
        """
        self.metrics_queue = metrics_queue
        self._client = http_client

        # Ensure prometheus_url is a string before applying string operations
        prom_url = prometheus_url or settings.prom_url
        self.prometheus_url = str(prom_url).rstrip("/")

        self.poll_interval = poll_interval
        self.health_check_interval = health_check_interval
        self.timeout = timeout
        self.metrics_queries = metrics_queries or settings.prom_queries

        self._is_running = False
        self._exit_stack = AsyncExitStack()
        self._polling_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._last_successful_poll: Optional[float] = None

        # Internal statistics
        self.polls_completed = 0
        self.metrics_fetched = 0
        self.successful_polls = 0
        self.failed_polls = 0

        # Query failure tracking
        self._query_failure_counts: Dict[str, int] = {}
        self._max_failures_before_disable = 5  # Consecutive failures to disable a query
        self._disabled_queries: Set[str] = set()

        logger.info(
            f"Initialized PrometheusFetcher: URL='{self.prometheus_url}', Queries={len(self.metrics_queries)}, Interval={poll_interval}s"
        )

    async def start(self) -> None:
        """Starts the background metric polling and health checking tasks."""
        if self._is_running:
            logger.warning("PrometheusFetcher is already running.")
            return

        logger.info(f"Starting PrometheusFetcher targeting {self.prometheus_url}")
        # Manage the HTTP client lifecycle using AsyncExitStack
        await self._exit_stack.enter_async_context(self._client)

        self._is_running = True
        self._polling_task = asyncio.create_task(
            self._metrics_polling_loop(), name="prometheus_poller"
        )
        self._health_check_task = asyncio.create_task(
            self._health_check_loop(), name="prometheus_health_checker"
        )
        logger.info("PrometheusFetcher started.")

    async def stop(self) -> None:
        """Stops the background tasks and cleans up resources."""
        if not self._is_running:
            logger.debug("PrometheusFetcher not running.")
            return

        logger.info("Stopping PrometheusFetcher...")
        self._is_running = False

        # Gracefully cancel background tasks
        for task in [self._polling_task, self._health_check_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug(f"Task {task.get_name()} cancelled.")
                except Exception as e:
                    logger.error(
                        f"Error during task {task.get_name()} cancellation: {e}"
                    )

        # Close the HTTP client via the exit stack
        await self._exit_stack.aclose()
        self._client = None  # Ensure client is cleared
        logger.info("PrometheusFetcher stopped.")

    async def _metrics_polling_loop(self) -> None:
        """Periodically fetches metrics and puts them on the queue."""
        logger.info(f"Starting metrics polling loop (interval: {self.poll_interval}s).")
        while self._is_running:
            start_time = time.monotonic()
            try:
                metrics = await self._fetch_all_metrics()
                for metric in metrics:
                    await self.metrics_queue.put(metric)

                self.polls_completed += 1
                self.metrics_fetched += len(metrics)
                self.successful_polls += 1
                self._last_successful_poll = time.monotonic()

                elapsed = time.monotonic() - start_time
                sleep_time = max(0.1, self.poll_interval - elapsed)
                logger.debug(
                    f"Poll cycle completed: Fetched {len(metrics)} metrics in {elapsed:.2f}s. Next poll in {sleep_time:.2f}s."
                )
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                logger.info("Metrics polling loop cancelled.")
                break
            except Exception as e:
                self.failed_polls += 1
                logger.exception(f"Error in metrics polling loop: {e}")
                await asyncio.sleep(
                    min(self.poll_interval, 5.0)
                )  # Short backoff on error

    async def _health_check_loop(self) -> None:
        """Periodically checks Prometheus API health."""
        logger.info(
            f"Starting Prometheus health check loop (interval: {self.health_check_interval}s)."
        )
        while self._is_running:
            try:
                await asyncio.sleep(self.health_check_interval)
                # Skip check if a poll succeeded recently
                if self._last_successful_poll and (
                    time.monotonic() - self._last_successful_poll
                    < self.health_check_interval
                ):
                    logger.debug("Skipping health check due to recent successful poll.")
                    continue

                is_healthy = await self._check_prometheus_health()

                # If unhealthy, attempt to recreate the HTTP client as connections might be stale/broken.
                if not is_healthy and self._client:
                    logger.warning(
                        "Prometheus health check failed. Recreating HTTP client."
                    )
                    # Optionally update external service status metrics here
                    # try:
                    #     from kubewise.api.context import SERVICE_UP
                    #     SERVICE_UP.labels(service="prometheus").set(0)
                    # except (ImportError, NameError): pass

                    await (
                        self._exit_stack.aclose()
                    )  # Close existing client managed by stack
                    self._client = httpx.AsyncClient(
                        timeout=self.timeout,
                        limits=DEFAULT_LIMITS,
                        http2=False,
                        transport=httpx.AsyncHTTPTransport(retries=1),
                    )
                    await self._exit_stack.enter_async_context(
                        self._client
                    )  # Register new client
                    logger.info("Recreated HTTP client after failed health check.")

            except asyncio.CancelledError:
                logger.info("Health check loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(5.0)  # Wait briefly after an error

    @with_exponential_backoff(
        max_retries=2, initial_delay=0.5
    )  # Fewer retries for health check
    async def _check_prometheus_health(self) -> bool:
        """Performs a basic connectivity and query check against Prometheus."""
        if not self._client:
            return False
        try:
            # 1. Basic connectivity check
            await self._client.head(f"{self.prometheus_url}/-/healthy", timeout=5.0)
            # 2. Simple query check
            data = await self._query_prometheus("vector(1)")  # Minimal query
            return data and data.get("status") == "success"
        except Exception as e:
            logger.error(f"Prometheus health check failed: {repr(e)}")
            return False

    async def _fetch_all_metrics(self) -> List[MetricPoint]:
        """Fetches results for all active PromQL queries concurrently."""
        if not self._client:
            return []

        active_queries = {
            name: query
            for name, query in self.metrics_queries.items()
            if name not in self._disabled_queries
        }
        if not active_queries:
            logger.warning("No active Prometheus queries to fetch.")
            return []

        logger.debug(f"Fetching {len(active_queries)} active Prometheus queries...")
        tasks = [
            self._fetch_single_metric(name, query)
            for name, query in active_queries.items()
        ]
        start_time = time.monotonic()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.monotonic() - start_time

        all_metrics: List[MetricPoint] = []
        success_count, error_count, queries_with_data = 0, 0, 0

        # Process results and manage query failures
        for i, result in enumerate(results):
            query_name = list(active_queries.keys())[i]
            if isinstance(result, Exception):
                error_count += 1
                logger.error(f"Query '{query_name}' failed: {result}")
                # Track consecutive failures to potentially disable the query
                self._query_failure_counts[query_name] = (
                    self._query_failure_counts.get(query_name, 0) + 1
                )
                if (
                    self._query_failure_counts[query_name]
                    >= self._max_failures_before_disable
                ):
                    logger.warning(
                        f"Disabling query '{query_name}' due to {self._max_failures_before_disable} consecutive failures."
                    )
                    self._disabled_queries.add(query_name)
            else:
                success_count += 1
                self._query_failure_counts[query_name] = (
                    0  # Reset failure count on success
                )
                if result:
                    _, metrics_list, _ = result
                    valid_metrics = [
                        m for m in metrics_list if math.isfinite(m.value)
                    ]  # Filter NaN/Inf
                    if valid_metrics:
                        all_metrics.extend(valid_metrics)
                        queries_with_data += 1
                    elif metrics_list:  # Log if filtering removed all metrics
                        logger.warning(
                            f"Query '{query_name}' returned only non-finite values."
                        )

        logger.debug(
            f"Query cycle completed in {elapsed:.2f}s: {success_count}/{len(active_queries)} queries succeeded, "
            f"{queries_with_data} returned data ({len(all_metrics)} total metrics). "
            f"{len(self._disabled_queries)} queries disabled."
        )
        return all_metrics

    @with_exponential_backoff(max_retries=3)
    async def _fetch_single_metric(
        self, name: str, query: str
    ) -> Tuple[str, List[MetricPoint], Any]:
        """Fetches and parses results for a single PromQL query."""
        logger.debug(f"Querying metric '{name}'...")
        data = await self._query_prometheus(query)
        metrics = self._parse_prometheus_response(name, data) if data else []
        # Handle fallback queries (e.g., `... or vector(0)`) returning empty results
        if not metrics and "vector(0)" in query:
            logger.debug(
                f"Query '{name}' used fallback 'vector(0)', creating default 0 value."
            )
            metrics.append(
                MetricPoint(
                    metric_name=name,
                    labels={"source": "fallback"},
                    value=0.0,
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                )
            )
        return name, metrics, data

    @with_circuit_breaker(
        service_name="prometheus"
    )  # Apply circuit breaker to Prometheus calls
    async def _query_prometheus(self, query: str) -> Optional[Dict[str, Any]]:
        """Executes a PromQL query against the Prometheus API."""
        if not self._client:
            raise RuntimeError("HTTP client not initialized")
        prometheus_query_url = f"{self.prometheus_url}/api/v1/query"
        params = {"query": query}
        try:
            response = await self._client.get(
                prometheus_query_url, params=params, timeout=self.timeout
            )
            response.raise_for_status()  # Raises HTTPStatusError for 4xx/5xx
            data = response.json()
            if data.get("status") != "success":
                logger.error(
                    f"Prometheus query failed: Status='{data.get('status')}', Error='{data.get('error', 'N/A')}', Type='{data.get('errorType', 'N/A')}'"
                )
                return None
            return data
        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP error querying Prometheus: {e.response.status_code} {e.response.reason_phrase}"
            )
            raise  # Re-raise to be handled by retry/circuit breaker decorators
        except httpx.RequestError as e:
            logger.error(f"Connection error querying Prometheus: {type(e).__name__}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error querying Prometheus: {e}")
            raise

    def _parse_prometheus_response(
        self, query_name: str, data: Dict
    ) -> List[MetricPoint]:
        """Parses a Prometheus API JSON response into MetricPoint objects."""
        metrics: List[MetricPoint] = []
        if not data or data.get("status") != "success" or "data" not in data:
            return metrics
        result_data = data["data"]
        result_type = result_data.get("resultType")
        results = result_data.get("result", [])

        if not results:
            # Log only if not expecting empty result (i.e., no 'vector(0)' fallback)
            if "vector(0)" not in self.metrics_queries.get(query_name, ""):
                logger.debug(f"Query '{query_name}' returned empty result set.")
            return metrics

        try:
            for item in results:
                labels = item.get("metric", {})
                if result_type == "vector":
                    value_pair = item.get("value")
                    if value_pair and len(value_pair) == 2:
                        ts_val, val_str = value_pair
                        try:
                            value = float(val_str)
                            # Skip non-finite values which can break models
                            if not math.isfinite(value):
                                logger.debug(
                                    f"Skipping non-finite value for {query_name}{labels}: {val_str}"
                                )
                                continue
                            ts = datetime.datetime.fromtimestamp(
                                float(ts_val), tz=datetime.timezone.utc
                            )
                            metrics.append(
                                MetricPoint(
                                    metric_name=query_name,
                                    labels=labels,
                                    value=value,
                                    timestamp=ts,
                                )
                            )
                        except (ValueError, TypeError) as e:
                            logger.warning(
                                f"Error parsing vector value for {query_name}: {e}, value pair: {value_pair}"
                            )
                elif result_type == "matrix":
                    value_pairs = item.get("values", [])
                    for ts_val, val_str in value_pairs:
                        try:
                            value = float(val_str)
                            if not math.isfinite(value):
                                continue
                            ts = datetime.datetime.fromtimestamp(
                                float(ts_val), tz=datetime.timezone.utc
                            )
                            metrics.append(
                                MetricPoint(
                                    metric_name=query_name,
                                    labels=labels,
                                    value=value,
                                    timestamp=ts,
                                )
                            )
                        except (ValueError, TypeError) as e:
                            logger.warning(
                                f"Error parsing matrix value for {query_name}: {e}, value pair: {(ts_val, val_str)}"
                            )
                # Note: 'scalar' and 'string' result types are generally not processed into MetricPoints
        except Exception as e:
            logger.exception(
                f"Unexpected error parsing Prometheus response for '{query_name}': {e}"
            )

        logger.debug(f"Parsed {len(metrics)} data points for '{query_name}'.")
        return metrics


# --- Legacy API Wrapper (for potential backward compatibility) ---


class PrometheusClient:
    """Legacy wrapper providing an async context manager for httpx.AsyncClient."""

    def __init__(
        self,
        prometheus_url: Optional[str] = None,
        limits: httpx.Limits = DEFAULT_LIMITS,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.prometheus_url = (prometheus_url or str(settings.prom_url)).rstrip("/")
        self.limits = limits
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> httpx.AsyncClient:
        """Initializes and returns the httpx client."""
        self._client = httpx.AsyncClient(
            limits=self.limits,
            timeout=self.timeout,
            http2=False,
            transport=httpx.AsyncHTTPTransport(retries=2),
        )
        await self._stack.enter_async_context(self._client)
        return self._client

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Cleans up the httpx client."""
        await self._stack.aclose()
        self._client = None


# Keep legacy functions but clearly mark them and delegate to new methods if possible
@with_exponential_backoff(max_retries=3)
async def query_prometheus(
    client: httpx.AsyncClient, query: str, prometheus_url: Optional[str] = None
) -> Optional[Dict]:
    """
    Legacy query function. Use PrometheusFetcher._query_prometheus for new code.
    Queries Prometheus API with retries.
    """
    logger.warning(
        "Using legacy query_prometheus function. Consider migrating to PrometheusFetcher."
    )
    base_url = (prometheus_url or str(settings.prom_url)).rstrip("/")
    url = f"{base_url}/api/v1/query"
    params = {"query": query}
    try:
        response = await client.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "success":
            return data
        logger.error(
            f"Legacy query failed: Status='{data.get('status')}', Error='{data.get('error')}'"
        )
    except Exception as e:
        logger.error(f"Error in legacy query_prometheus: {e}")
        raise
    return None


def parse_prometheus_response(query_name: str, data: Dict) -> List[MetricPoint]:
    """
    Legacy parsing function. Use PrometheusFetcher._parse_prometheus_response.
    Parses Prometheus API response.
    """
    logger.warning(
        "Using legacy parse_prometheus_response function. Consider migrating to PrometheusFetcher."
    )
    # Create a temporary fetcher instance to reuse parsing logic
    temp_fetcher = PrometheusFetcher(
        asyncio.Queue(), httpx.AsyncClient()
    )  # Client won't be used here
    return temp_fetcher._parse_prometheus_response(query_name, data)
