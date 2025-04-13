from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from typing import Dict, Any, List
from app.utils.prometheus_scraper import prometheus_scraper
from app.core.config import settings
from app.models.anomaly_detector import anomaly_detector

router = APIRouter(
    prefix="/metrics",
    tags=["Metrics"],
    responses={404: {"description": "Not found"}},
)

def get_worker_instance():
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
