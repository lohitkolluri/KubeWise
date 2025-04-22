from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from app.services.gemini_service import RemediationAction


class AnomalyStatus(str, Enum):
    """Enum for anomaly status."""

    DETECTED = "Detected"
    FAILURE_DETECTED = "FailureDetected"  # New status for failures
    REMEDIATION_SUGGESTED = "RemediationSuggested"
    REMEDIATION_ATTEMPTED = "RemediationAttempted"
    VERIFICATION_PENDING = "VerificationPending"
    REMEDIATION_FAILED = "RemediationFailed"
    RESOLVED = "Resolved"
    CRITICAL_FAILURE = "CriticalFailure"  # New status for critical failures
    PREDICTED_FAILURE = "PredictedFailure"  # New status for predicted failures
    POD_STATE_ISSUE = "PodStateIssue"  # New status for pod-specific state issues (CrashLoopBackOff, ImagePullBackOff, etc.)
    THRESHOLD_BREACH = "ThresholdBreach"  # New status for direct threshold breaches


class RemediationAttempt(BaseModel):
    """Model for tracking remediation attempts."""

    command: str = Field(..., description="Command executed for remediation")
    parameters: Dict[str, Any] = Field(
        ..., description="Parameters passed to the command"
    )
    executor: str = Field(..., description="Who executed the command (AUTO/MANUAL)")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="When remediation was attempted"
    )
    success: bool = Field(
        default=False, description="Whether the remediation attempt was successful"
    )
    result: Optional[str] = Field(None, description="Result of the remediation attempt")
    error: Optional[str] = Field(
        None, description="Error message if the attempt failed"
    )
    is_proactive: bool = Field(
        default=False,
        description="Whether this was a proactive remediation for a predicted failure",
    )


class PredictedFailure(BaseModel):
    """Model for tracking predicted failures."""

    metric: str = Field(..., description="Name of the metric predicted to fail")
    threshold: float = Field(..., description="Threshold value that will be crossed")
    minutes_until_failure: int = Field(
        ..., description="Estimated minutes until threshold crossing"
    )
    confidence: float = Field(
        ..., description="Confidence score of the prediction (0-1)"
    )


class ForecastData(BaseModel):
    """Model for metric forecasting data."""

    forecasted_values: Dict[str, List[float]] = Field(
        ..., description="Forecasted values for each metric"
    )
    threshold_crossings: Dict[str, Dict[str, Any]] = Field(
        ..., description="Threshold crossing information"
    )
    forecast_confidence: float = Field(
        ..., description="Overall confidence in forecast (0-1)"
    )
    risk_assessment: str = Field(
        ..., description="Overall risk assessment (low, medium, high, critical)"
    )


class PredictionData(BaseModel):
    """Model for prediction and forecasting data."""

    predicted_failures: List[PredictedFailure] = Field(
        default=[], description="List of predicted failures"
    )
    forecast: Optional[ForecastData] = Field(
        None, description="Forecast data if available"
    )
    recommendation: str = Field(
        default="monitor",
        description="Recommendation based on prediction (monitor, proactive_action, immediate_action)",
    )


class AnomalyEvent(BaseModel):
    """Model for tracking anomaly events."""

    anomaly_id: str = Field(..., description="Unique identifier for the anomaly")
    status: AnomalyStatus = Field(..., description="Current status of the anomaly")
    detection_timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="When the anomaly was detected"
    )
    entity_id: str = Field(
        ...,
        description="ID of the entity (pod, node, etc.) where the anomaly was detected",
    )
    entity_type: str = Field(
        ..., description="Type of entity (pod, node, deployment, etc.)"
    )
    metric_snapshot: List[Dict[str, Any]] = Field(
        ..., description="Metrics snapshot window at the time of detection"
    )
    anomaly_score: float = Field(..., description="Anomaly detection score")
    namespace: Optional[str] = Field(None, description="Namespace of affected resource")
    suggested_remediation_commands: Optional[List[RemediationAction]] = Field(
        None, description="Suggested remediation actions as structured commands"
    )
    remediation_attempts: Optional[List[RemediationAttempt]] = Field(
        default=[], description="Details of remediation attempts"
    )
    resolution_time: Optional[datetime] = Field(
        None, description="When the anomaly was resolved"
    )
    ai_analysis: Optional[Dict[str, Any]] = Field(
        None, description="AI analysis of the anomaly"
    )
    notes: Optional[str] = Field(None, description="Additional notes about the anomaly")
    verification_time: Optional[datetime] = Field(
        None, description="When verification was performed"
    )
    prediction_data: Optional[Dict[str, Any]] = Field(
        None, description="Prediction and forecasting data for the entity"
    )
    is_proactive: bool = Field(
        default=False,
        description="Whether this event is for a predicted future failure",
    )


class AnomalyEventUpdate(BaseModel):
    """Model for updating anomaly events."""

    status: Optional[AnomalyStatus] = None
    suggested_remediation_commands: Optional[List[RemediationAction]] = None
    remediation_attempts: Optional[List[RemediationAttempt]] = None
    resolution_time: Optional[datetime] = None
    ai_analysis: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    verification_time: Optional[datetime] = None
    prediction_data: Optional[Dict[str, Any]] = None
    is_proactive: Optional[bool] = None
