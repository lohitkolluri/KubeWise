import asyncio
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from loguru import logger

from app.core.dependencies.service_factory import get_anomaly_event_service
from app.models.anomaly_event import AnomalyStatus, RemediationAttempt
from app.models.ai_remediation_models import WorkflowStep
from app.services.anomaly_event_service import AnomalyEventService
from app.services.ai_remediation_service import ai_remediation_service
from app.utils.k8s_executor import k8s_executor

router = APIRouter(
    prefix="/remediation",
    tags=["Remediation"],
    responses={404: {"description": "Not found"}},
)


@router.get(
    "/events",
    summary="List anomaly events",
    description="Retrieve a list of detected anomaly events with optional filtering by status",
)
async def list_events(
    status: Optional[str] = None,
    limit: int = 50,
    event_service: AnomalyEventService = Depends(get_anomaly_event_service),
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
                    detail=f"Invalid status: {status}. Must be one of: {', '.join([s.value for s in AnomalyStatus])}",
                )

        events = await event_service.list_events(status=status_filter, limit=limit)

        return {
            "status": "success",
            "count": len(events),
            "events": [event.model_dump() for event in events],
        }
    except Exception as e:
        logger.error(f"Error listing events: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list events: {str(e)}")


@router.get(
    "/events/{anomaly_id}",
    summary="Get anomaly event details",
    description="Retrieve detailed information about a specific anomaly event",
)
async def get_event(
    anomaly_id: str,
    event_service: AnomalyEventService = Depends(get_anomaly_event_service),
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
                status_code=404, detail=f"Anomaly event not found: {anomaly_id}"
            )

        return {"status": "success", "event": event.model_dump()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting event {anomaly_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get event: {str(e)}")


@router.post(
    "/events/{anomaly_id}/remediate",
    summary="Apply remediation",
    description="Execute a remediation command to address a detected anomaly",
)
async def remediate_event(
    anomaly_id: str,
    command: str = Body(..., embed=True),
    event_service: AnomalyEventService = Depends(get_anomaly_event_service),
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
                status_code=404, detail=f"Anomaly event not found: {anomaly_id}"
            )

        # Validate command
        validation_result = k8s_executor.parse_and_validate_command(command)
        if not validation_result:
            raise HTTPException(
                status_code=400, detail=f"Invalid or unsafe command: {command}"
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
            result=result,
        )

        updated_event = await event_service.add_remediation_attempt(anomaly_id, attempt)

        return {
            "status": "success",
            "message": "Remediation applied successfully",
            "command": command,
            "result": result,
            "event": updated_event.model_dump() if updated_event else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error remediating event {anomaly_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to apply remediation: {str(e)}"
        )


@router.get(
    "/commands",
    summary="List available remediation commands",
    description="Get a list of all available safe Kubernetes remediation commands that can be used",
)
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

        return {"status": "success", "commands": commands}
    except Exception as e:
        logger.error(f"Error listing commands: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to list commands: {str(e)}"
        )


# New AI-Powered Remediation Endpoints

@router.post(
    "/events/{anomaly_id}/ai-analyze",
    summary="Generate AI remediation plan",
    description="Analyze the anomaly and generate an AI-powered remediation plan",
)
async def generate_ai_remediation_plan(
    anomaly_id: str,
):
    """
    Generate an AI-powered remediation plan for the anomaly.

    Parameters:
        anomaly_id (str): ID of the anomaly event to analyze

    Returns:
        dict: Remediation plan generation result containing:
            - status: "success" or "error"
            - workflow: Information about the created workflow
            - message: Description of the operation

    Raises:
        HTTPException: If event is not found or plan cannot be generated
    """
    try:
        # Start the workflow but don't wait for completion
        workflow = await ai_remediation_service.start_workflow(anomaly_id)

        return {
            "status": "success",
            "message": f"AI remediation plan generation started for anomaly {anomaly_id}",
            "workflow": {
                "anomaly_id": workflow.anomaly_id,
                "current_step": workflow.current_step,
                "started_at": workflow.started_at,
            }
        }
    except Exception as e:
        logger.error(f"Error starting AI remediation for {anomaly_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to start AI remediation: {str(e)}"
        )


@router.post(
    "/events/{anomaly_id}/ai-remediate",
    summary="Apply AI remediation",
    description="Execute the AI-generated remediation plan for an anomaly",
)
async def execute_ai_remediation(
    anomaly_id: str,
    confidence_threshold: float = Body(0.75, embed=True),
):
    """
    Execute the AI-generated remediation plan for the anomaly.

    Parameters:
        anomaly_id (str): ID of the anomaly event to remediate
        confidence_threshold (float): Confidence threshold required to execute (0.0-1.0)

    Returns:
        dict: Remediation execution results

    Raises:
        HTTPException: If event is not found, plan doesn't exist, or execution fails
    """
    try:
        # First check if there's a plan
        plan = await ai_remediation_service.get_remediation_plan(anomaly_id)
        
        if not plan:
            # Generate a plan first
            workflow = await ai_remediation_service.start_workflow(anomaly_id)
            
            # Wait for the plan generation to complete (up to 60 seconds)
            MAX_WAIT = 60
            for _ in range(MAX_WAIT):
                if workflow.current_step in [WorkflowStep.PLAN_GENERATION, WorkflowStep.COMPLETED, WorkflowStep.FAILED]:
                    break
                await asyncio.sleep(1)
                
            # Check if plan was generated
            plan = await ai_remediation_service.get_remediation_plan(anomaly_id)
            
            if not plan:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to generate remediation plan for anomaly {anomaly_id}"
                )
                
        # Check confidence
        confidence = plan.get("confidence", 0)
        if confidence < confidence_threshold:
            return {
                "status": "rejected",
                "message": f"Remediation confidence ({confidence}) below threshold ({confidence_threshold})",
                "plan": plan
            }
            
        # Execute the plan
        result = await ai_remediation_service.execute_remediation_plan(anomaly_id)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing AI remediation for {anomaly_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to execute AI remediation: {str(e)}"
        )


@router.get(
    "/events/{anomaly_id}/ai-plan",
    summary="Get AI remediation plan",
    description="Retrieve the AI-generated remediation plan for an anomaly",
)
async def get_ai_remediation_plan(
    anomaly_id: str,
):
    """
    Get the AI-generated remediation plan for an anomaly.

    Parameters:
        anomaly_id (str): ID of the anomaly event

    Returns:
        dict: Remediation plan or status information

    Raises:
        HTTPException: If event is not found or plan cannot be retrieved
    """
    try:
        plan = await ai_remediation_service.get_remediation_plan(anomaly_id)
        
        if plan:
            return {
                "status": "success",
                "message": "Remediation plan retrieved",
                "plan": plan
            }
        else:
            # Check if a workflow exists
            workflow_list = await ai_remediation_service.list_workflows()
            workflow = next((w for w in workflow_list if w["anomaly_id"] == anomaly_id), None)
            
            if workflow:
                return {
                    "status": "pending",
                    "message": f"Remediation plan generation in progress. Current step: {workflow['current_step']}",
                    "workflow": workflow
                }
            else:
                return {
                    "status": "not_found",
                    "message": "No remediation plan or workflow exists for this anomaly"
                }
            
    except Exception as e:
        logger.error(f"Error getting AI remediation plan for {anomaly_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to get remediation plan: {str(e)}"
        )


@router.get(
    "/workflows",
    summary="List remediation workflows",
    description="List all active AI remediation workflows",
)
async def list_workflows():
    """
    List all active remediation workflows.

    Returns:
        dict: List of active workflows

    Raises:
        HTTPException: If workflows cannot be retrieved
    """
    try:
        workflows = await ai_remediation_service.list_workflows()
        return {
            "status": "success",
            "count": len(workflows),
            "workflows": workflows
        }
    except Exception as e:
        logger.error(f"Error listing workflows: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to list workflows: {str(e)}"
        )
