from fastapi import APIRouter
from datetime import datetime

from app.models.api import HealthCheckResponse
from app.core.config import settings

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthCheckResponse)
async def health_check():
    """
    Health check endpoint.

    Returns:
        HealthCheckResponse: Health check information
    """
    # Get the version from the environment or a default value
    version = "0.1.0"  # This would be replaced with actual versioning

    return HealthCheckResponse(
        status="ok",
        version=version,
        timestamp=datetime.utcnow()
    )
