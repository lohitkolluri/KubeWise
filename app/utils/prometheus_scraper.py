# prometheus_scraper.py
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List
import socket
import requests
from kubernetes import client
from kubernetes.client.rest import ApiException
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.core.config import settings
from app.utils.metric_tagger import MetricTagger

try:
    from app.utils.k8s_config import load_kube_config
except ImportError:
    from kubernetes.config import load_kube_config


class PrometheusScraper:
    def __init__(self):
        self.prometheus_url = settings.PROMETHEUS_URL
        self.timeout = settings.PROMETHEUS_TIMEOUT
        logger.info(f"Initialized PrometheusScraper for URL: {self.prometheus_url}")

        self.ignore_entities = ["unknown", "unknown/unknown"]

        try:
            load_kube_config()
            self.k8s_core_v1 = client.CoreV1Api()
            self.k8s_apps_v1 = client.AppsV1Api()
            self.k8s_available = True
            logger.info("Kubernetes client ready for label fetching.")
        except Exception as e:
            logger.warning("Kubernetes client initialization failed: {e}", exc_info=True)
            self.k8s_core_v1 = None
            self.k8s_apps_v1 = None
            self.k8s_available = False

    async def validate_query(self, query: str) -> Dict[str, Any]:
        result = {"valid": False, "error": None, "returned_data": False, "warnings": []}

        if not query.strip():
            result["error"] = "Empty query"
            return result

        if "rate(" in query and "[" not in query:
            result["warnings"].append("rate() function used without time range")

        if "sum(" in query and "by (" not in query:
            result["warnings"].append("sum() without by() may be expensive")

        try:
            await asyncio.get_running_loop()
            response = await asyncio.to_thread(
                requests.get,
                f"{self.prometheus_url}/api/v1/query",
                params={"query": query},
                timeout=self.timeout / 2,
            )
            if response.status_code != 200:
                result["error"] = f"Status {response.status_code} from Prometheus"
                return result

            data = response.json()
            if data.get("status") == "success":
                result["valid"] = True
                if data.get("data", {}).get("result"):
                    result["returned_data"] = True
            else:
                result["error"] = f"{data.get('errorType')}: {data.get('error')}"

        except requests.exceptions.RequestException as e:
            result["error"] = f"Network error: {e}"
        except Exception as e:
            result["error"] = f"Unexpected error: {e}"

        return result

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2), retry=retry_if_exception_type(requests.exceptions.RequestException))
    async def query_prometheus(self, query: str, time_window_seconds: int) -> List[Dict[str, Any]]:
        results = []
        try:
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(seconds=time_window_seconds)
            response = await asyncio.to_thread(
                requests.get,
                f"{self.prometheus_url}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start_time.isoformat() + "Z",
                    "end": end_time.isoformat() + "Z",
                    "step": "15s",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()

            data = response.json()
            if data.get("status") == "success":
                for result in data["data"].get("result", []):
                    labels = result.get("metric", {})
                    entity_id, entity_type = self._detect_entity(query, labels)

                    values = result.get("values") or [result.get("value")]
                    if not values:
                        continue

                    results.append({
                        "entity_id": entity_id,
                        "entity_type": entity_type,
                        "namespace": labels.get("namespace"),
                        "query": query,
                        "labels": labels,
                        "values": values,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
        except Exception as e:
            logger.error(f"Failed to query Prometheus: {e}")

        return results

    def _detect_entity(self, query: str, labels: Dict[str, str]) -> (str, str):
        if "node" in labels:
            return labels["node"], "node"
        if "instance" in labels:
            return labels["instance"].split(":")[0], "node"
        if "pod" in labels:
            return labels["pod"], "pod"
        if "container" in labels and "pod" in labels:
            return labels["pod"], "pod"
        if "deployment" in labels:
            return labels["deployment"], "deployment"
        if "service" in labels:
            return labels["service"], "service"
        if "namespace" in labels:
            return labels["namespace"], "namespace"
        if "__name__" in labels:
            return labels["__name__"], "metric"
        if "node_cpu_seconds_total" in query or "node_memory" in query:
            return socket.gethostname(), "node"
        return "unknown", "unknown"

    async def fetch_all_metrics(self, queries: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch metrics for all queries and organize them by entity key.

        Args:
            queries: List of Prometheus PromQL queries to execute

        Returns:
            Dictionary of entity metrics keyed by entity identifier
        """
        logger.info(f"Fetching metrics for {len(queries)} queries")

        if not queries:
            logger.warning("No queries provided to fetch_all_metrics")
            return {}

        # DEBUG: Log the Prometheus URL being used
        logger.info(f"Using Prometheus URL: {self.prometheus_url}")
        # Test Prometheus connectivity
        try:
            response = await asyncio.to_thread(
                requests.get,
                f"{self.prometheus_url}/-/healthy",
                timeout=self.timeout / 2
            )
            logger.info(f"Prometheus health check status: {response.status_code}")
        except Exception as e:
            logger.error(f"Prometheus health check failed: {e}")

        # Collect all raw metrics from Prometheus
        all_metrics = {}
        query_tasks = []

        # Create tasks for executing all queries in parallel
        for query in queries:
            # Use 5 minutes as default time window for metrics
            time_window = getattr(settings, "ANOMALY_METRIC_WINDOW_SECONDS", 300)
            query_tasks.append(self.query_prometheus(query, time_window))

        # Execute all queries concurrently
        try:
            query_results = await asyncio.gather(*query_tasks, return_exceptions=True)

            # Process the results
            total_metrics = 0
            successful_queries = 0
            error_queries = 0
            empty_queries = 0
            # Track which queries are empty for debugging
            empty_query_list = []
            # List of queries that are expected to be empty in normal operation
            expected_empty_queries = [
                "sum by (namespace, pod) (kube_pod_container_status_last_terminated_reason{reason=\"OOMKilled\"}) > 0"
            ]

            for i, result in enumerate(query_results):
                if isinstance(result, Exception):
                    logger.warning(f"Error executing query '{queries[i]}': {result}")
                    error_queries += 1
                    continue

                if not result:
                    # Check if this is an expected empty query (like OOMKilled when no OOM events)
                    if queries[i] in expected_empty_queries:
                        # Log at debug level only for expected empty queries
                        logger.debug(f"Query returned no data as expected: '{queries[i]}'")
                    else:
                        # Log as warning for unexpected empty queries
                        logger.warning(f"Query returned no data: '{queries[i]}'")

                    empty_queries += 1
                    empty_query_list.append(queries[i])
                    continue

                successful_queries += 1

                # Process each metric result
                for metric in result:
                    entity_id = metric.get("entity_id")
                    entity_type = metric.get("entity_type")
                    namespace = metric.get("namespace")

                    # Skip metrics without proper entity identification
                    if entity_id == "unknown" or entity_type == "unknown":
                        continue

                    # Create entity key for organization
                    entity_key = f"{entity_type}/{entity_id}"
                    if namespace:
                        entity_key = f"{namespace}/{entity_key}"

                    # Skip ignored entities
                    if entity_key in self.ignore_entities:
                        continue

                    # Initialize entity in all_metrics if not exists
                    if entity_key not in all_metrics:
                        all_metrics[entity_key] = {
                            "entity_id": entity_id,
                            "entity_type": entity_type,
                            "namespace": namespace,
                            "metrics": []
                        }

                    # Add metric to the entity
                    # DEBUG: Add more info about the values we're getting
                    value_type = type(metric.get('values', []))
                    value_count = len(metric.get('values', [])) if hasattr(metric.get('values', []), '__len__') else 0
                    value_sample = str(metric.get('values', [])[:2]) if value_count > 0 else "None"
                    logger.debug(f"Adding metric: {metric.get('query', '')[:30]}... Type: {value_type}, Count: {value_count}, Sample: {value_sample}")

                    all_metrics[entity_key]["metrics"].append(metric)
                    total_metrics += 1

            # Log collection stats with more details
            logger.info(f"Metrics collection summary: Total queries: {len(queries)}, Successful: {successful_queries}, Errors: {error_queries}, Empty: {empty_queries}")

            # Only log empty queries as warnings if they're not in expected_empty_queries
            unexpected_empty_queries = [q for q in empty_query_list if q not in expected_empty_queries]
            if unexpected_empty_queries:
                logger.warning(f"Unexpected empty queries: {unexpected_empty_queries}")

            logger.info(f"Collected {total_metrics} metrics across {len(all_metrics)} entities")

            # If we got no metrics, log a more specific error
            if total_metrics == 0:
                logger.error("No metrics were collected. Check Prometheus connection and queries.")
                # Try one direct query to see what happens
                if queries:
                    try:
                        logger.info("Testing direct Prometheus query...")
                        response = await asyncio.to_thread(
                            requests.get,
                            f"{self.prometheus_url}/api/v1/query",
                            params={"query": "up"},
                            timeout=self.timeout / 2,
                        )
                        logger.info(f"Direct query status: {response.status_code}")
                        if response.status_code == 200:
                            logger.info(f"Direct query result: {response.json()}")
                    except Exception as e:
                        logger.error(f"Direct query failed: {e}")

            # Apply metric tagging before returning
            return MetricTagger.tag_metrics_batch(all_metrics)

        except Exception as e:
            logger.error(f"Error in fetch_all_metrics: {e}", exc_info=True)
            return {}

    async def fetch_metric_data_for_label_check(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Fetches metric data primarily for checking labels and structure,
        not for time series analysis.

        Args:
            query: The PromQL query to execute
            limit: Maximum number of results to return

        Returns:
            List of results with metric labels and values
        """
        try:
            # Use instant query for better performance when checking labels
            response = await asyncio.to_thread(
                requests.get,
                f"{self.prometheus_url}/api/v1/query",
                params={"query": query},
                timeout=self.timeout / 2,
            )
            response.raise_for_status()

            data = response.json()
            results = []

            if data.get("status") == "success":
                for result in data["data"].get("result", [])[:limit]:
                    labels = result.get("metric", {})
                    value = result.get("value")

                    results.append({
                        "labels": labels,
                        "value": value,
                        "metric": query.split("{")[0] if "{" in query else query
                    })

            return results
        except Exception as e:
            logger.warning(f"Error in fetch_metric_data_for_label_check: {e}")
            return []

    async def get_pod_metadata(self, namespace: str, pod_name: str) -> Dict[str, Any]:
        """
        Fetch metadata about a pod from the Kubernetes API.

        Args:
            namespace: Kubernetes namespace
            pod_name: Name of the pod

        Returns:
            Dictionary containing pod metadata
        """
        if not self.k8s_available or not self.k8s_core_v1:
            return {}

        try:
            pod = await asyncio.to_thread(
                self.k8s_core_v1.read_namespaced_pod,
                name=pod_name,
                namespace=namespace
            )

            # Extract relevant pod metadata
            metadata = {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "node": pod.spec.node_name,
                "labels": pod.metadata.labels or {},
                "containers": [container.name for container in pod.spec.containers],
                "creation_timestamp": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
            }

            return metadata
        except ApiException as e:
            logger.warning(f"Kubernetes API error fetching pod metadata: {e}")
        except Exception as e:
            logger.warning(f"Error fetching pod metadata: {e}")

        return {}


# Global scraper instance
prometheus_scraper = PrometheusScraper()
