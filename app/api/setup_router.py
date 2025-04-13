from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.core.database import get_database
from app.services.mode_service import mode_service
from app.core.config import settings
from loguru import logger
from typing import Dict, Any, Optional, List
from datetime import datetime
import os

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

@router.get("/mode",
           response_model=ModeResponse,
           summary="Get current operation mode",
           description="Retrieve the current remediation mode (AUTO/MANUAL) of the system")
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
        "message": f"Current operation mode is {current_mode}"
    }

@router.post("/mode",
            response_model=ModeResponse,
            summary="Update operation mode",
            description="Change the system's remediation mode between AUTO (automatic remediation) and MANUAL (suggested remediation)")
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
            detail=f"Invalid mode: {request.mode}. Must be 'AUTO' or 'MANUAL'"
        )

    success = await mode_service.set_mode(mode_upper)

    if success:
        return {
            "mode": mode_upper,
            "success": True,
            "message": f"Operation mode updated to {mode_upper}"
        }
    else:
        raise HTTPException(
            status_code=500,
            detail="Failed to update operation mode"
        )

@router.get("/config",
           summary="Get current configuration",
           description="Retrieve the current application configuration settings (excluding sensitive information)")
async def get_config():
    """
    Get the current configuration settings.

    Returns:
        dict: Configuration data containing:
            - status: "success" or "error"
            - config: Dictionary of configuration settings (sensitive values excluded)
    """
    # Filter out sensitive information
    config = settings.dict(exclude={"MONGODB_ATLAS_URI", "GEMINI_API_KEY"})

    return {
        "status": "success",
        "config": config
    }

@router.post("/settings/env",
           summary="Store environment variables",
           description="Store multiple environment variables in MongoDB for persistence between application restarts")
async def store_env_settings(env_settings: Dict[str, Any], db=Depends(get_database)):
    """
    Store environment variables in MongoDB for persistence.

    Parameters:
        env_settings (Dict[str, Any]): Dictionary of environment variable key-value pairs
        db: MongoDB database connection (injected)

    Returns:
        dict: Operation result containing:
            - status: "success" or "error"
            - message: Description of the operation result

    Raises:
        HTTPException: If no valid settings are provided or storage fails
    """
    try:
        # Filter out empty values
        filtered_settings = {k: v for k, v in env_settings.items() if v is not None and v != ""}

        if not filtered_settings:
            raise HTTPException(
                status_code=400,
                detail="No valid settings provided"
            )

        # Store settings in MongoDB
        await db.system_config.update_one(
            {"_id": "environment_settings"},
            {"$set": {
                "settings": filtered_settings,
                "updated_at": datetime.utcnow()
            }},
            upsert=True
        )

        logger.info(f"Stored {len(filtered_settings)} environment settings")

        return {
            "status": "success",
            "message": f"Successfully stored {len(filtered_settings)} environment settings"
        }
    except Exception as e:
        logger.error(f"Error storing environment settings: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to store environment settings: {str(e)}"
        )

@router.patch("/settings/env",
            summary="Update environment variables",
            description="Update specific environment variables without affecting existing ones")
async def update_env_settings(env_updates: Dict[str, Any], db=Depends(get_database)):
    """
    Update specific environment variables without affecting others.

    Parameters:
        env_updates (Dict[str, Any]): Dictionary of environment variable key-value pairs to update
        db: MongoDB database connection (injected)

    Returns:
        dict: Operation result containing:
            - status: "success" or "error"
            - message: Description of the operation result
            - updated_keys: List of keys that were updated

    Raises:
        HTTPException: If no valid updates are provided or update fails
    """
    try:
        # Filter out empty values
        filtered_updates = {k: v for k, v in env_updates.items() if v is not None}

        if not filtered_updates:
            raise HTTPException(
                status_code=400,
                detail="No valid updates provided"
            )

        # Get current settings
        config_doc = await db.system_config.find_one({"_id": "environment_settings"})
        current_settings = {}

        if config_doc and "settings" in config_doc:
            current_settings = config_doc["settings"]

        # Update settings
        updated_settings = {**current_settings, **filtered_updates}

        # Store updated settings in MongoDB
        await db.system_config.update_one(
            {"_id": "environment_settings"},
            {"$set": {
                "settings": updated_settings,
                "updated_at": datetime.utcnow()
            }},
            upsert=True
        )

        # Apply updates to current environment
        for key, value in filtered_updates.items():
            if value is not None and value != "":
                os.environ[key] = str(value)

        logger.info(f"Updated {len(filtered_updates)} environment settings")

        return {
            "status": "success",
            "message": f"Successfully updated {len(filtered_updates)} environment settings",
            "updated_keys": list(filtered_updates.keys())
        }
    except Exception as e:
        logger.error(f"Error updating environment settings: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update environment settings: {str(e)}"
        )

@router.delete("/settings/env",
             summary="Delete environment variables",
             description="Remove specific environment variables from the stored settings")
async def delete_env_settings(keys: List[str], db=Depends(get_database)):
    """
    Delete specific environment variables from the stored settings.

    Parameters:
        keys (List[str]): List of environment variable keys to delete
        db: MongoDB database connection (injected)

    Returns:
        dict: Operation result containing:
            - status: "success" or "warning"
            - message: Description of the operation result
            - deleted_keys: List of keys that were deleted

    Raises:
        HTTPException: If no keys are provided or deletion fails
    """
    try:
        if not keys:
            raise HTTPException(
                status_code=400,
                detail="No keys provided for deletion"
            )

        # Get current settings
        config_doc = await db.system_config.find_one({"_id": "environment_settings"})
        if not config_doc or "settings" not in config_doc:
            return {
                "status": "warning",
                "message": "No environment settings found to delete from"
            }

        current_settings = config_doc["settings"]

        # Remove specified keys
        deleted_keys = []
        for key in keys:
            if key in current_settings:
                del current_settings[key]
                deleted_keys.append(key)

        if not deleted_keys:
            return {
                "status": "warning",
                "message": "None of the specified keys were found in stored settings"
            }

        # Store updated settings
        await db.system_config.update_one(
            {"_id": "environment_settings"},
            {"$set": {
                "settings": current_settings,
                "updated_at": datetime.utcnow()
            }}
        )

        logger.info(f"Deleted {len(deleted_keys)} environment variables from settings")

        return {
            "status": "success",
            "message": f"Successfully deleted {len(deleted_keys)} environment variables",
            "deleted_keys": deleted_keys,
            "note": "Deleted variables may still be active in the current session until restart"
        }
    except Exception as e:
        logger.error(f"Error deleting environment settings: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete environment settings: {str(e)}"
        )

@router.get("/settings/env",
           summary="Get environment variables",
           description="Retrieve the stored environment variables with sensitive information masked")
async def get_env_settings(db=Depends(get_database)):
    """
    Retrieve the stored environment variables from MongoDB.

    Parameters:
        db: MongoDB database connection (injected)

    Returns:
        dict: Environment settings containing:
            - status: "success" or "error"
            - settings: Dictionary of environment variables (sensitive values masked)
            - last_updated: Timestamp when settings were last updated

    Raises:
        HTTPException: If settings cannot be retrieved
    """
    try:
        # Retrieve settings from MongoDB
        config_doc = await db.system_config.find_one({"_id": "environment_settings"})

        if not config_doc or "settings" not in config_doc:
            return {
                "status": "success",
                "settings": {},
                "message": "No environment settings found"
            }

        # Mask sensitive information
        settings_dict = config_doc["settings"]
        masked_settings = {}

        for key, value in settings_dict.items():
            if any(sensitive in key.upper() for sensitive in ["API_KEY", "SECRET", "PASSWORD", "TOKEN", "URI"]):
                masked_settings[key] = "********"
            else:
                masked_settings[key] = value

        return {
            "status": "success",
            "settings": masked_settings,
            "last_updated": config_doc.get("updated_at", None)
        }

    except Exception as e:
        logger.error(f"Error retrieving environment settings: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve environment settings: {str(e)}"
        )

@router.post("/settings/env/reload",
            summary="Reload environment variables",
            description="Reload stored environment variables and apply them to the current runtime without restarting")
async def reload_env_settings(db=Depends(get_database)):
    """
    Reload environment variables from MongoDB and apply them to the current runtime.

    Parameters:
        db: MongoDB database connection (injected)

    Returns:
        dict: Operation result containing:
            - status: "success" or "warning"
            - message: Description of the operation result
            - applied_variables: List of variables that were applied

    Raises:
        HTTPException: If settings cannot be reloaded
    """
    try:
        # Retrieve stored environment settings
        config_doc = await db.system_config.find_one({"_id": "environment_settings"})

        if not config_doc or "settings" not in config_doc:
            return {
                "status": "warning",
                "message": "No stored environment settings found to reload"
            }

        stored_settings = config_doc["settings"]
        if not stored_settings:
            return {
                "status": "warning",
                "message": "Empty environment settings document found"
            }

        # Apply environment variables
        env_vars_applied = 0
        applied_vars = []

        for key, value in stored_settings.items():
            # Skip if the value is None or empty string
            if value is None or value == "":
                continue

            # Apply environment variable
            os.environ[key] = str(value)
            env_vars_applied += 1

            # Track which variables were applied (mask sensitive values)
            if any(sensitive in key.upper() for sensitive in ["API_KEY", "SECRET", "PASSWORD", "TOKEN", "URI"]):
                applied_vars.append(f"{key}=********")
            else:
                applied_vars.append(f"{key}={value}")

        logger.info(f"Reloaded {env_vars_applied} environment variables from database")

        return {
            "status": "success",
            "message": f"Successfully reloaded {env_vars_applied} environment variables",
            "applied_variables": applied_vars,
            "note": "Some settings may require an application restart to take full effect"
        }

    except Exception as e:
        logger.error(f"Error reloading environment settings: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reload environment settings: {str(e)}"
        )
