from typing import List, Optional, Dict
import time
from datetime import datetime, timezone

import motor.motor_asyncio
from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
import httpx
from kubernetes_asyncio import client

from kubewise.config import Settings, settings
from kubewise.api.deps import get_mongo_db, get_settings, get_http_client, get_k8s_api_client, get_app_context
from kubewise.api.context import AppContext
from kubewise.models import AnomalyRecord
from kubewise.utils.retry import circuit_breakers

# Router for API endpoints
router = APIRouter()

# --- Health & Metrics Endpoints ---

@router.get("/health", tags=["Health"], status_code=status.HTTP_200_OK)
async def health_check():
    """Basic health check endpoint."""
    return {"status": "ok"}

@router.get("/livez", tags=["Health"], status_code=status.HTTP_200_OK)
async def liveness_check():
    """Kubernetes liveness probe endpoint."""
    # Add more checks here if needed (e.g., DB connection)
    return {"status": "live"}

class HealthDetail(BaseModel):
    """Health status details model."""
    status: str = Field(..., description="Overall status: ok, degraded, failed")
    uptime_seconds: float = Field(..., description="Application uptime in seconds")
    start_time: datetime = Field(..., description="Time the application started")
    connections: Dict[str, Dict[str, str]] = Field(..., description="Status of external connections")
    queue_status: Dict[str, int] = Field(..., description="Status of internal queues")
    circuit_breakers: Dict[str, Dict[str, str]] = Field(..., description="Status of circuit breakers")
    components: Dict[str, str] = Field(..., description="Status of application components")

@router.get(
    "/health/detail", 
    response_model=HealthDetail, 
    tags=["Health"],
    summary="Detailed health check with component status"
)
async def detailed_health_check(
    ctx: AppContext = Depends(get_app_context),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    k8s_client: client.ApiClient = Depends(get_k8s_api_client),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Provides detailed health information about the system including:
    - Connection status to external systems (MongoDB, Prometheus, Kubernetes)
    - Status of internal queues
    - Status of circuit breakers
    - Status of application components
    """
    start_time = getattr(ctx, "start_time", datetime.now(timezone.utc))
    uptime = time.time() - getattr(ctx, "start_timestamp", time.time())
    
    # Check MongoDB connection
    mongodb_status = "ok"
    mongodb_message = "Connected"
    try:
        await db.command("ping")
    except Exception as e:
        mongodb_status = "failed"
        mongodb_message = f"Connection error: {str(e)}"
    
    # Check Prometheus connection
    prometheus_status = "ok"
    prometheus_message = "Connected"
    try:
        response = await http_client.get(f"{settings.prom_url}/-/healthy", timeout=2.0)
        if response.status_code != 200:
            prometheus_status = "degraded"
            prometheus_message = f"Unhealthy response: {response.status_code}"
    except Exception as e:
        prometheus_status = "failed"
        prometheus_message = f"Connection error: {str(e)}"
    
    # Check Kubernetes connection
    kubernetes_status = "ok"
    kubernetes_message = "Connected"
    try:
        v1 = client.CoreV1Api(k8s_client)
        await v1.list_namespace(limit=1)
    except Exception as e:
        kubernetes_status = "failed"
        kubernetes_message = f"Connection error: {str(e)}"
    
    # Check anomaly detector
    detector_status = "ok" if ctx.anomaly_detector is not None else "failed"
    
    # Check Gemini agent
    gemini_status = "ok" if ctx.gemini_agent is not None else "not_configured"
    
    # Get queue statuses
    queues = {
        "metrics": ctx.metric_queue.qsize(),
        "events": ctx.event_queue.qsize(),
        "remediation": ctx.remediation_queue.qsize()
    }
    
    # Process circuit breaker statuses
    cb_status = {}
    for cb_key, cb_data in circuit_breakers.items():
        service_name = cb_key.split(":")[0]
        cb_status[cb_key] = {
            "state": cb_data["state"],
            "error_count": str(cb_data["error_count"]),
            "last_error": str(datetime.fromtimestamp(cb_data["last_error_time"], tz=timezone.utc)) if cb_data["last_error_time"] > 0 else "none"
        }
    
    # Determine overall status
    overall_status = "ok"
    if mongodb_status == "failed" or kubernetes_status == "failed" or detector_status == "failed":
        overall_status = "failed"
    elif prometheus_status == "failed" or prometheus_status == "degraded":
        overall_status = "degraded"
    
    return HealthDetail(
        status=overall_status,
        uptime_seconds=uptime,
        start_time=start_time,
        connections={
            "mongodb": {"status": mongodb_status, "message": mongodb_message},
            "prometheus": {"status": prometheus_status, "message": prometheus_message},
            "kubernetes": {"status": kubernetes_status, "message": kubernetes_message}
        },
        queue_status=queues,
        circuit_breakers=cb_status,
        components={
            "anomaly_detector": detector_status,
            "gemini_agent": gemini_status,
            "k8s_event_watcher": "ok" if ctx.k8s_event_watcher is not None else "failed"
        }
    )

@router.get("/metrics", tags=["Metrics"])
async def metrics_endpoint():
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Anomaly Endpoints ---

@router.get(
    "/anomalies",
    response_model=List[AnomalyRecord],
    tags=["Anomalies"],
    summary="List detected anomalies",
)
async def list_anomalies(
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_mongo_db),
    skip: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of records to return"),
):
    """
    Retrieve a list of recently detected anomalies, sorted by detection time descending.
    """
    try:
        anomalies_cursor = (
            db["anomalies"]
            .find()
            .sort("timestamp", -1) # Sort by timestamp descending
            .skip(skip)
            .limit(limit)
        )
        anomalies_raw = await anomalies_cursor.to_list(length=limit)

        # Convert MongoDB ObjectId to string for Pydantic validation if necessary
        # and handle potential validation errors per record
        anomalies_validated: List[AnomalyRecord] = []
        for raw_anomaly in anomalies_raw:
            if "_id" in raw_anomaly and isinstance(raw_anomaly["_id"], ObjectId):
                raw_anomaly["id"] = str(raw_anomaly["_id"]) # Add 'id' field
            try:
                anomalies_validated.append(AnomalyRecord.model_validate(raw_anomaly))
            except Exception as e:
                 # Log validation error but continue processing others
                 anomaly_id = raw_anomaly.get("id", raw_anomaly.get("_id", "unknown"))
                 logger.warning(f"Failed to validate anomaly record {anomaly_id} from DB: {e}")

        return anomalies_validated
    except Exception as e:
        logger.exception(f"Error fetching anomalies from database: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve anomalies from database.",
        )


# --- Configuration Endpoint ---

class ConfigResponse(BaseModel):
    """Response model for GET /config."""
    mongo_uri_set: bool
    prom_url: str
    gemini_api_key_set: bool
    anomaly_threshold: float
    log_level: str
    gemini_model_id: str

class ConfigUpdatePayload(BaseModel):
    """Payload for updating configuration."""
    anomaly_threshold: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="New anomaly score threshold (0.0 to 1.0)."
    )
    # Add other updatable fields here if needed (e.g., log_level)

@router.get(
    "/config",
    response_model=ConfigResponse,
    tags=["Configuration"],
    summary="View current configuration",
)
async def get_current_config(current_settings: Settings = Depends(get_settings)):
    """
    Retrieve the current application configuration (secrets masked).
    """
    return ConfigResponse(
        mongo_uri_set=bool(current_settings.mongo_uri),
        prom_url=str(current_settings.prom_url), # Convert HttpUrl to string
        gemini_api_key_set=bool(current_settings.gemini_api_key.get_secret_value() != "changeme" and current_settings.gemini_api_key.get_secret_value()),
        anomaly_threshold=current_settings.anomaly_threshold,
        log_level=current_settings.log_level,
        gemini_model_id=current_settings.gemini_model_id,
    )

@router.put(
    "/config",
    response_model=ConfigResponse,
    tags=["Configuration"],
    summary="Update configuration (hot-reload)",
)
async def update_config(
    payload: ConfigUpdatePayload = Body(...),
    current_settings: Settings = Depends(get_settings),
):
    """
    Update parts of the application configuration. Currently supports hot-reloading
    the `anomaly_threshold`.
    """
    updated = False
    if payload.anomaly_threshold is not None:
        if current_settings.anomaly_threshold != payload.anomaly_threshold:
            logger.info(
                f"Updating anomaly_threshold from {current_settings.anomaly_threshold} "
                f"to {payload.anomaly_threshold}"
            )
            current_settings.anomaly_threshold = payload.anomaly_threshold
            # Note: Need to ensure the OnlineAnomalyDetector instance actually uses
            # this updated value. This might require passing the settings object
            # to it or having a mechanism to notify it of the change.
            # For now, we update the global settings object.
            updated = True
        else:
             logger.debug(f"Anomaly threshold is already set to {payload.anomaly_threshold}. No change.")

    # Add logic for other updatable fields here

    if not updated:
        logger.info("No configuration values were updated.")
        # Return current config without indicating update
        # Or return 304 Not Modified? Let's return current state for simplicity.

    # Return the potentially updated configuration
    return await get_current_config(current_settings)

# Add PATCH endpoint as well for partial updates (optional, PUT can handle this)
# router.patch("/config", ...)

# --- Query Validation Endpoint ---

class QueryValidationRequest(BaseModel):
    """Request model for query validation."""
    query: str = Field(..., description="The PromQL query to validate and test")
    prometheus_url: Optional[str] = Field(None, description="Optional custom Prometheus URL to use for this query")

class QueryValidationResponse(BaseModel):
    """Response model for query validation."""
    query: str
    valid: bool
    result_type: Optional[str] = None
    results_count: Optional[int] = None
    sample_results: Optional[List[dict]] = None
    error: Optional[str] = None
    execution_time_ms: float
    timestamp: str

@router.post(
    "/validation/query",
    response_model=QueryValidationResponse,
    tags=["Validation"],
    summary="Validate and test a Prometheus query",
)
async def validate_prometheus_query(
    payload: QueryValidationRequest = Body(...),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_mongo_db),
    current_settings: Settings = Depends(get_settings),
):
    """
    Validate a Prometheus query and return the results.
    
    This endpoint tests a given PromQL query against Prometheus and returns:
    - Whether the query is valid
    - The type of result (scalar, vector, matrix)
    - Number of results returned
    - A sample of the results (up to 5)
    - Any error message if the query failed
    """
    import time
    import httpx
    from datetime import datetime
    from kubewise.collector.prometheus import parse_prometheus_response

    start_time = time.time()
    
    # Use provided Prometheus URL or fall back to configured one
    prometheus_url = payload.prometheus_url or str(current_settings.prom_url)
    
    try:
        # Create HTTP client
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Execute query
            url = f"{prometheus_url}/api/v1/query"
            params = {"query": payload.query}
            response = await client.get(url, params=params)
            response.raise_for_status()
            
            data = response.json()
            
            # Check if query was successful
            if data.get("status") != "success":
                error_msg = data.get("error", "Unknown error from Prometheus")
                return QueryValidationResponse(
                    query=payload.query,
                    valid=False,
                    error=error_msg,
                    execution_time_ms=(time.time() - start_time) * 1000,
                    timestamp=datetime.now().isoformat(),
                )
            
            # Parse the response
            result_type = data.get("data", {}).get("resultType")
            results = data.get("data", {}).get("result", [])
            
            # Parse the results into metrics
            metrics = parse_prometheus_response("custom_query", data)
            
            # Create sample results (up to 5)
            sample_results = []
            for i, metric in enumerate(metrics):
                if i >= 5:  # Limit to 5 samples
                    break
                sample_results.append({
                    "metric_name": metric.metric_name,
                    "labels": {k: v for k, v in metric.labels.items()},
                    "value": metric.value,
                    "timestamp": metric.timestamp.isoformat(),
                })
            
            return QueryValidationResponse(
                query=payload.query,
                valid=True,
                result_type=result_type,
                results_count=len(metrics),
                sample_results=sample_results,
                execution_time_ms=(time.time() - start_time) * 1000,
                timestamp=datetime.now().isoformat(),
            )
    
    except httpx.HTTPStatusError as e:
        return QueryValidationResponse(
            query=payload.query,
            valid=False,
            error=f"HTTP error: {e.response.status_code} - {e.response.text}",
            execution_time_ms=(time.time() - start_time) * 1000,
            timestamp=datetime.now().isoformat(),
        )
    except httpx.RequestError as e:
        return QueryValidationResponse(
            query=payload.query,
            valid=False,
            error=f"Request error: {str(e)}",
            execution_time_ms=(time.time() - start_time) * 1000,
            timestamp=datetime.now().isoformat(),
        )
    except Exception as e:
        logger.exception(f"Error validating query: {e}")
        return QueryValidationResponse(
            query=payload.query,
            valid=False,
            error=f"Internal error: {str(e)}",
            execution_time_ms=(time.time() - start_time) * 1000,
            timestamp=datetime.now().isoformat(),
        )
