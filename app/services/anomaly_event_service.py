from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import uuid
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.core.config import settings
from app.models.anomaly_event import AnomalyEvent, AnomalyEventUpdate, AnomalyStatus, RemediationAttempt

class AnomalyEventService:
    """Service for managing anomaly events."""

    def __init__(self, db: Optional[AsyncIOMotorDatabase] = None):
        """Initialize the anomaly event service."""
        if db is not None:
            self.db = db
        else:
            self.client = AsyncIOMotorClient(settings.MONGODB_ATLAS_URI)
            self.db = self.client[settings.MONGODB_DB_NAME]
        self.collection = self.db.anomaly_events
        logger.info("AnomalyEventService initialized")

    async def create_event(self, event_data: Dict[str, Any]) -> AnomalyEvent:
        """
        Create a new anomaly event.

        Args:
            event_data: Dictionary containing event data

        Returns:
            AnomalyEvent object
        """
        try:
            # Generate unique ID
            event_data["anomaly_id"] = str(uuid.uuid4())
            event_data["status"] = AnomalyStatus.DETECTED
            # Don't set detection_timestamp if already provided
            if "detection_timestamp" not in event_data:
                event_data["detection_timestamp"] = datetime.utcnow()

            # Create event object
            event = AnomalyEvent(**event_data)

            # Insert into database
            await self.collection.insert_one(event.model_dump())
            logger.info(f"Created new anomaly event: {event.anomaly_id}")

            return event

        except Exception as e:
            logger.error(f"Error creating anomaly event: {e}")
            raise

    async def update_event(self, anomaly_id: str, update_data: AnomalyEventUpdate) -> Optional[AnomalyEvent]:
        """
        Update an existing anomaly event.

        Args:
            anomaly_id: ID of the event to update
            update_data: AnomalyEventUpdate object with update data

        Returns:
            Updated AnomalyEvent object or None if not found
        """
        try:
            # Prepare update data
            update_dict = {k: v for k, v in update_data.model_dump().items() if v is not None}

            # If status is being updated to RESOLVED, set resolution_time
            if update_dict.get("status") == AnomalyStatus.RESOLVED and "resolution_time" not in update_dict:
                update_dict["resolution_time"] = datetime.utcnow()

            # Update the event
            result = await self.collection.find_one_and_update(
                {"anomaly_id": anomaly_id},
                {"$set": update_dict},
                return_document=True
            )

            if result:
                logger.info(f"Updated anomaly event: {anomaly_id} with status {update_dict.get('status', 'unchanged')}")
                return AnomalyEvent(**result)
            else:
                logger.warning(f"Anomaly event not found: {anomaly_id}")
                return None

        except Exception as e:
            logger.error(f"Error updating anomaly event: {e}")
            raise

    async def get_event(self, anomaly_id: str) -> Optional[AnomalyEvent]:
        """
        Get an anomaly event by ID.

        Args:
            anomaly_id: ID of the event to retrieve

        Returns:
            AnomalyEvent object or None if not found
        """
        try:
            result = await self.collection.find_one({"anomaly_id": anomaly_id})
            if result:
                return AnomalyEvent(**result)
            return None

        except Exception as e:
            logger.error(f"Error getting anomaly event: {e}")
            raise

    async def find_active_event(self, entity_id: str, entity_type: str, namespace: Optional[str] = None) -> Optional[AnomalyEvent]:
        """
        Find an active event for a given entity.

        Args:
            entity_id: ID of the entity
            entity_type: Type of the entity
            namespace: Namespace of the entity (optional)

        Returns:
            Active AnomalyEvent or None if not found
        """
        try:
            query = {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "status": {"$nin": [AnomalyStatus.RESOLVED, AnomalyStatus.REMEDIATION_FAILED]}
            }

            if namespace:
                query["namespace"] = namespace

            result = await self.collection.find_one(query)

            if result:
                return AnomalyEvent(**result)
            return None

        except Exception as e:
            logger.error(f"Error finding active event: {e}")
            raise

    async def list_events(
        self,
        status: Optional[AnomalyStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100
    ) -> List[AnomalyEvent]:
        """
        List anomaly events with optional filtering.

        Args:
            status: Filter by status
            start_time: Filter by start time
            end_time: Filter by end time
            limit: Maximum number of events to return

        Returns:
            List of AnomalyEvent objects
        """
        try:
            # Build query
            query = {}
            if status:
                query["status"] = status
            if start_time or end_time:
                query["detection_timestamp"] = {}
                if start_time:
                    query["detection_timestamp"]["$gte"] = start_time
                if end_time:
                    query["detection_timestamp"]["$lte"] = end_time

            # Execute query
            cursor = self.collection.find(query).sort("detection_timestamp", -1).limit(limit)
            events = await cursor.to_list(length=limit)

            return [AnomalyEvent(**event) for event in events]

        except Exception as e:
            logger.error(f"Error listing anomaly events: {e}")
            raise

    async def add_remediation_attempt(
        self,
        anomaly_id: str,
        attempt: RemediationAttempt
    ) -> Optional[AnomalyEvent]:
        """
        Add a remediation attempt to an anomaly event and schedule verification.

        Args:
            anomaly_id: ID of the event
            attempt: RemediationAttempt object

        Returns:
            Updated AnomalyEvent object or None if not found
        """
        try:
            # Convert attempt to dict
            attempt_dict = attempt.model_dump()

            # Calculate verification time
            verification_time = datetime.utcnow() + timedelta(seconds=settings.VERIFICATION_DELAY_SECONDS)

            # Update the event
            result = await self.collection.find_one_and_update(
                {"anomaly_id": anomaly_id},
                {
                    "$push": {"remediation_attempts": attempt_dict},
                    "$set": {
                        "status": AnomalyStatus.VERIFICATION_PENDING,
                        "verification_time": verification_time
                    }
                },
                return_document=True
            )

            if result:
                logger.info(f"Added remediation attempt to anomaly event: {anomaly_id}. Verification scheduled at {verification_time}")
                return AnomalyEvent(**result)
            else:
                logger.warning(f"Anomaly event not found: {anomaly_id}")
                return None

        except Exception as e:
            logger.error(f"Error adding remediation attempt: {e}")
            raise
