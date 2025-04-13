import requests
from loguru import logger
from typing import Dict, List, Any
from app.core.config import settings
from datetime import datetime, timedelta
import asyncio

class PrometheusScraper:
    """
    Utility for scraping metrics from Prometheus monitoring system.
    Provides methods to execute queries and fetch metrics.
    """

    def __init__(self):
        """Initialize the Prometheus scraper with the configured URL."""
        self.prometheus_url = settings.PROMETHEUS_URL
        logger.info(f"PrometheusScraper initialized for URL: {self.prometheus_url}")

    async def query_prometheus(self, query: str, time_window_seconds: int) -> List[Dict[str, Any]]:
        """
        Queries Prometheus for a given PromQL query over a time window.

        Args:
            query: The PromQL query string to execute
            time_window_seconds: Time window in seconds to fetch data for

        Returns:
            List of metric data dictionaries with entity information and values
        """
        results = []
        api_endpoint = f"{self.prometheus_url}/api/v1/query_range"

        try:
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(seconds=time_window_seconds)
            params = {
                'query': query,
                'start': start_time.isoformat() + "Z",
                'end': end_time.isoformat() + "Z",
                'step': '15s' # Adjust step as needed
            }

            # Use asyncio with requests in a thread pool
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, lambda: requests.get(api_endpoint, params=params, timeout=15))
            response.raise_for_status()

            data = response.json()
            if data['status'] == 'success':
                for result in data['data']['result']:
                    entity_labels = result['metric']
                    entity_id_keys = ['pod', 'node', 'deployment', 'statefulset', 'daemonset', 'persistentvolumeclaim', 'service']
                    entity_id = 'unknown'
                    entity_type = 'unknown'
                    for key in entity_id_keys:
                        if key in entity_labels:
                            entity_id = entity_labels[key]
                            entity_type = key
                            break # Take the first match

                    metric_data = {
                        "entity_id": entity_id,
                        "entity_type": entity_type,
                        "namespace": entity_labels.get("namespace"),
                        "query": query,
                        "labels": entity_labels,
                        "values": result['values']
                    }
                    results.append(metric_data)
            else:
                logger.warning(f"Prometheus query failed with status: {data.get('status')}, error: {data.get('error')}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error querying Prometheus ({api_endpoint}): {e}")
        except Exception as e:
            logger.error(f"Error processing Prometheus response for query '{query}': {e}")

        return results

    async def fetch_all_metrics(self, queries_to_run: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetches metrics for the provided list of queries.

        Args:
            queries_to_run: List of PromQL query strings to execute

        Returns:
            Dictionary of entity metrics grouped by entity key
        """
        all_metrics_by_entity = {} # Group by entity_key
        time_window = settings.ANOMALY_METRIC_WINDOW_SECONDS

        if not queries_to_run:
            logger.warning("fetch_all_metrics called with no queries to run.")
            return {}

        # Run queries concurrently
        tasks = [self.query_prometheus(query, time_window) for query in queries_to_run]
        results_list = await asyncio.gather(*tasks)

        # Process results
        for query_results in results_list:
            for metric_data in query_results:
                entity_key = f"{metric_data['entity_type']}/{metric_data['entity_id']}"
                if metric_data['namespace']:
                    entity_key = f"{metric_data['namespace']}/{entity_key}"

                if entity_key not in all_metrics_by_entity:
                    all_metrics_by_entity[entity_key] = {
                        "entity_id": metric_data['entity_id'],
                        "entity_type": metric_data['entity_type'],
                        "namespace": metric_data['namespace'],
                        "metrics": [] # Store list of metric results for this entity
                    }
                # Append the full metric result (query, labels, values)
                all_metrics_by_entity[entity_key]["metrics"].append({
                     "query": metric_data["query"],
                     "labels": metric_data["labels"],
                     "values": metric_data["values"]
                 })

        logger.info(f"Fetched metrics using {len(queries_to_run)} queries for {len(all_metrics_by_entity)} entities.")
        return all_metrics_by_entity

# Global instance
prometheus_scraper = PrometheusScraper()
