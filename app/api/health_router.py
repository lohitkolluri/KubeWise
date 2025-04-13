from fastapi import APIRouter, Depends
from app.utils.health_checker import HealthChecker
from app.core.config import settings
from loguru import logger

router = APIRouter(
    prefix="/health",
    tags=["Health"],
    responses={404: {"description": "Not found"}},
)

@router.get("/", summary="Health check endpoint", description="Performs a comprehensive health check of all dependent services including MongoDB, Prometheus, and Gemini API")
async def health_check():
    """
    Health check endpoint that verifies all services.

    Returns:
        dict: A dictionary containing overall system health status and individual service statuses
              - overall_status: "healthy" if all services are healthy, otherwise "unhealthy"
              - services: Details about each service's health status
    """
    try:
        health_checker = HealthChecker()
        health_status = await health_checker.check_all()

        all_healthy = all(
            data.get("status") == "healthy"
            for service, data in health_status.items()
            if service != "overall"
        )

        return {
            "overall_status": "healthy" if all_healthy else "unhealthy",
            "services": health_status
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "overall_status": "unhealthy",
            "error": str(e)
        }
