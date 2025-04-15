from app.core.config import settings
from loguru import logger
from typing import Optional
import os
import json
import pathlib
from datetime import datetime

class ModeService:
    """
    Service to manage the operation mode of the system.
    Maintains the current mode (MANUAL/AUTO) and exposes methods to get/set it.
    Uses file-based storage for persistence.
    """
    def __init__(self):
        self._current_mode = settings.DEFAULT_REMEDIATION_MODE  # Default from config
        self._config_file = pathlib.Path("data/config/mode.json")
        self._initialized = False
        self._initialize()

    def _initialize(self):
        """Initialize mode from file storage."""
        if self._initialized:
            return

        try:
            # Create directory if it doesn't exist
            self._config_file.parent.mkdir(parents=True, exist_ok=True)

            # Attempt to read mode from file
            if self._config_file.exists():
                with open(self._config_file, 'r') as file:
                    config = json.load(file)
                    if config and "mode" in config:
                        self._current_mode = config["mode"]
                        logger.info(f"Loaded operation mode from file: {self._current_mode}")
            else:
                # Save default mode if file not found
                self._persist_mode()
                logger.info(f"Initialized operation mode in file: {self._current_mode}")

            self._initialized = True
        except Exception as e:
            logger.error(f"Error initializing mode from file: {e}")
            # Fall back to default mode from settings

    def get_mode(self) -> str:
        """Get the current operation mode."""
        return self._current_mode

    async def set_mode(self, mode: str) -> bool:
        """
        Set the operation mode.
        Returns True if successful, False otherwise.
        """
        mode = mode.upper()
        if mode not in ["MANUAL", "AUTO"]:
            logger.error(f"Invalid mode: {mode}. Must be 'MANUAL' or 'AUTO'.")
            return False

        if self._current_mode != mode:
            self._current_mode = mode
            logger.info(f"Operation mode changed to: {mode}")
            self._persist_mode()

        return True

    def _persist_mode(self):
        """Save the current mode to file for persistence."""
        try:
            # Ensure directory exists
            self._config_file.parent.mkdir(parents=True, exist_ok=True)

            # Write mode to file
            config = {
                "mode": self._current_mode,
                "updated_at": datetime.utcnow().isoformat()
            }

            with open(self._config_file, 'w') as file:
                json.dump(config, file, indent=2)

            logger.debug(f"Persisted operation mode to file: {self._current_mode}")
        except Exception as e:
            logger.error(f"Failed to persist mode to file: {e}")

# Global instance
mode_service = ModeService()
