from typing import List, Optional, Dict
import time
from datetime import datetime, timezone

import motor.motor_asyncio
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
import httpx
from kubernetes_asyncio import client
from bson import ObjectId, errors

from kubewise.config import Settings, settings
from kubewise.api.deps import (
    get_mongo_db,
    get_settings,
    get_http_client,
    get_k8s_api_client,
    get_app_context,
)
from kubewise.api.context import AppContext
from kubewise.models import AnomalyRecord, RemediationPlan
from kubewise.utils.retry import circuit_breakers
from kubewise.remediation.diagnosis import DiagnosisEngine

# Router for API endpoints
router = APIRouter()

# --- Health & Metrics Endpoints ---


@router.get(
    "/health",
    tags=["Health"],
    status_code=status.HTTP_200_OK,
    summary="Basic health check",
    description="Simple health check endpoint that returns OK if the service is running.",
    response_description="Returns a simple status object indicating the service is healthy",
    responses={
        200: {
            "description": "Service is healthy",
            "content": {"application/json": {"example": {"status": "ok"}}},
        }
    },
)
async def health_check():
    """Basic health check endpoint."""
    return {"status": "ok"}


@router.get(
    "/livez",
    tags=["Health"],
    status_code=status.HTTP_200_OK,
    summary="Kubernetes liveness probe",
    description="Liveness probe endpoint for Kubernetes health checks.",
    response_description="Returns a simple status object for Kubernetes liveness probes",
    responses={
        200: {
            "description": "Service is alive",
            "content": {"application/json": {"example": {"status": "live"}}},
        }
    },
)
async def liveness_check():
    """Kubernetes liveness probe endpoint."""
    # Add more checks here if needed (e.g., DB connection)
    return {"status": "live"}


class HealthDetail(BaseModel):
    """Health status details model."""

    status: str = Field(..., description="Overall status: ok, degraded, failed")
    uptime_seconds: float = Field(..., description="Application uptime in seconds")
    start_time: datetime = Field(..., description="Time the application started")
    connections: Dict[str, Dict[str, str]] = Field(
        ..., description="Status of external connections"
    )
    queue_status: Dict[str, int] = Field(..., description="Status of internal queues")
    circuit_breakers: Dict[str, Dict[str, str]] = Field(
        ..., description="Status of circuit breakers"
    )
    components: Dict[str, str] = Field(
        ..., description="Status of application components"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "status": "ok",
                "uptime_seconds": 12345.67,
                "start_time": "2023-06-15T12:30:45.123456Z",
                "connections": {
                    "mongodb": {"status": "ok", "message": "Connected"},
                    "prometheus": {"status": "ok", "message": "Connected"},
                    "kubernetes": {"status": "ok", "message": "Connected"},
                },
                "queue_status": {"metrics": 42, "events": 12, "remediation": 3},
                "circuit_breakers": {
                    "prometheus:fetch_metrics": {
                        "state": "closed",
                        "error_count": "0",
                        "last_error": "none",
                    }
                },
                "components": {
                    "anomaly_detector": "ok",
                    "gemini_agent": "ok",
                    "k8s_event_watcher": "ok",
                },
            }
        }


@router.get(
    "/health/detail",
    response_model=HealthDetail,
    tags=["Health"],
    summary="Detailed health check with component status",
    description="""
    Provides comprehensive health information about all system components:
    
    · Overall system status (ok, degraded, or failed)
    · Uptime information
    · Connection status to external dependencies (MongoDB, Prometheus, Kubernetes)
    · Internal queue metrics
    · Circuit breaker statuses
    · Core component health statuses
    
    Use this endpoint for troubleshooting or monitoring dashboards.
    """,
    response_description="Detailed health status of all system components",
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
        "remediation": ctx.remediation_queue.qsize(),
    }

    # Process circuit breaker statuses
    cb_status = {}
    for cb_key, cb_data in circuit_breakers.items():
        service_name = cb_key.split(":")[0]
        cb_status[cb_key] = {
            "state": cb_data["state"],
            "error_count": str(cb_data["error_count"]),
            "last_error": str(
                datetime.fromtimestamp(cb_data["last_error_time"], tz=timezone.utc)
            )
            if cb_data["last_error_time"] > 0
            else "none",
        }

    # Determine overall status
    overall_status = "ok"
    if (
        mongodb_status == "failed"
        or kubernetes_status == "failed"
        or detector_status == "failed"
    ):
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
            "kubernetes": {"status": kubernetes_status, "message": kubernetes_message},
        },
        queue_status=queues,
        circuit_breakers=cb_status,
        components={
            "anomaly_detector": detector_status,
            "gemini_agent": gemini_status,
            "k8s_event_watcher": "ok"
            if ctx.k8s_event_watcher is not None
            else "failed",
        },
    )


@router.get(
    "/metrics",
    tags=["Metrics"],
    summary="Prometheus metrics endpoint",
    description="""
    Returns Prometheus-formatted metrics for the KubeWise service.
    This endpoint can be scraped by a Prometheus server to monitor KubeWise itself.
    
    Metrics include:
    · API request durations
    · Queue sizes
    · Processing times
    · Anomaly counts
    · Remediation statistics
    """,
    response_description="Prometheus metrics in text-based exposition format",
    responses={
        200: {
            "description": "Prometheus metrics in text format",
            "content": {
                "text/plain": {
                    "example": """
                    # HELP kubewise_anomalies_detected_total Total number of anomalies detected
                    # TYPE kubewise_anomalies_detected_total counter
                    kubewise_anomalies_detected_total{type="cpu"} 23
                    kubewise_anomalies_detected_total{type="memory"} 17
                    
                    # HELP kubewise_api_request_duration_seconds API request duration in seconds
                    # TYPE kubewise_api_request_duration_seconds histogram
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.005"} 42
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.01"} 84
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.025"} 112
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.05"} 198
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.075"} 239
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.1"} 251
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.25"} 254
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.5"} 254
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="0.75"} 254
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="1.0"} 254
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="2.5"} 254
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="5.0"} 254
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="7.5"} 254
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="10.0"} 254
                    kubewise_api_request_duration_seconds_bucket{endpoint="/health",le="+Inf"} 254
                    """
                }
            },
        }
    },
)
async def metrics_endpoint():
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Anomaly Endpoints ---


@router.get(
    "/anomalies",
    response_model=List[AnomalyRecord],
    tags=["Anomalies"],
    summary="List detected anomalies",
    description="""
    Retrieve a paginated list of anomalies detected by the system, sorted by detection time (most recent first).
    
    Each anomaly record contains:
    · Unique identifier
    · Timestamp of detection
    · Resource type and name affected
    · Anomaly score (0.0-1.0, higher means more severe)
    · Raw metric value that triggered the anomaly
    · Whether remediation was attempted and its status
    
    Use the 'skip' and 'limit' parameters for pagination.
    """,
    response_description="List of anomaly records sorted by timestamp (descending)",
    responses={
        500: {
            "description": "Database error",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to retrieve anomalies from database."}
                }
            },
        }
    },
)
async def list_anomalies(
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_mongo_db),
    skip: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    limit: int = Query(
        100, ge=1, le=500, description="Maximum number of records to return"
    ),
):
    """
    Retrieve a list of recently detected anomalies, sorted by detection time descending.
    """
    try:
        anomalies_cursor = (
            db["anomalies"]
            .find()
            .sort("timestamp", -1)  # Sort by timestamp descending
            .skip(skip)
            .limit(limit)
        )
        anomalies_raw = await anomalies_cursor.to_list(length=limit)

        # Convert MongoDB ObjectId to string for Pydantic validation
        anomalies_validated: List[AnomalyRecord] = []
        for raw_anomaly in anomalies_raw:
            # Store _id as a string in the id field and remove _id
            if "_id" in raw_anomaly:
                raw_anomaly["id"] = str(raw_anomaly["_id"])
                # Some validation code may still expect _id but as a string
                raw_anomaly["_id"] = str(raw_anomaly["_id"])
            
            # Convert ObjectId instances in remediation_plan_id field
            if "remediation_plan_id" in raw_anomaly and isinstance(raw_anomaly["remediation_plan_id"], ObjectId):
                raw_anomaly["remediation_plan_id"] = str(raw_anomaly["remediation_plan_id"])
            
            # Convert ObjectId instances in similar_anomaly_ids list if present
            if "similar_anomaly_ids" in raw_anomaly and isinstance(raw_anomaly["similar_anomaly_ids"], list):
                raw_anomaly["similar_anomaly_ids"] = [
                    str(item) if isinstance(item, ObjectId) else item 
                    for item in raw_anomaly["similar_anomaly_ids"]
                ]

            try:
                anomalies_validated.append(AnomalyRecord.model_validate(raw_anomaly))
            except Exception as e:
                # Log validation error but continue processing others
                anomaly_id = raw_anomaly.get("id", raw_anomaly.get("_id", "unknown"))
                logger.warning(
                    f"Failed to validate anomaly record {anomaly_id} from DB: {e}"
                )

        return anomalies_validated
    except Exception as e:
        logger.exception(f"Error fetching anomalies from database: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve anomalies from database.",
        )


# --- Remediation Plan Endpoints ---

class RemediationPlanRecord(RemediationPlan):
    """Pydantic model for a remediation plan record retrieved from the database."""
    id: Optional[str] = Field(None, alias="_id", description="Unique ID of the remediation plan")

    class Config:
        populate_by_name = True # Allow using alias _id for id
        json_schema_extra = {
            "example": {
                "id": "60c72b2f9b1e8b3b4c6e4d2a",
                "anomaly_id": "60c72b2f9b1e8b3b4c6e4d29",
                "plan_name": "Restart Pod",
                "description": "Restart the pod 'example-pod-123' in namespace 'default'.",
                "actions": [
                    {
                        "action_type": "restart_pod",
                        "parameters": {"namespace": "default", "name": "example-pod-123"},
                        "estimated_impact": "low",
                        "estimated_confidence": 0.9
                    }
                ],
                "status": "pending",
                "created_at": "2023-01-01T12:00:00Z",
                "updated_at": "2023-01-01T12:00:00Z"
            }
        }


@router.get(
    "/remediation/plans/{plan_id}",
    response_model=RemediationPlanRecord,
    tags=["Remediation"],
    summary="Get a specific remediation plan by ID",
    description="""
    Retrieve detailed information about a specific remediation plan.
    
    The response contains:
    · Unique identifier for the plan
    · ID of the anomaly it addresses
    · Plan name and description
    · Sequence of actions to be performed
    · Status of the plan (e.g., pending, executing, completed, failed)
    """,
    response_description="Detailed information about the remediation plan",
    responses={
        404: {
            "description": "Plan not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Remediation plan with ID 60c72b2f9b1e8b3b4c6e4d2a not found"}
                }
            },
        },
        500: {
            "description": "Database error",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to retrieve remediation plan from database"}
                }
            },
        }
    },
)
async def get_remediation_plan(
    plan_id: str,
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Retrieve a specific remediation plan by its ID.
    """
    try:
        # Convert string ID to ObjectId
        try:
            obj_id = ObjectId(plan_id)
        except errors.InvalidId:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid plan ID format: {plan_id}",
            )
            
        # Query the database for the plan
        plan_doc = await db["remediation_plans"].find_one({"_id": obj_id})
        
        if not plan_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Remediation plan with ID {plan_id} not found",
            )
        
        # Convert ObjectId to string for the response
        if "_id" in plan_doc:
            plan_doc["id"] = str(plan_doc["_id"])
            plan_doc["_id"] = str(plan_doc["_id"])  # Ensure _id is also a string for model_validate
            
        # Convert any ObjectId in anomaly_id if present
        if "anomaly_id" in plan_doc and isinstance(plan_doc["anomaly_id"], ObjectId):
            plan_doc["anomaly_id"] = str(plan_doc["anomaly_id"])
            
        try:
            return RemediationPlanRecord.model_validate(plan_doc)
        except Exception as e:
            logger.error(f"Failed to validate remediation plan: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to process remediation plan data: {str(e)}",
            )
            
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.exception(f"Error fetching remediation plan from database: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve remediation plan from database",
        )

@router.get(
    "/remediation/plans",
    response_model=List[RemediationPlanRecord],
    tags=["Remediation"],
    summary="List remediation plans",
    description="""
    Retrieve a paginated list of remediation plans, sorted by creation time (most recent first).
    
    Each plan record contains:
    · Unique identifier for the plan
    · ID of the anomaly it addresses
    · Plan name and description
    · Sequence of actions to be performed
    · Status of the plan (e.g., pending, executing, completed, failed)
    
    Use the 'skip' and 'limit' parameters for pagination.
    """,
    response_description="List of remediation plan records sorted by timestamp (descending)",
    responses={
        500: {
            "description": "Database error",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to retrieve remediation plans from database."}
                }
            },
        }
    },
)
async def list_remediation_plans(
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_mongo_db),
    skip: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    limit: int = Query(
        100, ge=1, le=500, description="Maximum number of records to return"
    ),
):
    """
    Retrieve a list of remediation plans, sorted by creation time descending.
    """
    try:
        plans_cursor = (
            db["remediation_plans"]
            .find()
            .sort("created_at", -1)  # Sort by creation time descending
            .skip(skip)
            .limit(limit)
        )
        plans_raw = await plans_cursor.to_list(length=limit)

        plans_validated: List[RemediationPlanRecord] = []
        for raw_plan in plans_raw:
            if "_id" in raw_plan:
                raw_plan["id"] = str(raw_plan["_id"])
                 # Ensure _id is also a string if present for Pydantic model_validate
                raw_plan["_id"] = str(raw_plan["_id"])


            try:
                # Validate against RemediationPlanRecord which includes the id
                plans_validated.append(RemediationPlanRecord.model_validate(raw_plan))
            except Exception as e:
                plan_id = raw_plan.get("id", raw_plan.get("_id", "unknown"))
                logger.warning(
                    f"Failed to validate remediation plan record {plan_id} from DB: {e}"
                )
        return plans_validated
    except Exception as e:
        logger.exception(f"Error fetching remediation plans from database: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve remediation plans from database.",
        )


# --- Configuration Endpoint ---


class ConfigResponse(BaseModel):
    """Response model for GET /config."""

    mongo_uri_set: bool = Field(..., description="Whether MongoDB URI is configured")
    prom_url: str = Field(..., description="Prometheus URL")
    gemini_api_key_set: bool = Field(
        ..., description="Whether Gemini API key is configured"
    )
    anomaly_threshold: float = Field(
        ..., description="Anomaly detection threshold (0.0-1.0)"
    )
    log_level: str = Field(..., description="Current logging level")
    gemini_model_id: str = Field(..., description="Gemini model ID being used")

    class Config:
        json_schema_extra = {
            "example": {
                "mongo_uri_set": True,
                "prom_url": "http://prometheus.monitoring:9090",
                "gemini_api_key_set": True,
                "anomaly_threshold": 0.85,
                "log_level": "INFO",
                "gemini_model_id": "gemini-1.5-flash",
            }
        }


class ConfigUpdatePayload(BaseModel):
    """Payload for updating configuration."""

    anomaly_threshold: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="New anomaly score threshold (0.0 to 1.0)."
    )
    # Add other updatable fields here if needed (e.g., log_level)

    class Config:
        json_schema_extra = {"example": {"anomaly_threshold": 0.75}}


@router.get(
    "/config",
    response_model=ConfigResponse,
    tags=["Configuration"],
    summary="View current configuration",
    description="""
    Retrieve the current application configuration settings.
    
    This endpoint provides visibility into:
    · Whether essential credentials are configured (Mongo URI, Gemini API key)
    · Current Prometheus URL
    · Anomaly detection threshold
    · Log level
    · AI model configuration
    
    Sensitive values are masked for security reasons.
    """,
    response_description="Current configuration settings with credentials masked",
)
async def get_current_config(current_settings: Settings = Depends(get_settings)):
    """
    Retrieve the current application configuration (secrets masked).
    """
    return ConfigResponse(
        mongo_uri_set=bool(current_settings.mongo_uri),
        prom_url=str(current_settings.prom_url),  # Convert HttpUrl to string
        gemini_api_key_set=bool(
            current_settings.gemini_api_key.get_secret_value() != "changeme"
            and current_settings.gemini_api_key.get_secret_value()
        ),
        anomaly_threshold=current_settings.anomaly_threshold,
        log_level=current_settings.log_level,
        gemini_model_id=current_settings.gemini_model_id,
    )


@router.put(
    "/config",
    response_model=ConfigResponse,
    tags=["Configuration"],
    summary="Update configuration (hot-reload)",
    description="""
    Update application configuration settings at runtime without requiring a restart.
    
    Currently supports updating:
    · 'anomaly_threshold': Adjust sensitivity of anomaly detection (0.0-1.0)
    
    Changes take effect immediately and persist for the current application instance.
    The updated configuration is returned.
    """,
    response_description="Updated configuration settings",
    responses={
        400: {
            "description": "Invalid configuration value",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Anomaly threshold must be between 0.0 and 1.0"
                    }
                }
            },
        }
    },
)
async def update_config(
    payload: ConfigUpdatePayload = Body(...),
    current_settings: Settings = Depends(get_settings),
):
    """
    Update parts of the application configuration. Currently supports hot-reloading
    the `anomaly_threshold`.
    """
    # Handle anomaly threshold update
    if payload.anomaly_threshold is not None:
        # We already have validation from Pydantic, but let's double check
        if payload.anomaly_threshold < 0.0 or payload.anomaly_threshold > 1.0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Anomaly threshold must be between 0.0 and 1.0",
            )

        # Update in runtime settings
        current_settings.anomaly_threshold = payload.anomaly_threshold
        logger.info(f"Anomaly threshold updated to {payload.anomaly_threshold}")

    # Add other config updates here if more fields are added to ConfigUpdatePayload

    # Return the updated config
    return ConfigResponse(
        mongo_uri_set=bool(current_settings.mongo_uri),
        prom_url=str(current_settings.prom_url),
        gemini_api_key_set=bool(
            current_settings.gemini_api_key.get_secret_value() != "changeme"
            and current_settings.gemini_api_key.get_secret_value()
        ),
        anomaly_threshold=current_settings.anomaly_threshold,
        log_level=current_settings.log_level,
        gemini_model_id=current_settings.gemini_model_id,
    )


# Add PATCH endpoint as well for partial updates (optional, PUT can handle this)
# router.patch("/config", ...)

# --- Validation Endpoints ---


class QueryValidationRequest(BaseModel):
    """Request model for query validation."""

    query: str = Field(..., description="The PromQL query to validate and test")
    prometheus_url: Optional[str] = Field(
        None, description="Optional custom Prometheus URL to use for this query"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "query": 'sum(rate(container_cpu_usage_seconds_total{namespace="default"}[5m])) by (pod)',
                "prometheus_url": None,  # Uses the system default
            }
        }


class QueryValidationResponse(BaseModel):
    """Response model for query validation."""

    query: str = Field(..., description="The original PromQL query that was tested")
    valid: bool = Field(
        ..., description="Whether the query is syntactically valid and returned results"
    )
    result_type: Optional[str] = Field(
        None, description="Type of result returned (vector, matrix, scalar, string)"
    )
    results_count: Optional[int] = Field(
        None, description="Number of data points/series returned"
    )
    sample_results: Optional[List[dict]] = Field(
        None, description="Sample of the actual results (limited)"
    )
    error: Optional[str] = Field(None, description="Error message if query failed")
    execution_time_ms: float = Field(
        ..., description="Time taken to execute the query in milliseconds"
    )
    timestamp: str = Field(..., description="Timestamp when the query was executed")

    class Config:
        json_schema_extra = {
            "example": {
                "query": 'sum(rate(container_cpu_usage_seconds_total{namespace="default"}[5m])) by (pod)',
                "valid": True,
                "result_type": "vector",
                "results_count": 5,
                "sample_results": [
                    {
                        "metric": {"pod": "my-app-76d5c8b675-f7t9j"},
                        "value": [1623766278.123, "0.056712"],
                    },
                    {
                        "metric": {"pod": "my-app-76d5c8b675-2kl7m"},
                        "value": [1623766278.123, "0.078321"],
                    },
                ],
                "error": None,
                "execution_time_ms": 123.45,
                "timestamp": "2023-06-15T12:31:18.123456Z",
            }
        }


@router.post(
    "/validation/query",
    response_model=QueryValidationResponse,
    tags=["Validation"],
    summary="Validate and test a Prometheus query",
    description="""
    Validates and executes a PromQL query against the configured Prometheus instance.
    
    This endpoint is useful for:
    · Testing whether a query is syntactically valid
    · Seeing sample results from a query
    · Measuring query performance
    · Verifying Prometheus connectivity
    
    You can optionally specify a different Prometheus URL than the system default.
    """,
    response_description="Query validation results with sample data",
    responses={
        400: {
            "description": "Invalid request",
            "content": {
                "application/json": {"example": {"detail": "Query cannot be empty"}}
            },
        },
        500: {
            "description": "Prometheus connection error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Failed to connect to Prometheus: Connection refused"
                    }
                }
            },
        },
    },
)
async def validate_prometheus_query(
    payload: QueryValidationRequest = Body(...),
    http_client: httpx.AsyncClient = Depends(get_http_client),
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
    from datetime import datetime

    # Validate input
    if not payload.query.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Query cannot be empty"
        )

    start_time = time.time()
    timestamp = datetime.now(timezone.utc).isoformat()

    # Use provided Prometheus URL or fall back to configured one
    prometheus_url = payload.prometheus_url or str(current_settings.prom_url)

    try:
        # Execute query
        url = f"{prometheus_url}/api/v1/query"
        params = {"query": payload.query}

        response = await http_client.get(url, params=params, timeout=10.0)
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
                timestamp=timestamp,
            )

        # Process result
        result_type = data.get("data", {}).get("resultType")
        results = data.get("data", {}).get("result", [])

        # Create sample results (limit to 5 entries)
        sample_results = results[:5] if results else None

        return QueryValidationResponse(
            query=payload.query,
            valid=True,
            result_type=result_type,
            results_count=len(results),
            sample_results=sample_results,
            error=None,
            execution_time_ms=(time.time() - start_time) * 1000,
            timestamp=timestamp,
        )

    except httpx.HTTPStatusError as e:
        logger.warning(
            f"HTTP error validating Prometheus query: {e.response.status_code} - {e.response.text}"
        )
        return QueryValidationResponse(
            query=payload.query,
            valid=False,
            error=f"HTTP error: {e.response.status_code} - {e.response.text}",
            execution_time_ms=(time.time() - start_time) * 1000,
            timestamp=timestamp,
        )
    except httpx.ConnectError as e:
        logger.error(f"Failed to connect to Prometheus at {prometheus_url}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to connect to Prometheus: {str(e)}",
        )
    except httpx.RequestError as e:
        logger.error(f"Request error: {str(e)}")
        return QueryValidationResponse(
            query=payload.query,
            valid=False,
            error=f"Request error: {str(e)}",
            execution_time_ms=(time.time() - start_time) * 1000,
            timestamp=timestamp,
        )
    except Exception as e:
        logger.exception(f"Error validating query: {e}")
        return QueryValidationResponse(
            query=payload.query,
            valid=False,
            error=f"Internal error: {str(e)}",
            execution_time_ms=(time.time() - start_time) * 1000,
            timestamp=timestamp,
        )

class RemediationResponse(BaseModel):
    """Response model for remediation requests."""
    
    anomaly_id: str = Field(..., description="ID of the anomaly being remediated")
    status: str = Field(..., description="Status of the remediation request")
    message: str = Field(..., description="Message about the remediation request")
    diagnosis_id: Optional[str] = Field(None, description="ID of the diagnosis if created")
    
    class Config:
        json_schema_extra = {
            "example": {
                "anomaly_id": "507f1f77bcf86cd799439011",
                "status": "initiated",
                "message": "Remediation workflow started. A diagnosis has been created and remediation will be attempted based on findings.",
                "diagnosis_id": "507f1f77bcf86cd799439012"
            }
        }

@router.post(
    "/anomalies/{anomaly_id}/remediate",
    response_model=RemediationResponse,
    tags=["Anomalies"],
    summary="Trigger remediation for a specific anomaly",
    description="""
    Trigger the remediation workflow for a specific anomaly.
    
    This endpoint will:
    1. Validate that the anomaly exists and is eligible for remediation
    2. Initiate a diagnosis workflow for the anomaly
    3. Queue the anomaly for remediation processing
    
    The remediation will be performed asynchronously. The response includes the status
    of the request and the ID of the initiated diagnosis.
    """,
    response_description="Status of the remediation request",
    responses={
        404: {
            "description": "Anomaly not found",
            "content": {
                "application/json": {"example": {"detail": "Anomaly not found or not eligible for remediation"}}
            },
        },
        400: {
            "description": "Invalid request",
            "content": {
                "application/json": {"example": {"detail": "Anomaly already has active remediation in progress"}}
            },
        },
        500: {
            "description": "Internal server error",
            "content": {
                "application/json": {"example": {"detail": "Failed to initiate remediation"}}
            },
        },
    },
)
async def remediate_anomaly(
    anomaly_id: str,
    ctx: AppContext = Depends(get_app_context),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_mongo_db),
    k8s_client: client.ApiClient = Depends(get_k8s_api_client),
):
    """Trigger remediation for a specific anomaly."""
    logger.info(f"Received remediation request for anomaly {anomaly_id}")
    
    try:
        # Convert string ID to ObjectId if necessary
        try:
            anomaly_oid = ObjectId(anomaly_id)
        except errors.InvalidId:
            logger.warning(f"Invalid ObjectId format for anomaly: {anomaly_id}")
            raise HTTPException(
                status_code=404,
                detail="Anomaly not found or not eligible for remediation",
            )
        
        # Find the anomaly in the database
        anomaly_record = await db.anomalies.find_one({"_id": anomaly_oid})
        
        if not anomaly_record:
            logger.warning(f"Anomaly not found: {anomaly_id}")
            raise HTTPException(
                status_code=404,
                detail="Anomaly not found or not eligible for remediation",
            )
        
        # Validate if the anomaly is eligible for remediation
        if anomaly_record.get("remediation_status") in ["in_progress", "pending", "diagnosing"]:
            logger.warning(f"Anomaly {anomaly_id} already has active remediation")
            raise HTTPException(
                status_code=400,
                detail="Anomaly already has active remediation in progress",
            )
        
        # Create a DiagnosisEngine instance
        diagnosis_engine = DiagnosisEngine(
            db=db,
            k8s_client=k8s_client,
        )
        
        # Initiate diagnosis
        diagnosis_id = await diagnosis_engine.initiate_diagnosis(anomaly_record)
        
        if not diagnosis_id:
            logger.error(f"Failed to initiate diagnosis for anomaly {anomaly_id}")
            raise HTTPException(
                status_code=500,
                detail="Failed to initiate diagnosis workflow",
            )
        
        # Queue the remediation task
        logger.info(f"Queuing anomaly {anomaly_id} for remediation with diagnosis {diagnosis_id}")
        await ctx.remediation_queue.put({
            "anomaly_id": str(anomaly_oid),
            "diagnosis_id": str(diagnosis_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        
        # Update the anomaly record to indicate remediation is in progress
        update_result = await db.anomalies.update_one(
            {"_id": anomaly_oid},
            {"$set": {
                "remediation_status": "diagnosing",
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }}
        )
        
        if update_result.modified_count == 0:
            logger.warning(f"Failed to update remediation status for anomaly {anomaly_id}")
        
        # Return a successful response
        return RemediationResponse(
            anomaly_id=anomaly_id,
            status="initiated",
            message="Remediation workflow started",
            diagnosis_id=str(diagnosis_id),
        )
        
    except HTTPException:
        # Re-raise HTTP exceptions for proper API responses
        raise
        
    except Exception as e:
        logger.exception(f"Error triggering remediation for anomaly {anomaly_id}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to initiate remediation: {str(e)}"
        )

class RemediationStatusResponse(BaseModel):
    """Response model for remediation status requests."""
    
    diagnosis_id: str = Field(..., description="ID of the diagnosis")
    anomaly_id: str = Field(..., description="ID of the anomaly being remediated")
    status: str = Field(..., description="Current status of the remediation process")
    plan_id: Optional[str] = Field(None, description="ID of the remediation plan if created")
    plan_status: Optional[str] = Field(None, description="Status of the remediation plan")
    completed: bool = Field(default=False, description="Whether remediation is complete")
    message: str = Field(..., description="Description of current status")
    
    class Config:
        json_schema_extra = {
            "example": {
                "diagnosis_id": "507f1f77bcf86cd799439012",
                "anomaly_id": "507f1f77bcf86cd799439011",
                "status": "in_progress",
                "plan_id": "507f1f77bcf86cd799439013",
                "plan_status": "executing",
                "completed": False,
                "message": "Remediation in progress - executing plan actions",
            }
        }

@router.get(
    "/remediation/status/{diagnosis_id}",
    response_model=RemediationStatusResponse,
    tags=["Remediation"],
    summary="Check status of a remediation process",
    description="""
    Get the current status of a remediation process by its diagnosis ID.
    
    This endpoint provides visibility into:
    · Current status of the remediation process
    · Associated remediation plan (if created)
    · Whether remediation is complete
    · Any relevant messages about the current state
    
    Use this endpoint to track remediation progress from the CLI or other tools.
    """,
    response_description="Current status of the remediation process",
    responses={
        404: {
            "description": "Diagnosis not found",
            "content": {
                "application/json": {"example": {"detail": "Diagnosis with ID 507f1f77bcf86cd799439012 not found"}}
            },
        },
        500: {
            "description": "Database error",
            "content": {
                "application/json": {"example": {"detail": "Failed to retrieve remediation status"}}
            },
        },
    },
)
async def get_remediation_status(
    diagnosis_id: str,
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Get current status of a remediation process by diagnosis ID."""
    # Validate the diagnosis ID format
    try:
        obj_id = ObjectId(diagnosis_id)
    except errors.InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid diagnosis ID format: {diagnosis_id}",
        )
    
    # Fetch the diagnosis from the database
    diagnosis_dict = await db["diagnoses"].find_one({"_id": obj_id})
    
    if not diagnosis_dict:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Diagnosis with ID {diagnosis_id} not found",
        )
    
    # Get the anomaly ID associated with this diagnosis
    anomaly_id = diagnosis_dict.get("anomaly_id")
    if not anomaly_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid diagnosis record: missing anomaly ID",
        )
    
    # Convert ObjectId to string if needed
    if isinstance(anomaly_id, ObjectId):
        anomaly_id = str(anomaly_id)
    
    # Check if a remediation plan was created
    plan_id = diagnosis_dict.get("remediation_plan_id")
    plan_status = None
    completed = False
    
    if plan_id:
        # Convert ObjectId to string if needed
        if isinstance(plan_id, ObjectId):
            plan_id = str(plan_id)
            
        # Try to get the plan details
        plan_dict = await db["remediation_plans"].find_one({"_id": ObjectId(plan_id)})
        if plan_dict:
            # Determine plan status
            if plan_dict.get("completed", False):
                plan_status = "completed"
                completed = True
            elif plan_dict.get("executing", False):
                plan_status = "executing"
            else:
                plan_status = "planned"
    
    # Determine overall status
    diagnosis_status = diagnosis_dict.get("status", "unknown")
    
    if diagnosis_status == "completed" and plan_id and plan_status == "completed":
        status = "completed"
        completed = True
        message = "Remediation completed successfully"
    elif diagnosis_status == "failed":
        status = "failed"
        completed = True
        message = diagnosis_dict.get("error_message", "Remediation failed")
    elif not plan_id:
        status = "diagnosing"
        message = "Analyzing anomaly and determining remediation actions"
    elif plan_status == "executing":
        status = "in_progress"
        message = "Remediation in progress - executing plan actions"
    elif plan_status == "planned":
        status = "planned"
        message = "Remediation plan created and awaiting execution"
    else:
        status = diagnosis_status
        message = "Remediation status is being updated"
    
    return RemediationStatusResponse(
        diagnosis_id=diagnosis_id,
        anomaly_id=anomaly_id,
        status=status,
        plan_id=plan_id,
        plan_status=plan_status,
        completed=completed,
        message=message,
    )
