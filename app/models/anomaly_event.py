from datetime import datetime
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from enum import Enum

class AnomalyStatus(str, Enum):
    """Enum for anomaly status."""
    DETECTED = "Detected"
    REMEDIATION_SUGGESTED = "RemediationSuggested"
    REMEDIATION_ATTEMPTED = "RemediationAttempted"
    VERIFICATION_PENDING = "VerificationPending"
    REMEDIATION_FAILED = "RemediationFailed"
    RESOLVED = "Resolved"

class RemediationAttempt(BaseModel):
    """Model for tracking remediation attempts."""
    command: str = Field(..., description="Command executed for remediation")
    parameters: Dict[str, Any] = Field(..., description="Parameters passed to the command")
    executor: str = Field(..., description="Who executed the command (AUTO/MANUAL/USER)")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="When remediation was attempted")
    success: bool = Field(default=False, description="Whether the remediation attempt was successful")
    result: Optional[str] = Field(None, description="Result of the remediation attempt")
    error: Optional[str] = Field(None, description="Error message if the attempt failed")

class AnomalyEvent(BaseModel):
    """Model for tracking anomaly events."""
    anomaly_id: str = Field(..., description="Unique identifier for the anomaly")
    status: AnomalyStatus = Field(..., description="Current status of the anomaly")
    detection_timestamp: datetime = Field(default_factory=datetime.utcnow, description="When the anomaly was detected")
    entity_id: str = Field(..., description="ID of the entity (pod, node, etc.) where the anomaly was detected")
    entity_type: str = Field(..., description="Type of entity (pod, node, deployment, etc.)")
    metric_snapshot: List[Dict[str, Any]] = Field(..., description="Metrics snapshot window at the time of detection")
    anomaly_score: float = Field(..., description="Anomaly detection score")
    namespace: Optional[str] = Field(None, description="Namespace of affected resource")
    suggested_remediation_commands: Optional[List[str]] = Field(None, description="Suggested remediation steps")
    remediation_attempts: Optional[List[RemediationAttempt]] = Field(default=[], description="Details of remediation attempts")
    resolution_time: Optional[datetime] = Field(None, description="When the anomaly was resolved")
    ai_analysis: Optional[Dict[str, Any]] = Field(None, description="AI analysis of the anomaly")
    notes: Optional[str] = Field(None, description="Additional notes about the anomaly")
    verification_time: Optional[datetime] = Field(None, description="When verification was performed")

class AnomalyEventUpdate(BaseModel):
    """Model for updating anomaly events."""
    status: Optional[AnomalyStatus] = None
    suggested_remediation_commands: Optional[List[str]] = None
    remediation_attempts: Optional[List[RemediationAttempt]] = None
    resolution_time: Optional[datetime] = None
    ai_analysis: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    verification_time: Optional[datetime] = None
