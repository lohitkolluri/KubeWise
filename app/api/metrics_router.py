from fastapi import APIRouter, HTTPException, Query
import asyncio
import requests
from loguru import logger

from app.core.config import settings
from app.models.anomaly_detector import anomaly_detector
from app.utils.prometheus_scraper import PrometheusScraper, prometheus_scraper
from app.worker import AutonomousWorker

router = APIRouter(
    prefix="/metrics",
    tags=["Metrics"],
    responses={404: {"description": "Not found"}},
)

scraper = PrometheusScraper()


def get_worker_instance() -> AutonomousWorker:
    """Helper to get worker instance from main app."""
    try:
        from app.main import worker_instance

        return worker_instance
    except (ImportError, AttributeError):
        logger.warning("Could not obtain worker instance from main app")
        return None


@router.get(
    "/",
    summary="Get current cluster metrics",
    description="Fetch metrics from Prometheus for all entities using the active PromQL queries",
)
async def get_metrics():
    """
    Fetch metrics from Prometheus using configured PromQL queries.
    Returns metrics for all detected entities.

    Returns:
        dict: Metrics data containing:
            - status: "success" or "error"
            - count: Number of entities with metrics
            - metrics: Dictionary of entity metrics keyed by entity identifier

    Raises:
        HTTPException: If metrics cannot be fetched from Prometheus
    """
    try:
        # Use active queries from worker if available, otherwise use defaults
        worker = get_worker_instance()
        queries = (
            worker.active_promql_queries if worker else settings.DEFAULT_PROMQL_QUERIES
        )

        # Get metrics using current queries
        metrics = await prometheus_scraper.fetch_all_metrics(queries)

        entity_count = len(metrics)

        return {"status": "success", "count": entity_count, "metrics": metrics}
    except Exception as e:
        logger.error(f"Error fetching metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching metrics: {str(e)}")


@router.get(
    "/queries",
    summary="Get active monitoring queries",
    description="Retrieve the current set of PromQL queries being used to monitor the Kubernetes cluster",
)
async def get_queries():
    """
    Get the current set of PromQL queries used for monitoring.

    Returns:
        dict: Query information containing:
            - status: "success" or "error"
            - queries: List of active PromQL queries currently in use
            - default_queries: List of default queries used as fallback
    """
    # Use active queries from worker if available, otherwise use defaults
    worker = get_worker_instance()
    active_queries = (
        worker.active_promql_queries if worker else settings.DEFAULT_PROMQL_QUERIES
    )

    return {
        "status": "success",
        "queries": active_queries,
        "default_queries": settings.DEFAULT_PROMQL_QUERIES,
    }


@router.get(
    "/model/info",
    summary="Get model information",
    description="Retrieve information about the currently loaded anomaly detection model",
)
async def get_model_info():
    """
    Get information about the current anomaly detection model.

    Returns:
        dict: Model information containing:
            - status: "success" or "error"
            - model_info: Details about the model parameters and configuration

    Raises:
        HTTPException: If model information cannot be retrieved
    """
    try:
        model_params = anomaly_detector.get_model_parameters()

        return {"status": "success", "model_info": model_params}
    except Exception as e:
        logger.error(f"Error getting model info: {e}")
        raise HTTPException(
            status_code=500, detail=f"Error getting model info: {str(e)}"
        )


@router.get(
    "/verify",
    summary="Verify PromQL queries",
    description="Test the current set of PromQL queries to ensure they are working properly",
)
async def verify_queries():
    """
    Verify the current set of PromQL queries by testing them against Prometheus.

    Returns:
        Dict containing verification results:
            - status: str indicating overall status
            - working_queries: List of queries that worked
            - failed_queries: List of queries that failed with errors
            - total_queries: int total number of queries tested
            - success_rate: float percentage of successful queries
    """
    try:
        # Get worker instance and then get queries
        worker = get_worker_instance()
        # Use active_promql_queries instead of promql_queries to match the format used elsewhere
        queries = worker.active_promql_queries if worker else settings.DEFAULT_PROMQL_QUERIES

        # Convert list to dict if necessary for consistent processing
        if isinstance(queries, list):
            queries = {f"query_{i}": query for i, query in enumerate(queries)}

        working_queries = []
        failed_queries = []

        for query_name, query in queries.items():
            logger.info(f"Verifying query: {query_name}")

            # First validate the query
            validation = await scraper.validate_query(query)

            if not validation["valid"]:
                failed_queries.append(
                    {
                        "name": query_name,
                        "query": query,
                        "error": validation["error"],
                        "warnings": validation["warnings"],
                    }
                )
                continue

            # If validation passed, try executing the query
            try:
                results = await scraper.query_prometheus(query, time_window_seconds=300)
                working_queries.append(
                    {
                        "name": query_name,
                        "query": query,
                        "warnings": validation["warnings"],
                        "result_count": len(results),
                    }
                )
            except Exception as e:
                failed_queries.append(
                    {
                        "name": query_name,
                        "query": query,
                        "error": str(e),
                        "warnings": validation["warnings"],
                    }
                )

        total_queries = len(queries)
        success_rate = (
            (len(working_queries) / total_queries) * 100 if total_queries > 0 else 0
        )

        return {
            "status": "success" if success_rate > 0 else "failed",
            "working_queries": working_queries,
            "failed_queries": failed_queries,
            "total_queries": total_queries,
            "success_rate": success_rate,
        }

    except Exception as e:
        logger.error(f"Error verifying queries: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/diagnostic")
async def run_metric_diagnostic(entity_id: str = Query(None, description="Optional entity ID to filter results")):
    """
    Run a diagnostic to check which metrics are available for each entity.
    This helps troubleshoot the 'Only X% of metrics are non-zero' issue.
    """
    try:
        # Get worker instance to use its queries
        worker = get_worker_instance()
        queries = worker.active_promql_queries if worker else settings.DEFAULT_PROMQL_QUERIES

        if not queries:
            return {
                "diagnostic_results": [],
                "summary": {
                    "error": "No queries available",
                    "total_entities": 0
                }
            }

        # Create a dictionary mapping metric types to queries based on metric names in queries
        metric_types = {}
        for query in queries:
            # Extract the metric name from the query (simple heuristic)
            for metric in settings.FEATURE_METRICS:
                if metric in query:
                    if metric not in metric_types:
                        metric_types[metric] = []
                    metric_types[metric].append(query)

        # Collect metrics using all available queries
        entity_metrics = {}

        # Try each query for each metric type
        for metric_type, metric_queries in metric_types.items():
            for query in metric_queries:
                try:
                    # Execute the query
                    data = await prometheus_scraper.query_prometheus(
                        query, settings.ANOMALY_METRIC_WINDOW_SECONDS
                    )

                    # Filter by entity_id if provided
                    if entity_id:
                        data = [item for item in data if entity_id.lower() in str(item.get("entity_id", "")).lower()]

                    # Process the data by entity
                    for item in data:
                        # Generate entity key
                        e_id = item.get("entity_id", "unknown")
                        e_type = item.get("entity_type", "unknown")
                        namespace = item.get("namespace")

                        entity_key = f"{e_type}/{e_id}"
                        if namespace:
                            entity_key = f"{namespace}/{entity_key}"

                        if entity_key not in entity_metrics:
                            entity_metrics[entity_key] = {
                                "entity_id": e_id,
                                "entity_type": e_type,
                                "namespace": namespace,
                                "found_metrics": {}
                            }

                        # Add the metric type and its values
                        if metric_type not in entity_metrics[entity_key]["found_metrics"]:
                            values = item.get("values", [])
                            if values:
                                entity_metrics[entity_key]["found_metrics"][metric_type] = {
                                    "query": query,
                                    "has_values": len(values) > 0,
                                    "num_values": len(values),
                                    "sample": values[0] if values else None
                                }

                except Exception as e:
                    logger.warning(f"Error executing query '{query}': {e}")

        # Calculate metrics coverage for each entity
        results = []
        for entity_key, entity_data in entity_metrics.items():
            covered_metrics = len(entity_data["found_metrics"])
            total_metrics = len(settings.FEATURE_METRICS)
            coverage = (covered_metrics / total_metrics) * 100 if total_metrics > 0 else 0

            results.append({
                "entity_key": entity_key,
                "entity_id": entity_data["entity_id"],
                "entity_type": entity_data["entity_type"],
                "namespace": entity_data["namespace"],
                "metrics_coverage": f"{coverage:.1f}%",
                "covered_metrics": covered_metrics,
                "total_metrics": total_metrics,
                "found_metrics": entity_data["found_metrics"],
                "missing_metrics": [
                    metric for metric in settings.FEATURE_METRICS
                    if metric not in entity_data["found_metrics"]
                ]
            })

        # Sort by coverage (lowest first)
        results.sort(key=lambda x: len(x["found_metrics"]))

        return {
            "diagnostic_results": results,
            "summary": {
                "total_entities": len(results),
                "entities_with_full_coverage": len([r for r in results if len(r["found_metrics"]) == len(settings.FEATURE_METRICS)]),
                "entities_with_no_metrics": len([r for r in results if len(r["found_metrics"]) == 0])
            }
        }
    except Exception as e:
        logger.error(f"Error running metric diagnostic: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error running diagnostic: {e}")

@router.get("/available-metrics")
async def get_available_metrics():
    """
    Get a list of all available metric names from Prometheus.
    This helps identify which metrics are actually present in your Prometheus instance.
    """
    try:
        # Use direct query to get metric names since list_available_metrics doesn't exist
        response = await asyncio.to_thread(
            requests.get,
            f"{settings.PROMETHEUS_URL}/api/v1/label/__name__/values",
            timeout=settings.PROMETHEUS_TIMEOUT / 2
        )
        response.raise_for_status()

        data = response.json()
        if data.get("status") != "success":
            raise ValueError(f"Prometheus returned error status: {data.get('status')}")

        metric_names = data.get("data", [])

        # Group metrics by common prefixes
        grouped_metrics = {}
        for metric in metric_names:
            # Split by first underscore
            parts = metric.split("_", 1)
            prefix = parts[0] if len(parts) > 1 else "other"

            if prefix not in grouped_metrics:
                grouped_metrics[prefix] = []

            grouped_metrics[prefix].append(metric)

        # Find relevant metrics for KubeWise
        relevant_metrics = {
            "container": [m for m in metric_names if "container_" in m],
            "pod": [m for m in metric_names if "pod_" in m],
            "kube": [m for m in metric_names if "kube_" in m],
            "node": [m for m in metric_names if "node_" in m],
            "cpu": [m for m in metric_names if "cpu" in m],
            "memory": [m for m in metric_names if "memory" in m],
            "network": [m for m in metric_names if "network" in m],
        }

        return {
            "total_metrics": len(metric_names),
            "grouped_metrics": grouped_metrics,
            "relevant_metrics": relevant_metrics
        }
    except Exception as e:
        logger.error(f"Error listing available metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error listing metrics: {e}")

@router.post(
    "/model/threshold",
    summary="Update anomaly detection threshold",
    description="Dynamically adjust the anomaly detection threshold based on changing environmental conditions",
)
async def update_threshold(threshold: float):
    """
    Dynamically update the anomaly detection threshold.

    This endpoint allows for real-time adjustment of the sensitivity of the anomaly detector
    without requiring a restart. Higher threshold values make the detector less sensitive
    (fewer anomalies will be detected), while lower values make it more sensitive.

    Args:
        threshold: The new threshold value to set (typically between 0.001 and 0.1)

    Returns:
        dict: Results of the threshold update containing:
            - status: "success" or "error"
            - old_threshold: Previous threshold value
            - new_threshold: Updated threshold value
            - recommended_range: Suggested range for threshold values
            - note: Additional information about the update

    Raises:
        HTTPException: If the threshold is invalid or cannot be updated
    """
    try:
        if threshold <= 0:
            raise ValueError("Threshold must be a positive number")

        # Store the old threshold for reporting
        old_threshold = anomaly_detector.threshold

        # Get current model parameters to check validity
        model_params = anomaly_detector.get_model_parameters()
        if model_params.get("status") != "loaded":
            raise ValueError("Cannot update threshold: model is not loaded correctly")

        # Update the threshold in the model
        anomaly_detector.threshold = threshold
        logger.info(f"Anomaly detection threshold updated from {old_threshold} to {threshold}")

        # Determine if the new threshold falls within a reasonable range
        # based on the model's typical reconstruction error
        is_reasonable = True
        note = "Threshold updated successfully"

        # Check if the threshold seems too high (insensitive) or too low (hypersensitive)
        if threshold > 0.1:
            is_reasonable = False
            note = "Warning: Threshold may be too high, potentially missing anomalies"
        elif threshold < 0.001:
            is_reasonable = False
            note = "Warning: Threshold may be too low, potentially causing false positives"

        # Return the result with helpful context
        return {
            "status": "success",
            "old_threshold": old_threshold,
            "new_threshold": threshold,
            "recommended_range": "0.001 to 0.1",
            "is_reasonable": is_reasonable,
            "note": note
        }

    except Exception as e:
        logger.error(f"Error updating threshold: {e}")
        raise HTTPException(
            status_code=400, detail=f"Error updating threshold: {str(e)}"
        )
