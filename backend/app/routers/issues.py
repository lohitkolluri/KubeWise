from typing import List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Depends
from datetime import datetime

from app.models.api import (
    IssueResponse, IssuesListParams, IssueStatusUpdate
)
from app.models.database import IssueStatus
from app.services.supabase_client import supabase_service

router = APIRouter(prefix="/issues", tags=["issues"])


async def parse_list_params(
    status: List[str] = Query(None),
    severity: List[str] = Query(None),
    k8s_object_type: List[str] = Query(None),
    namespace: str = Query(None),
    start_time: datetime = Query(None),
    end_time: datetime = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0)
) -> IssuesListParams:
    """
    Parse and validate list parameters.
    """
    # Convert string values to enum types
    status_enums = None
    if status:
        status_enums = [IssueStatus(s) for s in status]

    return IssuesListParams(
        status=status_enums,
        severity=severity,
        k8s_object_type=k8s_object_type,
        namespace=namespace,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset
    )


@router.get("", response_model=List[IssueResponse])
async def list_issues(params: IssuesListParams = Depends(parse_list_params)):
    """
    List issues with optional filtering.

    Args:
        params: Query parameters for filtering

    Returns:
        List of issues matching the criteria
    """
    issues = await supabase_service.list_issues(
        status=params.status,
        severity=params.severity,
        k8s_object_type=params.k8s_object_type,
        namespace=params.namespace,
        start_time=params.start_time,
        end_time=params.end_time,
        limit=params.limit,
        offset=params.offset
    )

    return issues


@router.get("/{issue_id}", response_model=IssueResponse)
async def get_issue(issue_id: UUID):
    """
    Get a specific issue by ID.

    Args:
        issue_id: UUID of the issue to retrieve

    Returns:
        Issue details

    Raises:
        HTTPException: If issue not found
    """
    issue = await supabase_service.get_issue_by_id(issue_id)

    if not issue:
        raise HTTPException(status_code=404, detail=f"Issue {issue_id} not found")

    return issue


@router.post("/{issue_id}/acknowledge", response_model=IssueResponse)
async def acknowledge_issue(issue_id: UUID, update: IssueStatusUpdate):
    """
    Update the status of an issue.

    Args:
        issue_id: UUID of the issue to update
        update: New status for the issue

    Returns:
        Updated issue

    Raises:
        HTTPException: If issue not found
    """
    updated_issue = await supabase_service.update_issue_status(issue_id, update.status)

    if not updated_issue:
        raise HTTPException(status_code=404, detail=f"Issue {issue_id} not found")

    return updated_issue
