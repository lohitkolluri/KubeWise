from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.models.api import RemediationRequest, RemediationResponse
from app.services.remediation_executor import remediation_executor
from app.services.supabase_client import supabase_service

router = APIRouter(prefix="/remediate", tags=["remediate"])

# Simple API key auth for the remediation endpoint
api_key_header = APIKeyHeader(name="X-API-Key")


async def verify_api_key(api_key: str = Security(api_key_header)):
    """
    Verify the API key for the remediation endpoint.

    Args:
        api_key: API key from the request header

    Returns:
        The API key if valid

    Raises:
        HTTPException: If API key is invalid
    """
    # In a real application, you would use a more secure method
    # This is just a simple example
    expected_key = "YOUR_API_KEY_HERE"  # This should be loaded from environment variables

    if api_key != expected_key:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key"
        )

    return api_key


@router.post("/{issue_id}", response_model=RemediationResponse)
async def remediate_issue(
    issue_id: UUID,
    request: RemediationRequest,
    api_key: str = Depends(verify_api_key)
):
    """
    Execute remediation for an issue.

    Args:
        issue_id: UUID of the issue to remediate
        request: Remediation request with confirmation
        api_key: API key for authentication

    Returns:
        Result of the remediation

    Raises:
        HTTPException: If issue not found or remediation failed
    """
    # Check if issue exists
    issue = await supabase_service.get_issue_by_id(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail=f"Issue {issue_id} not found")

    # Check if confirmation is provided
    if not request.confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required to execute remediation. Set 'confirm' to true."
        )

    # Execute remediation
    result = await remediation_executor.execute_remediation(issue_id)

    # Return result
    return result
