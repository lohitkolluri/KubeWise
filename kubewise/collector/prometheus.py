import json
import os
import asyncio
import datetime
import math
import random
import time
from contextlib import AsyncExitStack
from functools import wraps
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, TypeVar, Union, Generator

import httpx
from loguru import logger
from motor import motor_asyncio
from pydantic import create_model, Field

from kubewise.config import settings
from kubewise.models import MetricPoint
from kubewise.utils.retry import with_exponential_backoff, with_circuit_breaker # Import from utility file

# --- Type Variables for Generic Functions ---
T = TypeVar('T')

# --- Connection Configuration ---
DEFAULT_TIMEOUT = 30.0  # seconds
DEFAULT_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=30)
RESPONSE_BODY_LIMIT = 100 * 1024 * 1024  # 100MB

# --- Monitoring Constants ---
METRICS_POLL_INTERVAL = 60.0  # seconds
DEFAULT_HEALTH_CHECK_INTERVAL = 300.0  # seconds

# --- Utility Functions ---

class PrometheusFetcher:
    """
    Fetches metrics from Prometheus and places them in a queue for processing.
    
    This class handles connection to Prometheus API, periodic polling,
    and parsing of results into MetricPoint objects for anomaly detection.
    """
    
    def __init__(
        self,
        metrics_queue: asyncio.Queue[MetricPoint],
        http_client: httpx.AsyncClient,
        prometheus_url: Optional[str] = None,
        metrics_queries: Optional[Dict[str, str]] = None,
        poll_interval: float = METRICS_POLL_INTERVAL,
        health_check_interval: float = DEFAULT_HEALTH_CHECK_INTERVAL,
        timeout: float = 30.0,  # Default timeout for Prometheus API requests
    ):
        """
        Initialize the Prometheus fetcher.
        
        Args:
            metrics_queue: Queue to put metrics into
            http_client: Shared HTTP client to use for requests
            prometheus_url: URL of the Prometheus API
            metrics_queries: Dict of metric name to PromQL query
            poll_interval: How often to fetch metrics in seconds
            health_check_interval: How often to check connection health
            timeout: Timeout in seconds for Prometheus API requests
        """
        self.metrics_queue = metrics_queue
        self._client = http_client
        self.prometheus_url = prometheus_url or settings.prom_url
        # Strip trailing slash if present
        if isinstance(self.prometheus_url, str):
            self.prometheus_url = self.prometheus_url.rstrip('/')
            
        self.poll_interval = poll_interval
        self.health_check_interval = health_check_interval
        self.timeout = timeout  # Store the timeout value
        
        # Initialize with default queries if none provided
        self.metrics_queries = metrics_queries or settings.prom_queries
        
        # State tracking
        self._is_running = False
        self._exit_stack = AsyncExitStack()
        self._polling_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._last_successful_poll: Optional[float] = None
        
        # Metrics for monitoring
        self.connections_established = 0
        self.connection_errors = 0
        self.polls_completed = 0
        self.metrics_fetched = 0
        self.successful_polls = 0  # Initialize counter
        self.failed_polls = 0      # Initialize counter
        
        # Metrics query failure tracking
        self._query_failure_counts: Dict[str, int] = {}
        self._max_failures_before_disable = 5  # Disable a query after this many consecutive failures
        self._disabled_queries: Set[str] = set()  # Track disabled queries
        
        logger.info(f"Initialized PrometheusFetcher with {len(self.metrics_queries)} queries, poll_interval={poll_interval}s")
        
    async def start(self) -> None:
        """Start the metrics poller with proper connection setup."""
        if self._is_running:
            logger.warning("PrometheusFetcher is already running")
            return
            
        logger.info(f"Starting Prometheus metrics fetcher (URL: {self.prometheus_url})")
        
        # Register client for cleanup
        await self._exit_stack.enter_async_context(self._client)
        
        # Start background tasks
        self._is_running = True
        self._polling_task = asyncio.create_task(
            self._metrics_polling_loop(),
            name="prometheus_metrics_poller"
        )
        self._health_check_task = asyncio.create_task(
            self._health_check_loop(), 
            name="prometheus_health_checker"
        )
        
        logger.info("Prometheus metrics fetcher started successfully")
    
    async def stop(self) -> None:
        """Stop the metrics poller and clean up connections."""
        if not self._is_running:
            logger.debug("PrometheusFetcher is not running")
            return
            
        logger.info("Stopping Prometheus metrics fetcher")
        self._is_running = False
        
        # Cancel tasks
        for task in [self._polling_task, self._health_check_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Close the exit stack (which will close the client)
        await self._exit_stack.aclose()
        self._client = None
        
        logger.info("Prometheus metrics fetcher stopped")
        
    async def _metrics_polling_loop(self) -> None:
        """Background task that periodically polls for metrics."""
        logger.info(f"Starting metrics polling loop every {self.poll_interval}s")
        
        while self._is_running:
            try:
                start_time = time.monotonic()
                
                # Fetch metrics and put them on the queue
                metrics = await self._fetch_all_metrics()
                for metric in metrics:
                    await self.metrics_queue.put(metric)
                
                # Update statistics
                self.metrics_fetched += len(metrics)
                self.successful_polls += 1
                self._last_successful_poll = time.monotonic()
                
                # Calculate sleep time to maintain consistent intervals
                elapsed = time.monotonic() - start_time
                sleep_time = max(0.1, self.poll_interval - elapsed)
                
                logger.info(
                    f"Fetched {len(metrics)} metrics in {elapsed:.2f}s. "
                    f"Next poll in {sleep_time:.2f}s"
                )
                
                # Wait until next poll interval
                await asyncio.sleep(sleep_time)
                
            except asyncio.CancelledError:
                logger.info("Metrics polling loop cancelled")
                break
                
            except Exception as e:
                self.failed_polls += 1
                logger.exception(f"Error in metrics polling loop: {e}")
                
                # Backoff on failure to avoid overwhelming the server
                await asyncio.sleep(min(self.poll_interval, 5.0))
    
    async def _health_check_loop(self) -> None:
        """Background task that periodically checks Prometheus connection health."""
        logger.info(f"Starting Prometheus health check loop every {self.health_check_interval}s")
        
        while self._is_running:
            try:
                await asyncio.sleep(self.health_check_interval)
                
                # Skip health check if we've had a successful poll recently
                if (self._last_successful_poll and 
                    time.monotonic() - self._last_successful_poll < self.health_check_interval):
                    logger.debug("Skipping health check, recent successful poll")
                    continue
                
                # Simple health check with up metric
                is_healthy = await self._check_prometheus_health()
                
                # If health check failed, recreate the client as it might be in a bad state
                if not is_healthy and self._client:
                    logger.warning("Prometheus health check failed. Recreating HTTP client.")
                    
                    # Update Prometheus service status metric if available
                    try:
                        from kubewise.api.context import SERVICE_UP
                        SERVICE_UP.labels(service="prometheus").set(0)
                        logger.info("Updated Prometheus service status metric to DOWN")
                    except (ImportError, NameError):
                        pass
                    
                    # Perform diagnostic checks
                    logger.info(f"Diagnostic information for Prometheus URL: {self.prometheus_url}")
                    try:
                        import socket
                        from urllib.parse import urlparse
                        parsed_url = urlparse(str(self.prometheus_url))
                        host = parsed_url.hostname
                        if host:
                            try:
                                ip_address = socket.gethostbyname(host)
                                logger.info(f"DNS resolution for {host}: {ip_address}")
                            except socket.gaierror as dns_err:
                                logger.error(f"DNS resolution failed for {host}: {dns_err}")
                    except Exception as diag_err:
                        logger.error(f"Error during diagnostics: {diag_err}")
                    
                    # Close old client
                    old_client = self._client
                    self._client = None
                    await self._exit_stack.aclose()
                    
                    # Initialize a new client with fresh connections
                    self._exit_stack = AsyncExitStack()
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(timeout=self.timeout),
                        limits=httpx.Limits(max_keepalive_connections=20, max_connections=30),
                        http2=False, # Disable HTTP/2 as 'h2' package might not be installed
                        transport=httpx.AsyncHTTPTransport(retries=1)
                    )
                    
                    # Register new client for cleanup
                    await self._exit_stack.enter_async_context(self._client)
                    logger.info("Successfully recreated HTTP client after failed health check")
                
            except asyncio.CancelledError:
                logger.info("Health check loop cancelled")
                break
                
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(5.0)  # Short sleep on error
    
    @with_exponential_backoff(max_retries=3)
    async def _check_prometheus_health(self) -> bool:
        """
        Check if Prometheus is healthy with a simple query.
        
        Returns:
            True if healthy, False otherwise
        """
        if not self._client:
            logger.error("Cannot check health: HTTP client not initialized")
            return False
            
        try:
            # First try a basic connectivity check using a HEAD request
            logger.debug(f"Testing basic connectivity to Prometheus at {self.prometheus_url}")
            try:
                response = await self._client.head(
                    f"{self.prometheus_url}/-/healthy", 
                    timeout=5.0
                )
                response.raise_for_status()
                logger.debug(f"Basic connectivity check succeeded with status code {response.status_code}")
            except Exception as conn_err:
                logger.error(f"Prometheus connectivity check failed: {repr(conn_err)}")
                # Try to get more diagnostic information
                logger.info(f"Prometheus URL: {self.prometheus_url}")
                logger.info(f"Attempting DNS resolution and diagnosis...")
                
                # We don't re-raise this exception because we want to try the query anyway
            
            # Now try a real query as an additional health check
            logger.debug("Performing Prometheus query health check")
            data = await self._query_prometheus("up{job='prometheus'}")
            
            if data and data.get("status") == "success":
                logger.info("Prometheus health check: OK")
                return True
            
            logger.warning(f"Prometheus health check failed: {data}")
            return False
            
        except Exception as e:
            logger.error(f"Prometheus health check error: {repr(e)}")
            return False
    
    async def _fetch_all_metrics(self) -> List[MetricPoint]:
        """
        Fetch all configured metrics concurrently.
        
        Returns:
            List of MetricPoint objects
        """
        if not self._client:
            logger.error("Cannot fetch metrics: HTTP client not initialized")
            return []
            
        all_metrics: List[MetricPoint] = []
        
        # Filter out disabled queries - NEW
        active_queries = {
            name: query for name, query in self.metrics_queries.items()
            if name not in self._disabled_queries
        }
        
        query_count = len(active_queries)
        
        # Skip if no queries defined
        if not query_count:
            logger.warning("No active Prometheus queries defined, skipping fetch")
            return []
            
        logger.debug(f"Fetching {query_count} Prometheus queries concurrently (disabled: {len(self._disabled_queries)})")
        
        # Create tasks for each query
        tasks = []
        for name, query in active_queries.items():
            task = self._fetch_single_metric(name, query)
            tasks.append(task)
        
        # Execute all queries concurrently with proper error handling
        start_time = time.monotonic()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.monotonic() - start_time
        
        # Process results, handling any exceptions
        success_count = 0
        error_count = 0
        queries_with_data = 0 # New counter

        for i, result in enumerate(results):
            query_name = list(active_queries.keys())[i]
            
            if isinstance(result, Exception):
                error_count += 1
                logger.error(f"Query '{query_name}' failed with error: {result}")
                
                # Increment failure count for this query - NEW
                self._query_failure_counts[query_name] = self._query_failure_counts.get(query_name, 0) + 1
                
                # Disable query if it has failed too many times - NEW
                if self._query_failure_counts[query_name] >= self._max_failures_before_disable:
                    logger.warning(f"Disabling query '{query_name}' after {self._max_failures_before_disable} consecutive failures")
                    self._disabled_queries.add(query_name)
                
                continue

            # Query completed without exception
            success_count += 1
            
            # Reset failure count on success - NEW
            self._query_failure_counts[query_name] = 0

            if result:  # Check if the list of metrics is not empty
                name, metrics_list, raw_data = result
                
                # Validate metric values before adding - NEW
                valid_metrics = []
                for metric in metrics_list:
                    # Skip metrics with NaN or infinite values
                    if not math.isfinite(metric.value):
                        logger.warning(f"Skipping metric with non-finite value: {metric.metric_name} = {metric.value}")
                        continue
                    valid_metrics.append(metric)
                
                if valid_metrics:
                    all_metrics.extend(valid_metrics)
                    queries_with_data += 1 # Increment counter for queries returning data
                else:
                    logger.debug(f"Query '{name}' returned no valid metrics after filtering")

        # Log statistics - Updated log message
        logger.debug(
            f"Processed {query_count} Prometheus queries in {elapsed:.2f}s: "
            f"{success_count} completed, {error_count} failed, "
            f"{queries_with_data} returned data, total {len(all_metrics)} metrics, "
            f"{len(self._disabled_queries)} disabled"
        )

        return all_metrics
    
    @with_exponential_backoff(max_retries=3)
    async def _fetch_single_metric(self, name: str, query: str) -> Tuple[str, List[MetricPoint], Any]:
        """
        Fetch and parse a single metric query with retries.
        
        Args:
            name: Metric name
            query: PromQL query
            
        Returns:
            Tuple of (name, list of parsed MetricPoint objects, raw data)
        """
        # This function gets retry logic from the decorator
        logger.debug(f"Querying metric '{name}': {query[:80]}...")
        data = await self._query_prometheus(query)
        
        if not data:
            logger.warning(f"Empty response for metric '{name}'")
            return name, [], None
        
        # Parse the response
        metrics = self._parse_prometheus_response(name, data)
        logger.debug(f"Parsed {len(metrics)} data points for '{name}'")
        
        # Special handling for empty result sets that use "or vector(0)" fallbacks
        # If the result set is empty but we're using a fallback vector(0), create a default datapoint
        if not metrics and "vector(0)" in query:
            logger.debug(f"Creating fallback datapoint with value 0 for '{name}'")
            now = datetime.datetime.now(datetime.timezone.utc)
            metrics.append(
                MetricPoint(
                    metric_name=name,
                    labels={"source": "fallback"},
                    value=0.0,
                    timestamp=now
                )
            )
            
        return name, metrics, data
    
    @with_circuit_breaker(service_name="prometheus")
    async def _query_prometheus(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Execute a Prometheus query with proper error handling and circuit breaking.
        
        Properly handles URL construction to avoid issues with double slashes
        by stripping any trailing slashes from the base URL. Protected by circuit
        breaker to prevent cascading failures if Prometheus is unavailable.
        
        Args:
            query: PromQL query string
            
        Returns:
            Parsed JSON response dict or None on error
        """
        if not self._client:
            raise RuntimeError("HTTP client not initialized")
        
        # Ensure no double slashes by stripping trailing slash    
        prometheus_base = self.prometheus_url.rstrip('/') if isinstance(self.prometheus_url, str) else self.prometheus_url
        prometheus_url = f"{prometheus_base}/api/v1/query"
        params = {"query": query}
        
        logger.debug(f"Querying Prometheus API at: {prometheus_url}")

        try:
            response = await self._client.get(
                prometheus_url,
                params=params,
                timeout=self.timeout
            )

            # Raise for status will trigger retry in calling function
            response.raise_for_status()
            
            # Parse response
            data = response.json()
            if data.get("status") != "success":
                error_msg = f"Prometheus query failed with status '{data.get('status')}'."
                if "error" in data:
                    error_msg += f" Error: {data.get('error')}"
                if "errorType" in data:
                    error_msg += f" Type: {data.get('errorType')}"
                logger.error(error_msg)
                return None
            
            return data
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error querying Prometheus: {e.response.status_code} - {e.response.reason_phrase}")
            # Including more response details for debugging
            if hasattr(e.response, 'text') and e.response.text:
                logger.error(f"Response details: {e.response.text[:500]}")
            raise
        except httpx.RequestError as e:
            # More detailed logging for connection errors
            logger.error(f"Connection error querying Prometheus: {repr(e)}")
            if hasattr(e, 'request') and e.request:
                logger.error(f"Failed request: {e.request.method} {e.request.url}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error querying Prometheus: {repr(e)}")
            raise
    
    def _parse_prometheus_response(self, query_name: str, data: Dict) -> List[MetricPoint]:
        """
        Parse the JSON response from Prometheus API into MetricPoint objects.
        
        Args:
            query_name: The logical name of the query (used as metric_name if not in result)
            data: The JSON dictionary returned by the Prometheus query API
            
        Returns:
            List of MetricPoint objects
        """
        metrics = []
        if not data:
            logger.warning(f"Empty data for query '{query_name}'")
            return metrics
            
        if "data" not in data:
            logger.warning(f"No 'data' field in response for query '{query_name}'")
            return metrics
            
        # Extract data section for convenience
        data = data["data"]
        
        if "result" not in data:
            logger.warning(f"No 'result' field in data for query '{query_name}'")
            return metrics
            
        if not data["result"]:
            logger.info(f"Query '{query_name}' returned empty result set (type: {data.get('resultType', 'unknown')})")
            return metrics
            
        # Get current time if not in the response
        now = datetime.datetime.now(datetime.timezone.utc)
        timestamp = data.get("timestamp", now.timestamp())
        if isinstance(timestamp, str):
            try:
                timestamp = float(timestamp)
            except (ValueError, TypeError):
                timestamp = now.timestamp()
                
        # Convert timestamp to datetime if it's a number
        if isinstance(timestamp, (int, float)):
            timestamp_dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        else:
            timestamp_dt = now
            
        # Process based on result type
        result_type = data.get("resultType", "unknown")
        
        try:
            for item in data["result"]:
                # Get metric info
                metric_info = item.get("metric", {})
                
                # Handle different result types
                if result_type == "vector":
                    # Regular instant query result
                    if "value" in item:
                        timestamp_val, value = item["value"]
                        
                        # Try to convert value to float, skipping NaN or inf
                        try:
                            value_float = float(value)
                            if math.isnan(value_float) or math.isinf(value_float):
                                logger.info(f"Skipping {query_name} with special value {value}")
                                continue
                        except (ValueError, TypeError):
                            logger.warning(f"Could not convert value '{value}' to float for {query_name}")
                            continue
                            
                        # Create timestamp datetime
                        try:
                            ts = datetime.datetime.fromtimestamp(float(timestamp_val), tz=datetime.timezone.utc)
                        except (ValueError, TypeError):
                            ts = timestamp_dt
                            
                        # Create MetricPoint
                        metrics.append(
                            MetricPoint(
                                metric_name=query_name,
                                labels=metric_info,
                                value=value_float,
                                timestamp=ts
                            )
                        )
                    else:
                        logger.warning(f"No 'value' field in result item for query '{query_name}'")
                elif result_type == "matrix":
                    # Range query result with values array
                    if "values" in item:
                        for val_timestamp, val in item["values"]:
                            # Try to convert value to float, skipping NaN or inf
                            try:
                                value_float = float(val)
                                if math.isnan(value_float) or math.isinf(value_float):
                                    logger.info(f"Skipping {query_name} with special value {val}")
                                    continue
                            except (ValueError, TypeError):
                                continue
                                
                            # Create timestamp datetime
                            try:
                                ts = datetime.datetime.fromtimestamp(float(val_timestamp), tz=datetime.timezone.utc)
                            except (ValueError, TypeError):
                                ts = timestamp_dt
                                
                            # Create MetricPoint
                            metrics.append(
                                MetricPoint(
                                    metric_name=query_name,
                                    labels=metric_info,
                                    value=value_float,
                                    timestamp=ts
                                )
                            )
                    else:
                        logger.warning(f"No 'values' field in result item for query '{query_name}'")
                else:
                    # Scalar or other types - typically not useful for anomaly detection
                    logger.warning(f"Unsupported result type '{result_type}' for query '{query_name}'")
        except Exception as e:
            logger.exception(f"Error parsing Prometheus response for '{query_name}': {e}")
            
        logger.debug(f"Parsed {len(metrics)} data points for '{query_name}'")
        return metrics

    # def _save_raw_data(self, query_name: str, data: Dict):
    #     """Saves the raw Prometheus response data to a file."""
    #     output_dir = "scraped_data"
    #     os.makedirs(output_dir, exist_ok=True)
    #     filename = os.path.join(output_dir, f"{query_name}_raw_data.json")
    #     try:
    #         with open(filename, "w") as f:
    #             json.dump(data, f, indent=4)
    #         logger.info(f"Saved raw data for '{query_name}' to {filename}")
    #     except Exception as e:
    #         logger.error(f"Failed to save raw data for '{query_name}' to {filename}: {e}")


# --- Legacy API for backward compatibility ---

class PrometheusClient:
    """
    Legacy API wrapper for better connection pooling and concurrent querying.
    Contains httpx client with proper pooling configuration.
    """
    
    def __init__(
        self,
        prometheus_url: Optional[str] = None,
        limits: httpx.Limits = DEFAULT_LIMITS,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        Initialize the Prometheus client wrapper.
        
        Args:
            prometheus_url: Prometheus server URL (if None, uses settings.prom_url)
            limits: httpx connection limits
            timeout: Request timeout in seconds
        """
        self.prometheus_url = prometheus_url or settings.prom_url
        # Strip trailing slash if present
        if isinstance(self.prometheus_url, str):
            self.prometheus_url = self.prometheus_url.rstrip('/')
            
        self.limits = limits
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._stack = AsyncExitStack()
        
    async def __aenter__(self) -> httpx.AsyncClient:
        """Enter async context manager."""
        self._client = httpx.AsyncClient(
            limits=self.limits,
            timeout=httpx.Timeout(timeout=self.timeout),
            http2=False, # Disable HTTP/2 as 'h2' package might not be installed
            transport=httpx.AsyncHTTPTransport(retries=2) # Updated retries to 2
        )
        await self._stack.enter_async_context(self._client)
        return self._client
        
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager."""
        await self._stack.aclose()
        self._client = None


@with_exponential_backoff(max_retries=3)
async def query_prometheus(
    client: httpx.AsyncClient, query: str, prometheus_url: Optional[str] = None
) -> Optional[Dict]:
    """
    Legacy function for backward compatibility.
    Asynchronously query the Prometheus API with automatic retries.

    Args:
        client: An httpx.AsyncClient instance with connection pooling.
        query: The PromQL query string.
        prometheus_url: Override Prometheus URL (if None, uses settings.prom_url)

    Returns:
        The JSON response dictionary from Prometheus, or None if an error occurs.
    """
    # Ensure no double slashes if prom_url already ends with /
    prometheus_base = prometheus_url or str(settings.prom_url).rstrip('/')
    prometheus_url = f"{prometheus_base}/api/v1/query"
    params = {"query": query}
    
    logger.debug(f"Legacy API: Querying Prometheus at: {prometheus_url}")
    
    # This function has retry logic through the decorator
    response = await client.get(prometheus_url, params=params, timeout=30.0)
    response.raise_for_status()  # Raise exception for bad status codes (4xx or 5xx)
    
    data = response.json()
    if data.get("status") != "success":
        error_msg = f"Prometheus query failed with status '{data.get('status')}'. "
        if "error" in data:
            error_msg += f"Error: {data.get('error')}"
        if "errorType" in data:
            error_msg += f" Type: {data.get('errorType')}"
        logger.error(error_msg)
        return None
    
    return data


def parse_prometheus_response(
    query_name: str, data: Dict
) -> List[MetricPoint]:
    """
    Legacy function for backward compatibility.
    Parse the JSON response from Prometheus API into MetricPoint objects.

    Args:
        query_name: The logical name of the query (used as metric_name if not in result).
        data: The JSON dictionary returned by the Prometheus query API.

    Returns:
        A list of MetricPoint objects.
    """
    # Create a temporary fetcher just to use its parsing logic
    fetcher = PrometheusFetcher(asyncio.Queue())
    return fetcher._parse_prometheus_response(query_name, data)


# Example usage (for testing or direct script execution):
if __name__ == "__main__":
    from kubewise.logging import setup_logging
    setup_logging()

    async def test_prometheus_fetcher():
        """Test the PrometheusFetcher class"""
        # Create a queue to receive metrics
        metrics_queue = asyncio.Queue()

        # Create and start the fetcher
        # This will now use settings.prom_queries from kubewise.config
        fetcher = PrometheusFetcher(metrics_queue)
        try:
            await fetcher.start()

            logger.info("Waiting for metrics to be fetched...")
            # Wait a bit for first poll to complete
            await asyncio.sleep(settings.prom_queries_poll_interval + 2) # Wait for at least one poll cycle

            # Process metrics from the queue
            received_count = 0
            logger.info("Processing received metrics...")

            # Check queue for metrics with timeout to avoid blocking indefinitely
            try:
                while not metrics_queue.empty() and received_count < 20: # Process up to 20 metrics
                    metric = metrics_queue.get_nowait()
                    print(f"Received metric: {metric.metric_name}{metric.labels} = {metric.value} @ {metric.timestamp}")
                    metrics_queue.task_done()
                    received_count += 1

                if received_count == 0:
                     logger.warning("No metrics received from the queue. Check Prometheus connection and queries.")

                logger.info(f"Fetcher stats: fetched={fetcher.metrics_fetched}, errors={fetcher.query_errors}, successful_polls={fetcher.successful_polls}, failed_polls={fetcher.failed_polls}")

            except asyncio.QueueEmpty:
                logger.info("Metrics queue is empty.")

        finally:
            # Clean up
            await fetcher.stop()
            logger.info("Prometheus fetcher stopped")

    async def main():
        """Run the PrometheusFetcher test"""
        logger.info("=== Testing PrometheusFetcher Class ===")
        await test_prometheus_fetcher()

    try:
        # Ensure settings are loaded before running tests
        from kubewise.config import settings
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Tests stopped by user")
    except Exception as e:
        logger.exception(f"Unhandled error in tests: {e}")
