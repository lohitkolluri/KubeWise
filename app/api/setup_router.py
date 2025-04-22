import os

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.config import settings
from app.services.mode_service import mode_service

router = APIRouter(
    prefix="/setup",
    tags=["Setup"],
    responses={404: {"description": "Not found"}},
)


class ModeUpdateRequest(BaseModel):
    """Request model for updating operation mode."""

    mode: str


class ModeResponse(BaseModel):
    """Response model containing system operation mode."""

    mode: str
    success: bool
    message: str


@router.get(
    "/mode",
    response_model=ModeResponse,
    summary="Get current operation mode",
    description="Retrieve the current remediation mode (AUTO/MANUAL) of the system",
)
async def get_current_mode():
    """
    Get the current operating mode of the system.

    Returns:
        ModeResponse: Object containing:
            - mode: Current mode ("AUTO" or "MANUAL")
            - success: Whether the operation was successful
            - message: Description of the current mode
    """
    current_mode = mode_service.get_mode()
    return {
        "mode": current_mode,
        "success": True,
        "message": f"Current operation mode is {current_mode}",
    }


@router.put(
    "/mode",
    response_model=ModeResponse,
    summary="Update operation mode",
    description="Change the system's remediation mode between AUTO (automatic remediation) and MANUAL (suggested remediation)",
)
async def update_mode(request: ModeUpdateRequest):
    """
    Update the operating mode of the system.

    Parameters:
        request (ModeUpdateRequest): Object containing:
            - mode: The new mode to set ("AUTO" or "MANUAL")

    Returns:
        ModeResponse: Object containing:
            - mode: The updated mode
            - success: Whether the update was successful
            - message: Description of the update result

    Raises:
        HTTPException: If the mode is invalid or update fails
    """
    mode_upper = request.mode.upper()
    if mode_upper not in ["AUTO", "MANUAL"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode: {request.mode}. Must be 'AUTO' or 'MANUAL'",
        )

    success = await mode_service.set_mode(mode_upper)

    if success:
        return {
            "mode": mode_upper,
            "success": True,
            "message": f"Operation mode updated to {mode_upper}",
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to update operation mode")


@router.get(
    "/config",
    summary="Get current configuration",
    description="Retrieve the current application configuration settings (excluding sensitive information)",
)
async def get_config():
    """
    Get the current configuration settings.

    Returns:
        dict: Configuration data containing:
            - status: "success" or "error"
            - config: Dictionary of configuration settings (sensitive values excluded)
    """
    # Filter out sensitive information
    config = settings.model_dump(exclude={"GEMINI_API_KEY"})

    return {
        "status": "success",
        "config": config,
        "note": "Configuration is sourced from environment variables (.env file)",
    }


@router.get(
    "/settings/env",
    summary="Get current environment variables",
    description="Retrieve the current environment variables with sensitive information masked",
)
async def get_env_settings():
    """
    Retrieve the current environment variables.

    Returns:
        dict: Environment settings containing:
            - status: "success" or "error"
            - settings: Dictionary of environment variables (sensitive values masked)

    Note:
        To modify settings, please update your .env file directly.
    """
    try:
        # Get relevant environment variables (those used in settings)
        env_vars = {}
        for key in settings.model_fields.keys():
            if key in os.environ:
                # Mask sensitive information
                if any(
                    sensitive in key.upper()
                    for sensitive in ["API_KEY", "SECRET", "PASSWORD", "TOKEN", "URI"]
                ):
                    env_vars[key] = "********"
                else:
                    env_vars[key] = os.environ[key]

        return {
            "status": "success",
            "settings": env_vars,
            "note": "To modify settings, please update your .env file and restart the application.",
        }
    except Exception as e:
        logger.error(f"Error retrieving environment settings: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve environment settings: {str(e)}"
        )
