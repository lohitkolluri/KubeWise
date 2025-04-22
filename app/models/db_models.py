import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class AnomalyStatusEnum(enum.Enum):
    DETECTED = "Detected"
    FAILURE_DETECTED = "FailureDetected"
    REMEDIATION_SUGGESTED = "RemediationSuggested"
    REMEDIATION_ATTEMPTED = "RemediationAttempted"
    VERIFICATION_PENDING = "VerificationPending"
    REMEDIATION_FAILED = "RemediationFailed"
    RESOLVED = "Resolved"
    CRITICAL_FAILURE = "CriticalFailure"
    PREDICTED_FAILURE = "PredictedFailure"


class AnomalyEventDB(Base):
    """SQLAlchemy model for anomaly events."""

    __tablename__ = "anomaly_events"

    id = Column(Integer, primary_key=True, index=True)
    anomaly_id = Column(String, unique=True, index=True)
    status = Column(String)
    detection_timestamp = Column(DateTime, default=datetime.utcnow)
    entity_id = Column(String, index=True)
    entity_type = Column(String, index=True)
    metric_snapshot = Column(JSON)
    anomaly_score = Column(Float)
    namespace = Column(String, nullable=True)
    suggested_remediation_commands = Column(JSON, nullable=True)
    resolution_time = Column(DateTime, nullable=True)
    ai_analysis = Column(JSON, nullable=True)
    notes = Column(String, nullable=True)
    verification_time = Column(DateTime, nullable=True)
    prediction_data = Column(JSON, nullable=True)
    is_proactive = Column(Boolean, default=False)

    # Relationship with remediation attempts
    remediation_attempts = relationship(
        "RemediationAttemptDB",
        back_populates="anomaly_event",
        cascade="all, delete-orphan",
    )


class RemediationAttemptDB(Base):
    """SQLAlchemy model for remediation attempts."""

    __tablename__ = "remediation_attempts"

    id = Column(Integer, primary_key=True, index=True)
    anomaly_event_id = Column(String, ForeignKey("anomaly_events.anomaly_id"))
    command = Column(String)
    parameters = Column(JSON)
    executor = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    success = Column(Boolean, default=False)
    result = Column(String, nullable=True)
    error = Column(String, nullable=True)
    is_proactive = Column(Boolean, default=False)

    # Relationship with anomaly event
    anomaly_event = relationship(
        "AnomalyEventDB", back_populates="remediation_attempts"
    )
