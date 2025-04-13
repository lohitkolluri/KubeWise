from app.core.config import settings
from loguru import logger
from typing import Optional
import asyncio
from datetime import datetime

class ModeService:
    """
    Service to manage the operation mode of the system.
    Maintains the current mode (MANUAL/AUTO) and exposes methods to get/set it.
    Now supports persistent storage in MongoDB.
    """
    def __init__(self):
        self._current_mode = settings.DEFAULT_REMEDIATION_MODE  # Default from config
        self._db = None  # Will be set after service initialization
        self._initialized = False

    def set_db(self, db_instance):
        """Set the database instance for persistence."""
        self._db = db_instance

    async def initialize(self):
        """Initialize mode from database."""
        if self._initialized or not self._db:
            return

        try:
            # Attempt to retrieve mode from database
            config_doc = await self._db.system_config.find_one({"_id": "operation_mode"})
            if config_doc and "mode" in config_doc:
                self._current_mode = config_doc["mode"]
                logger.info(f"Loaded operation mode from database: {self._current_mode}")
            else:
                # Save default mode if not found
                await self._persist_mode()
                logger.info(f"Initialized operation mode in database: {self._current_mode}")

            self._initialized = True
        except Exception as e:
            logger.error(f"Error initializing mode from database: {e}")
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

            # Persist to database if available
            if self._db:
                await self._persist_mode()

        return True

    async def _persist_mode(self):
        """Save the current mode to MongoDB for persistence."""
        if not self._db:
            return

        try:
            await self._db.system_config.update_one(
                {"_id": "operation_mode"},
                {"$set": {
                    "mode": self._current_mode,
                    "updated_at": datetime.utcnow()
                }},
                upsert=True
            )
            logger.debug(f"Persisted operation mode to database: {self._current_mode}")
        except Exception as e:
            logger.error(f"Failed to persist mode to database: {e}")

    async def wait_for_db(self, max_attempts: int = 5, retry_delay: int = 2):
        """Wait for database to be available and then initialize."""
        attempts = 0
        while attempts < max_attempts:
            if self._db:
                await self.initialize()
                return True

            logger.warning(f"Database not yet available for mode service, retrying... ({attempts+1}/{max_attempts})")
            attempts += 1
            await asyncio.sleep(retry_delay)

        logger.error("Failed to initialize mode service with database after maximum attempts")
        return False

# Global instance
mode_service = ModeService()
