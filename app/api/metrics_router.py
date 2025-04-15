from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from typing import Dict, Any, List
from app.utils.prometheus_scraper import prometheus_scraper, PrometheusScraper
from app.core.config import settings
from app.models.anomaly_detector import anomaly_detector
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

@router.get("/",
            summary="Get current cluster metrics",
            description="Fetch metrics from Prometheus for all entities using the active PromQL queries")
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
        queries = worker.active_promql_queries if worker else settings.DEFAULT_PROMQL_QUERIES

        # Get metrics using current queries
        metrics = await prometheus_scraper.fetch_all_metrics(queries)

        entity_count = len(metrics)

        return {
            "status": "success",
            "count": entity_count,
            "metrics": metrics
        }
    except Exception as e:
        logger.error(f"Error fetching metrics: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching metrics: {str(e)}"
        )

@router.get("/queries",
            summary="Get active monitoring queries",
            description="Retrieve the current set of PromQL queries being used to monitor the Kubernetes cluster")
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
    active_queries = worker.active_promql_queries if worker else settings.DEFAULT_PROMQL_QUERIES

    return {
        "status": "success",
        "queries": active_queries,
        "default_queries": settings.DEFAULT_PROMQL_QUERIES
    }

@router.get("/model/info",
            summary="Get model information",
            description="Retrieve information about the currently loaded anomaly detection model")
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

        return {
            "status": "success",
            "model_info": model_params
        }
    except Exception as e:
        logger.error(f"Error getting model info: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error getting model info: {str(e)}"
        )

@router.get("/verify",
            summary="Verify PromQL queries",
            description="Test the current set of PromQL queries to ensure they are working properly")
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
        # Get current queries from worker or use defaults
        queries = worker.promql_queries if worker else settings.PROMQL_QUERIES

        working_queries = []
        failed_queries = []

        for query_name, query in queries.items():
            logger.info(f"Verifying query: {query_name}")

            # First validate the query
            validation = await scraper.validate_query(query)

            if not validation["valid"]:
                failed_queries.append({
                    "name": query_name,
                    "query": query,
                    "error": validation["error"],
                    "warnings": validation["warnings"]
                })
                continue

            # If validation passed, try executing the query
            try:
                results = await scraper.query_prometheus(query, time_window_seconds=300)
                working_queries.append({
                    "name": query_name,
                    "query": query,
                    "warnings": validation["warnings"],
                    "result_count": len(results)
                })
            except Exception as e:
                failed_queries.append({
                    "name": query_name,
                    "query": query,
                    "error": str(e),
                    "warnings": validation["warnings"]
                })

        total_queries = len(queries)
        success_rate = (len(working_queries) / total_queries) * 100 if total_queries > 0 else 0

        return {
            "status": "success" if success_rate > 0 else "failed",
            "working_queries": working_queries,
            "failed_queries": failed_queries,
            "total_queries": total_queries,
            "success_rate": success_rate
        }

    except Exception as e:
        logger.error(f"Error verifying queries: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
