from fastapi import APIRouter, Depends, HTTPException, Body
from app.services.anomaly_event_service import AnomalyEventService
from app.utils.k8s_executor import k8s_executor
from app.models.anomaly_event import AnomalyStatus, RemediationAttempt, AnomalyEventUpdate
from app.core.dependencies.service_factory import get_anomaly_event_service
from loguru import logger
from typing import Dict, Any, List, Optional
from datetime import datetime

router = APIRouter(
    prefix="/remediation",
    tags=["Remediation"],
    responses={404: {"description": "Not found"}},
)

@router.get("/events",
           summary="List anomaly events",
           description="Retrieve a list of detected anomaly events with optional filtering by status")
async def list_events(
    status: Optional[str] = None,
    limit: int = 50,
    event_service: AnomalyEventService = Depends(get_anomaly_event_service)
):
    """
    List anomaly events with optional filtering by status.

    Parameters:
        status (str, optional): Filter by event status (e.g., "DETECTED", "REMEDIATION_SUGGESTED", "RESOLVED")
        limit (int, default=50): Maximum number of events to return
        event_service: Service for anomaly event management (injected)

    Returns:
        dict: List of anomaly events containing:
            - status: "success" or "error"
            - count: Number of events returned
            - events: List of anomaly event objects

    Raises:
        HTTPException: If status is invalid or events cannot be retrieved
    """
    try:
        # Convert string status to enum if provided
        status_filter = None
        if status:
            try:
                status_filter = AnomalyStatus(status)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status: {status}. Must be one of: {', '.join([s.value for s in AnomalyStatus])}"
                )

        events = await event_service.list_events(status=status_filter, limit=limit)

        return {
            "status": "success",
            "count": len(events),
            "events": [event.model_dump() for event in events]
        }
    except Exception as e:
        logger.error(f"Error listing events: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list events: {str(e)}"
        )

@router.get("/events/{anomaly_id}",
           summary="Get anomaly event details",
           description="Retrieve detailed information about a specific anomaly event")
async def get_event(
    anomaly_id: str,
    event_service: AnomalyEventService = Depends(get_anomaly_event_service)
):
    """
    Get details of a specific anomaly event.

    Parameters:
        anomaly_id (str): ID of the anomaly event to retrieve
        event_service: Service for anomaly event management (injected)

    Returns:
        dict: Anomaly event details containing:
            - status: "success" or "error"
            - event: Full anomaly event object with all properties

    Raises:
        HTTPException: If event is not found or cannot be retrieved
    """
    try:
        event = await event_service.get_event(anomaly_id)

        if not event:
            raise HTTPException(
                status_code=404,
                detail=f"Anomaly event not found: {anomaly_id}"
            )

        return {
            "status": "success",
            "event": event.model_dump()
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting event {anomaly_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get event: {str(e)}"
        )

@router.post("/events/{anomaly_id}/remediate",
            summary="Apply remediation",
            description="Execute a remediation command to address a detected anomaly")
async def remediate_event(
    anomaly_id: str,
    command: str = Body(..., embed=True),
    event_service: AnomalyEventService = Depends(get_anomaly_event_service)
):
    """
    Apply a remediation command to an anomaly.

    Parameters:
        anomaly_id (str): ID of the anomaly event to remediate
        command (str): Remediation command to execute
                      (e.g., "restart_deployment name=my-deployment namespace=default")
        event_service: Service for anomaly event management (injected)

    Returns:
        dict: Remediation results containing:
            - status: "success" or "error"
            - message: Description of the operation result
            - command: The executed command
            - result: Result of the command execution
            - event: Updated anomaly event object

    Raises:
        HTTPException: If event is not found, command is invalid, or execution fails
    """
    try:
        event = await event_service.get_event(anomaly_id)

        if not event:
            raise HTTPException(
                status_code=404,
                detail=f"Anomaly event not found: {anomaly_id}"
            )

        # Validate command
        validation_result = k8s_executor.parse_and_validate_command(command)
        if not validation_result:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid or unsafe command: {command}"
            )

        operation, params = validation_result

        # Execute command
        result = await k8s_executor.execute_validated_command(operation, params)

        # Record remediation attempt
        attempt = RemediationAttempt(
            command=command,
            parameters=params,
            executor="USER",
            success=True,
            result=result
        )

        updated_event = await event_service.add_remediation_attempt(anomaly_id, attempt)

        return {
            "status": "success",
            "message": "Remediation applied successfully",
            "command": command,
            "result": result,
            "event": updated_event.model_dump() if updated_event else None
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error remediating event {anomaly_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to apply remediation: {str(e)}"
        )

@router.get("/commands",
           summary="List available remediation commands",
           description="Get a list of all available safe Kubernetes remediation commands that can be used")
async def list_commands():
    """
    List all available safe remediation commands that can be executed.

    Returns:
        dict: Available commands containing:
            - status: "success" or "error"
            - commands: Dictionary of available commands with their descriptions and parameter specifications

    Raises:
        HTTPException: If commands cannot be retrieved
    """
    try:
        commands = k8s_executor.get_safe_operations_info()

        return {
            "status": "success",
            "commands": commands
        }
    except Exception as e:
        logger.error(f"Error listing commands: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list commands: {str(e)}"
        )
