import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import database, get_async_db
from app.models.anomaly_event import (
    AnomalyEvent,
    AnomalyEventUpdate,
    AnomalyStatus,
    RemediationAttempt,
)
from app.models.db_models import AnomalyEventDB, RemediationAttemptDB


class AnomalyEventService:
    """Service for managing anomaly events using SQLite database storage."""

    def __init__(self):
        """Initialize the anomaly event service."""
        # Ensure database tables are created
        database.create_tables()

        # Add locks for concurrent operations on shared resources
        self._db_lock = asyncio.Lock()

        # Track active operations with correlation IDs
        self._active_operations = {}

        logger.info("AnomalyEventService initialized with SQLite storage")

    async def create_event(self, event_data: Dict[str, Any]) -> AnomalyEvent:
        """
        Create a new anomaly event.

        Args:
            event_data: Dictionary containing event data

        Returns:
            AnomalyEvent object

        Raises:
            ValueError: If event data is invalid
            DatabaseError: If database operation fails
        """
        # Generate operation ID for tracking
        operation_id = str(uuid.uuid4())[:8]
        logger.info(
            f"[OperationID:{operation_id}] Creating new anomaly event for {event_data.get('entity_type')}/{event_data.get('entity_id')}"
        )

        try:
            # Generate unique ID if not provided
            if "anomaly_id" not in event_data:
                event_data["anomaly_id"] = str(uuid.uuid4())

            # Set default status if not provided
            if "status" not in event_data:
                event_data["status"] = AnomalyStatus.DETECTED

            # Don't set detection_timestamp if already provided
            if "detection_timestamp" not in event_data:
                event_data["detection_timestamp"] = datetime.utcnow()

            # Fix metric_snapshot format if it's incorrectly nested
            if "metric_snapshot" in event_data:
                # Check if metric_snapshot is a list containing a single list instead of list of dicts
                if (
                    isinstance(event_data["metric_snapshot"], list)
                    and len(event_data["metric_snapshot"]) > 0
                    and isinstance(event_data["metric_snapshot"][0], list)
                ):
                    # Flatten the structure
                    event_data["metric_snapshot"] = event_data["metric_snapshot"][0]

                # Ensure it's a list of dictionaries
                if not event_data["metric_snapshot"] or not isinstance(
                    event_data["metric_snapshot"][0], dict
                ):
                    # If empty or invalid, initialize with an empty dict in the list
                    event_data["metric_snapshot"] = [{}]

            # Create Pydantic event object to validate data
            try:
                event = AnomalyEvent(**event_data)
            except Exception as validation_error:
                logger.error(
                    f"[OperationID:{operation_id}] Event data validation failed: {validation_error}"
                )
                raise ValueError(
                    f"Invalid event data: {validation_error}"
                ) from validation_error

            # Extract remediation attempts if any
            remediation_attempts = []
            if event.remediation_attempts:
                remediation_attempts = [
                    RemediationAttemptDB(
                        anomaly_event_id=event.anomaly_id,
                        command=attempt.command,
                        parameters=attempt.parameters,
                        executor=attempt.executor,
                        timestamp=attempt.timestamp,
                        success=attempt.success,
                        result=attempt.result,
                        error=attempt.error,
                        is_proactive=attempt.is_proactive,
                    )
                    for attempt in event.remediation_attempts
                ]

            # Prepare suggested_remediation_commands for database
            suggested_remediation_commands = None
            if event.suggested_remediation_commands:
                suggested_remediation_commands = event.suggested_remediation_commands

            # Create SQLAlchemy model instance
            db_event = AnomalyEventDB(
                anomaly_id=event.anomaly_id,
                status=event.status.value,
                detection_timestamp=event.detection_timestamp,
                entity_id=event.entity_id,
                entity_type=event.entity_type,
                metric_snapshot=event.metric_snapshot,
                anomaly_score=event.anomaly_score,
                namespace=event.namespace,
                suggested_remediation_commands=suggested_remediation_commands,
                resolution_time=event.resolution_time,
                ai_analysis=event.ai_analysis,
                notes=event.notes,
                verification_time=event.verification_time,
                prediction_data=event.prediction_data,
                is_proactive=event.is_proactive,
            )

            # Use lock for database operations to prevent race conditions
            async with self._db_lock:
                # Track operation
                self._active_operations[operation_id] = {
                    "type": "create",
                    "entity_id": event.entity_id,
                    "entity_type": event.entity_type,
                    "start_time": datetime.utcnow(),
                }

                try:
                    # Get async database session
                    async for session in get_async_db():
                        # Add to database
                        session.add(db_event)

                        # Add remediation attempts if any
                        if remediation_attempts:
                            for attempt in remediation_attempts:
                                session.add(attempt)

                        # Commit transaction
                        await session.commit()

                        # Refresh the object to get generated IDs
                        await session.refresh(db_event)

                    # Complete operation tracking
                    self._active_operations[operation_id][
                        "end_time"
                    ] = datetime.utcnow()
                    self._active_operations[operation_id]["status"] = "success"

                except Exception as db_error:
                    # Update operation tracking with failure
                    self._active_operations[operation_id][
                        "end_time"
                    ] = datetime.utcnow()
                    self._active_operations[operation_id]["status"] = "error"
                    self._active_operations[operation_id]["error"] = str(db_error)

                    logger.error(
                        f"[OperationID:{operation_id}] Database error creating anomaly event: {db_error}"
                    )
                    # Use a more specific exception type
                    from sqlalchemy.exc import SQLAlchemyError

                    if isinstance(db_error, SQLAlchemyError):
                        raise SQLAlchemyError(
                            f"Database error: {db_error}"
                        ) from db_error
                    else:
                        raise RuntimeError(
                            f"Error during database operation: {db_error}"
                        ) from db_error

            logger.info(
                f"[OperationID:{operation_id}] Created new anomaly event: {event.anomaly_id} for {event.entity_type}/{event.entity_id}"
            )
            return event

        except ValueError:
            # Re-raise validation errors
            raise
        except Exception as e:
            logger.error(
                f"[OperationID:{operation_id}] Unexpected error creating anomaly event: {e}",
                exc_info=True,
            )
            raise

    async def update_event(
        self, anomaly_id: str, update_data: AnomalyEventUpdate
    ) -> Optional[AnomalyEvent]:
        """
        Update an existing anomaly event.

        Args:
            anomaly_id: ID of the event to update
            update_data: AnomalyEventUpdate object with update data

        Returns:
            Updated AnomalyEvent object or None if not found

        Raises:
            ValueError: If update data is invalid
            DatabaseError: If database operation fails
            NotFoundError: If event not found
        """
        # Generate operation ID for tracking
        operation_id = str(uuid.uuid4())[:8]
        logger.info(
            f"[OperationID:{operation_id}] Updating anomaly event: {anomaly_id}"
        )

        try:
            # Validate update data
            if not isinstance(update_data, AnomalyEventUpdate):
                try:
                    # Try to convert to AnomalyEventUpdate if it's a dict
                    if isinstance(update_data, dict):
                        update_data = AnomalyEventUpdate(**update_data)
                    else:
                        raise ValueError(
                            "update_data must be AnomalyEventUpdate or dict"
                        )
                except Exception as validation_error:
                    logger.error(
                        f"[OperationID:{operation_id}] Update data validation failed: {validation_error}"
                    )
                    raise ValueError(
                        f"Invalid update data: {validation_error}"
                    ) from validation_error

            # Prepare update data dict
            update_dict = {
                k: v for k, v in update_data.model_dump().items() if v is not None
            }

            # If status is being updated to RESOLVED, set resolution_time
            if (
                update_dict.get("status") == AnomalyStatus.RESOLVED
                and "resolution_time" not in update_dict
            ):
                update_dict["resolution_time"] = datetime.utcnow()

            # Convert status enum to string value if present
            if "status" in update_dict:
                update_dict["status"] = update_dict["status"].value

            # Save remediation attempts separately
            new_attempts = []
            if "remediation_attempts" in update_dict:
                new_attempts = update_dict.pop("remediation_attempts")

            # Use lock for database operations to prevent race conditions
            async with self._db_lock:
                # Track operation
                self._active_operations[operation_id] = {
                    "type": "update",
                    "anomaly_id": anomaly_id,
                    "start_time": datetime.utcnow(),
                }

                try:
                    # Get async database session
                    async for session in get_async_db():
                        # First check if the event exists
                        stmt = select(AnomalyEventDB).where(
                            AnomalyEventDB.anomaly_id == anomaly_id
                        )
                        result = await session.execute(stmt)
                        db_event = result.scalars().first()

                        if not db_event:
                            logger.warning(
                                f"[OperationID:{operation_id}] Anomaly event not found: {anomaly_id}"
                            )
                            # Update operation tracking
                            self._active_operations[operation_id][
                                "end_time"
                            ] = datetime.utcnow()
                            self._active_operations[operation_id][
                                "status"
                            ] = "not_found"

                            # Use a specific exception for not found
                            class NotFoundError(Exception):
                                pass

                            raise NotFoundError(
                                f"Anomaly event not found: {anomaly_id}"
                            )

                        # Track entity info in operation
                        self._active_operations[operation_id][
                            "entity_id"
                        ] = db_event.entity_id
                        self._active_operations[operation_id][
                            "entity_type"
                        ] = db_event.entity_type

                        # Add remediation attempts if any
                        for attempt_data in new_attempts:
                            # Convert dict to RemediationAttempt if needed
                            if isinstance(attempt_data, dict):
                                # If it's a dict, convert to RemediationAttempt
                                attempt_data = RemediationAttempt(**attempt_data)

                            # Create new remediation attempt
                            new_attempt = RemediationAttemptDB(
                                anomaly_event_id=anomaly_id,
                                command=attempt_data.command,
                                parameters=attempt_data.parameters,
                                executor=attempt_data.executor,
                                timestamp=attempt_data.timestamp,
                                success=attempt_data.success,
                                result=attempt_data.result,
                                error=attempt_data.error,
                                is_proactive=attempt_data.is_proactive,
                            )
                            session.add(new_attempt)

                        # Update event fields
                        for key, value in update_dict.items():
                            setattr(db_event, key, value)

                        # Commit the changes
                        await session.commit()
                        await session.refresh(db_event)

                        # Load remediation attempts - use a new session for reading
                        stmt = select(RemediationAttemptDB).where(
                            RemediationAttemptDB.anomaly_event_id == anomaly_id
                        )
                        result = await session.execute(stmt)
                        remediation_attempts = result.scalars().all()

                        # Convert DB model to Pydantic model for return
                        remediation_attempt_models = []
                        for attempt in remediation_attempts:
                            remediation_attempt_models.append(
                                RemediationAttempt(
                                    command=attempt.command,
                                    parameters=attempt.parameters,
                                    executor=attempt.executor,
                                    timestamp=attempt.timestamp,
                                    success=attempt.success,
                                    result=attempt.result,
                                    error=attempt.error,
                                    is_proactive=attempt.is_proactive,
                                )
                            )

                        # Build the complete return model
                        event = AnomalyEvent(
                            anomaly_id=db_event.anomaly_id,
                            status=AnomalyStatus(db_event.status),
                            detection_timestamp=db_event.detection_timestamp,
                            entity_id=db_event.entity_id,
                            entity_type=db_event.entity_type,
                            metric_snapshot=db_event.metric_snapshot,
                            anomaly_score=db_event.anomaly_score,
                            namespace=db_event.namespace,
                            suggested_remediation_commands=db_event.suggested_remediation_commands,
                            remediation_attempts=remediation_attempt_models,
                            resolution_time=db_event.resolution_time,
                            ai_analysis=db_event.ai_analysis,
                            notes=db_event.notes,
                            verification_time=db_event.verification_time,
                            prediction_data=db_event.prediction_data,
                            is_proactive=db_event.is_proactive,
                        )

                        # Update operation tracking with success
                        self._active_operations[operation_id][
                            "end_time"
                        ] = datetime.utcnow()
                        self._active_operations[operation_id]["status"] = "success"

                        # Log status change if applicable
                        if "status" in update_dict:
                            new_status = AnomalyStatus(update_dict["status"])
                            logger.info(
                                f"[OperationID:{operation_id}] Event {anomaly_id} status changed to {new_status.name}"
                            )

                        logger.info(
                            f"[OperationID:{operation_id}] Updated anomaly event: {anomaly_id}"
                        )
                        return event

                except Exception as db_error:
                    # Update operation tracking with failure
                    self._active_operations[operation_id][
                        "end_time"
                    ] = datetime.utcnow()
                    self._active_operations[operation_id]["status"] = "error"
                    self._active_operations[operation_id]["error"] = str(db_error)

                    # Re-raise appropriate exception
                    logger.error(
                        f"[OperationID:{operation_id}] Error updating anomaly event: {db_error}"
                    )
                    raise

        except Exception as e:
            if isinstance(e, ValueError) or "NotFoundError" in str(type(e)):
                # Re-raise validation or not found errors
                raise
            logger.error(
                f"[OperationID:{operation_id}] Unexpected error updating anomaly event: {e}",
                exc_info=True,
            )
            raise RuntimeError(f"Unexpected error updating anomaly event: {e}") from e

    async def get_event(self, anomaly_id: str) -> Optional[AnomalyEvent]:
        """
        Get an anomaly event by ID.

        Args:
            anomaly_id: ID of the event to retrieve

        Returns:
            AnomalyEvent object or None if not found
        """
        try:
            async for session in get_async_db():
                # Query for the event with remediation attempts
                stmt = select(AnomalyEventDB).where(
                    AnomalyEventDB.anomaly_id == anomaly_id
                )
                result = await session.execute(stmt)
                db_event = result.scalars().first()

                if not db_event:
                    return None

                # Load remediation attempts
                stmt = select(RemediationAttemptDB).where(
                    RemediationAttemptDB.anomaly_event_id == anomaly_id
                )
                result = await session.execute(stmt)
                remediation_attempts = result.scalars().all()

                # Convert remediation attempts to Pydantic models
                remediation_attempt_models = []
                for attempt in remediation_attempts:
                    remediation_attempt_models.append(
                        RemediationAttempt(
                            command=attempt.command,
                            parameters=attempt.parameters,
                            executor=attempt.executor,
                            timestamp=attempt.timestamp,
                            success=attempt.success,
                            result=attempt.result,
                            error=attempt.error,
                            is_proactive=attempt.is_proactive,
                        )
                    )

                # Convert DB model to Pydantic model
                event = AnomalyEvent(
                    anomaly_id=db_event.anomaly_id,
                    status=AnomalyStatus(db_event.status),
                    detection_timestamp=db_event.detection_timestamp,
                    entity_id=db_event.entity_id,
                    entity_type=db_event.entity_type,
                    metric_snapshot=db_event.metric_snapshot,
                    anomaly_score=db_event.anomaly_score,
                    namespace=db_event.namespace,
                    suggested_remediation_commands=db_event.suggested_remediation_commands,
                    remediation_attempts=remediation_attempt_models,
                    resolution_time=db_event.resolution_time,
                    ai_analysis=db_event.ai_analysis,
                    notes=db_event.notes,
                    verification_time=db_event.verification_time,
                    prediction_data=db_event.prediction_data,
                    is_proactive=db_event.is_proactive,
                )

                return event

        except Exception as e:
            logger.error(f"Error getting anomaly event from SQLite: {e}")
            raise

    async def find_active_event(
        self, entity_id: str, entity_type: str, namespace: Optional[str] = None
    ) -> Optional[AnomalyEvent]:
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
            async for session in get_async_db():
                # Build the query base
                query = select(AnomalyEventDB).where(
                    AnomalyEventDB.entity_id == entity_id,
                    AnomalyEventDB.entity_type == entity_type,
                    AnomalyEventDB.status.not_in(
                        [
                            AnomalyStatus.RESOLVED.value,
                            AnomalyStatus.REMEDIATION_FAILED.value,
                        ]
                    ),
                )

                # Add namespace filter if provided
                if namespace:
                    query = query.where(AnomalyEventDB.namespace == namespace)

                # Execute the query
                result = await session.execute(query)
                db_event = result.scalars().first()

                if not db_event:
                    return None

                # Load remediation attempts
                stmt = select(RemediationAttemptDB).where(
                    RemediationAttemptDB.anomaly_event_id == db_event.anomaly_id
                )
                result = await session.execute(stmt)
                remediation_attempts = result.scalars().all()

                # Convert remediation attempts to Pydantic models
                remediation_attempt_models = []
                for attempt in remediation_attempts:
                    remediation_attempt_models.append(
                        RemediationAttempt(
                            command=attempt.command,
                            parameters=attempt.parameters,
                            executor=attempt.executor,
                            timestamp=attempt.timestamp,
                            success=attempt.success,
                            result=attempt.result,
                            error=attempt.error,
                            is_proactive=attempt.is_proactive,
                        )
                    )

                # Convert DB model to Pydantic model
                event = AnomalyEvent(
                    anomaly_id=db_event.anomaly_id,
                    status=AnomalyStatus(db_event.status),
                    detection_timestamp=db_event.detection_timestamp,
                    entity_id=db_event.entity_id,
                    entity_type=db_event.entity_type,
                    metric_snapshot=db_event.metric_snapshot,
                    anomaly_score=db_event.anomaly_score,
                    namespace=db_event.namespace,
                    suggested_remediation_commands=db_event.suggested_remediation_commands,
                    remediation_attempts=remediation_attempt_models,
                    resolution_time=db_event.resolution_time,
                    ai_analysis=db_event.ai_analysis,
                    notes=db_event.notes,
                    verification_time=db_event.verification_time,
                    prediction_data=db_event.prediction_data,
                    is_proactive=db_event.is_proactive,
                )

                return event

        except Exception as e:
            logger.error(f"Error finding active event in SQLite: {e}")
            raise

    async def list_events(
        self,
        status: Optional[AnomalyStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
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
            async for session in get_async_db():
                # Start building the query
                query = select(AnomalyEventDB)

                # Add filters
                if status:
                    query = query.where(AnomalyEventDB.status == status.value)

                if start_time:
                    query = query.where(
                        AnomalyEventDB.detection_timestamp >= start_time
                    )

                if end_time:
                    query = query.where(AnomalyEventDB.detection_timestamp <= end_time)

                # Sort by detection timestamp (newest first)
                query = query.order_by(AnomalyEventDB.detection_timestamp.desc())

                # Limit results
                query = query.limit(limit)

                # Execute the query
                result = await session.execute(query)
                db_events = result.scalars().all()

                # Convert to Pydantic models
                events = []
                for db_event in db_events:
                    # Load remediation attempts for this event
                    stmt = select(RemediationAttemptDB).where(
                        RemediationAttemptDB.anomaly_event_id == db_event.anomaly_id
                    )
                    result = await session.execute(stmt)
                    remediation_attempts = result.scalars().all()

                    # Convert remediation attempts to Pydantic models
                    remediation_attempt_models = []
                    for attempt in remediation_attempts:
                        remediation_attempt_models.append(
                            RemediationAttempt(
                                command=attempt.command,
                                parameters=attempt.parameters,
                                executor=attempt.executor,
                                timestamp=attempt.timestamp,
                                success=attempt.success,
                                result=attempt.result,
                                error=attempt.error,
                                is_proactive=attempt.is_proactive,
                            )
                        )

                    # Convert DB model to Pydantic model
                    event = AnomalyEvent(
                        anomaly_id=db_event.anomaly_id,
                        status=AnomalyStatus(db_event.status),
                        detection_timestamp=db_event.detection_timestamp,
                        entity_id=db_event.entity_id,
                        entity_type=db_event.entity_type,
                        metric_snapshot=db_event.metric_snapshot,
                        anomaly_score=db_event.anomaly_score,
                        namespace=db_event.namespace,
                        suggested_remediation_commands=db_event.suggested_remediation_commands,
                        remediation_attempts=remediation_attempt_models,
                        resolution_time=db_event.resolution_time,
                        ai_analysis=db_event.ai_analysis,
                        notes=db_event.notes,
                        verification_time=db_event.verification_time,
                        prediction_data=db_event.prediction_data,
                        is_proactive=db_event.is_proactive,
                    )

                    events.append(event)

                return events

        except Exception as e:
            logger.error(f"Error listing anomaly events from SQLite: {e}")
            raise

    async def add_remediation_attempt(
        self, anomaly_id: str, attempt: RemediationAttempt
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
            async for session in get_async_db():
                # Check if event exists
                stmt = select(AnomalyEventDB).where(
                    AnomalyEventDB.anomaly_id == anomaly_id
                )
                result = await session.execute(stmt)
                db_event = result.scalars().first()

                if not db_event:
                    logger.warning(f"Anomaly event not found in SQLite: {anomaly_id}")
                    return None

                # Calculate verification time
                verification_time = datetime.utcnow() + timedelta(
                    seconds=settings.VERIFICATION_DELAY_SECONDS
                )

                # Update the event status and verification time
                db_event.status = AnomalyStatus.VERIFICATION_PENDING.value
                db_event.verification_time = verification_time

                # Create new remediation attempt
                db_attempt = RemediationAttemptDB(
                    anomaly_event_id=anomaly_id,
                    command=attempt.command,
                    parameters=attempt.parameters,
                    executor=attempt.executor,
                    timestamp=attempt.timestamp,
                    success=attempt.success,
                    result=attempt.result,
                    error=attempt.error,
                    is_proactive=attempt.is_proactive,
                )

                # Add to session and commit
                session.add(db_attempt)
                await session.commit()

                # Refresh the event object
                await session.refresh(db_event)

                # Load all remediation attempts for this event
                stmt = select(RemediationAttemptDB).where(
                    RemediationAttemptDB.anomaly_event_id == anomaly_id
                )
                result = await session.execute(stmt)
                remediation_attempts = result.scalars().all()

                # Convert remediation attempts to Pydantic models
                remediation_attempt_models = []
                for db_attempt in remediation_attempts:
                    remediation_attempt_models.append(
                        RemediationAttempt(
                            command=db_attempt.command,
                            parameters=db_attempt.parameters,
                            executor=db_attempt.executor,
                            timestamp=db_attempt.timestamp,
                            success=db_attempt.success,
                            result=db_attempt.result,
                            error=db_attempt.error,
                            is_proactive=db_attempt.is_proactive,
                        )
                    )

                # Convert DB model to Pydantic model for return
                event = AnomalyEvent(
                    anomaly_id=db_event.anomaly_id,
                    status=AnomalyStatus(db_event.status),
                    detection_timestamp=db_event.detection_timestamp,
                    entity_id=db_event.entity_id,
                    entity_type=db_event.entity_type,
                    metric_snapshot=db_event.metric_snapshot,
                    anomaly_score=db_event.anomaly_score,
                    namespace=db_event.namespace,
                    suggested_remediation_commands=db_event.suggested_remediation_commands,
                    remediation_attempts=remediation_attempt_models,
                    resolution_time=db_event.resolution_time,
                    ai_analysis=db_event.ai_analysis,
                    notes=db_event.notes,
                    verification_time=db_event.verification_time,
                    prediction_data=db_event.prediction_data,
                    is_proactive=db_event.is_proactive,
                )

                logger.info(
                    f"Added remediation attempt to anomaly event in SQLite: {anomaly_id}. Verification scheduled at {verification_time}"
                )
                return event

        except Exception as e:
            logger.error(f"Error adding remediation attempt in SQLite: {e}")
            raise

    async def handle_anomaly_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Handle an anomaly event and execute remediation if needed"""
        try:
            # Extract event details
            anomaly_type = event.get("anomaly_type")
            severity = event.get("severity")
            details = event.get("details", {})

            # Get remediation strategy
            strategy = self.get_remediation_strategy(anomaly_type, severity)

            if strategy == "auto":
                # Execute remediation command
                remediation_command = self.get_remediation_command(
                    anomaly_type, details
                )
                if remediation_command:
                    try:
                        # Handle both string and dictionary commands
                        if isinstance(remediation_command, dict):
                            operation = remediation_command.get("command")
                            params = remediation_command.get("params", {})
                        else:
                            operation = remediation_command
                            params = details

                        result = await self.k8s_executor.execute_validated_command(
                            operation, params
                        )
                        return {
                            "status": "success",
                            "message": "Remediation executed successfully",
                            "result": result,
                        }
                    except Exception as e:
                        return {
                            "status": "error",
                            "message": f"Failed to execute remediation: {str(e)}",
                        }
                else:
                    return {
                        "status": "warning",
                        "message": f"No remediation command found for anomaly type: {anomaly_type}",
                    }
            else:
                return {
                    "status": "info",
                    "message": f"Remediation strategy is set to {strategy}, skipping automatic remediation",
                }

        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to handle anomaly event: {str(e)}",
            }
