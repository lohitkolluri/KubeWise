import json
import os
import pathlib
from datetime import datetime

from loguru import logger

from app.core.config import settings


class ModeService:
    """
    Service to manage the operation mode of the system.
    Maintains the current mode (learning/assisted/autonomous) and exposes methods to get/set it.
    Uses both .env file and file-based storage for persistence.
    """

    # Valid operation modes
    VALID_MODES = ["learning", "assisted", "autonomous"]

    def __init__(self):
        self._current_mode = settings.DEFAULT_REMEDIATION_MODE  # Default from config
        self._config_file = pathlib.Path("data/config/mode.json")
        self._initialized = False
        self._initialize()

    def _initialize(self):
        """Initialize mode from .env file first, then config file as fallback."""
        if self._initialized:
            return

        try:
            # First try to get mode from .env
            env_mode = os.getenv("KUBEWISE_MODE", "").lower()
            if env_mode in self.VALID_MODES:
                self._current_mode = env_mode
                logger.info(f"Loaded operation mode from .env: {self._current_mode}")
            else:
                # If not in .env or invalid, try the config file
                if self._config_file.exists():
                    with open(self._config_file, "r") as file:
                        config = json.load(file)
                        if config and "mode" in config:
                            file_mode = config["mode"].lower()
                            if file_mode in self.VALID_MODES:
                                self._current_mode = file_mode
                                logger.info(
                                    f"Loaded operation mode from file: {self._current_mode}"
                                )
                            else:
                                logger.warning(f"Invalid mode in config file: {file_mode}. Using default.")

                # Save default mode if mode not found in .env or file
                self._persist_mode()
                logger.info(f"Initialized operation mode in file: {self._current_mode}")

            self._initialized = True
        except Exception as e:
            logger.error(f"Error initializing mode: {e}")
            # Fall back to default mode from settings

    def get_mode(self) -> str:
        """Get the current operation mode."""
        return self._current_mode

    async def set_mode(self, mode: str) -> bool:
        """
        Set the operation mode.
        Returns True if successful, False otherwise.
        """
        mode = mode.lower()
        if mode not in self.VALID_MODES:
            logger.error(f"Invalid mode: {mode}. Must be one of: {', '.join(self.VALID_MODES)}.")
            return False

        if self._current_mode != mode:
            self._current_mode = mode
            logger.info(f"Operation mode changed to: {mode}")

            # Update both .env and the config file for consistency
            self._update_env_file(mode)
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

            with open(self._config_file, "w") as file:
                json.dump(config, file, indent=2)

            logger.debug(f"Persisted operation mode to file: {self._current_mode}")
        except Exception as e:
            logger.error(f"Failed to persist mode to file: {e}")

    def _update_env_file(self, mode: str):
        """Update the .env file with the new mode."""
        try:
            # Path to .env file
            env_file = pathlib.Path(".env")

            # Read existing .env content if it exists
            if env_file.exists():
                with open(env_file, "r") as file:
                    lines = file.readlines()

                # Look for existing KUBEWISE_MODE line to replace
                mode_found = False
                for i, line in enumerate(lines):
                    if line.startswith("KUBEWISE_MODE="):
                        lines[i] = f"KUBEWISE_MODE={mode}\n"
                        mode_found = True
                        break

                # Add the mode if not found
                if not mode_found:
                    lines.append(f"\n# Operation mode (learning, assisted, autonomous)\nKUBEWISE_MODE={mode}\n")

                # Write back to .env
                with open(env_file, "w") as file:
                    file.writelines(lines)
            else:
                # Create new .env file if it doesn't exist
                with open(env_file, "w") as file:
                    file.write(f"# KubeWise configuration\n\n# Operation mode (learning, assisted, autonomous)\nKUBEWISE_MODE={mode}\n")

            logger.debug(f"Updated mode in .env file: {mode}")
        except Exception as e:
            logger.error(f"Failed to update mode in .env file: {e}")


# Global instance
mode_service = ModeService()
