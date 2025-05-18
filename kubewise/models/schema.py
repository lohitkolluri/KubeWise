"""
KubeWise data models for storage and processing.
"""

import datetime
from enum import Enum
from typing import Dict, List, Optional, Union, Any

from bson import ObjectId
from pydantic import ConfigDict, Field, field_validator

from kubewise.models.base import BaseKubeWiseModel, PyObjectId, generate_objectid

# --- Data Models for Storage & Processing ---


class ActionType(str, Enum):
    """Type of remediation action"""

    RESTART_POD = "restart_pod"
    SCALE_DEPLOYMENT = "scale_deployment"
    EVICT_POD = "evict_pod"
    DRAIN_NODE = "drain_node"
    UNCORDON_NODE = "uncordon_node"
    CORDON_NODE = "cordon_node"
    RESTART_DEPLOYMENT = "restart_deployment"
    DELETE_POD = "delete_pod"
    MANUAL_INTERVENTION = "manual_intervention"
    SCALE_STATEFULSET = "scale_statefulset"
    VERTICAL_SCALE_DEPLOYMENT = "vertical_scale_deployment"
    VERTICAL_SCALE_STATEFULSET = "vertical_scale_statefulset"
    RESTART_DAEMONSET = "restart_daemonset"
    RESTART_STATEFULSET = "restart_statefulset"
    UPDATE_RESOURCE_LIMITS = "update_resource_limits"
    UPDATE_CONFIGMAP = "update_configmap"
    UPDATE_SECRET = "update_secret"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    FIX_SERVICE_SELECTORS = "fix_service_selectors"
    FORCE_DELETE_POD = "force_delete_pod"
    UPDATE_CONTAINER_IMAGE = "update_container_image"
    FIX_DNS_CONFIG = "fix_dns_config"
    RESTART_CRONJOB = "restart_cronjob"
    RESTART_JOB = "restart_job"
    TAINT_NODE = "taint_node"


class MetricPoint(BaseKubeWiseModel):
    """Represents a single Prometheus metric data point."""

    metric_name: str = Field(..., description="Name of the Prometheus metric.")
    labels: Dict[str, str] = Field(
        default_factory=dict, description="Labels associated with the metric."
    )
    value: float = Field(..., description="The numeric value of the metric.")
    timestamp: datetime.datetime = Field(
        ..., description="Timestamp of the metric data point."
    )


class KubernetesEvent(BaseKubeWiseModel):
    """Represents a relevant Kubernetes event."""

    event_id: str = Field(
        ..., description="Unique ID of the event (e.g., metadata.uid)."
    )
    reason: str = Field(
        "Unknown", description="Reason for the event (e.g., 'Failed', 'Evicted')."
    )
    message: str = Field("", description="Detailed message associated with the event.")
    type: str = Field(..., description="Event type (e.g., 'Normal', 'Warning').")
    involved_object_kind: str = Field(
        ...,
        alias="involvedObjectKind",
        description="Kind of the involved object (e.g., 'Pod', 'Node').",
    )
    involved_object_name: str = Field(
        ..., alias="involvedObjectName", description="Name of the involved object."
    )
    involved_object_namespace: Optional[str] = Field(
        None,
        alias="involvedObjectNamespace",
        description="Namespace of the involved object.",
    )
    event_time: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow,
        alias="eventTime",
        description="Time the event occurred.",
    )
    # Extended event context
    grouped_events: List[Dict] = Field(
        default_factory=list,
        description="Related events that were grouped with this one.",
    )
    group_size: int = Field(
        default=1, description="Number of events in this group (1 if not grouped)."
    )
    first_timestamp: Optional[datetime.datetime] = Field(
        None, description="Time of the first event in this group/series."
    )
    last_timestamp: Optional[datetime.datetime] = Field(
        None, description="Time of the last event in this group/series."
    )
    count: int = Field(
        default=1, description="Number of times this event has occurred."
    )

    # Denormalized fields for easier filtering
    node_name: str = Field(
        "", description="Node name, if this event is related to a node."
    )
    pod_name: str = Field(
        "", description="Pod name, if this event is related to a pod."
    )
    namespace: str = Field("default", description="Namespace of the involved object.")
    severity: str = Field(
        "Info",
        description="Severity level of the event (e.g., 'Critical', 'Warning', 'Info').",
    )
    component: str = Field("", description="Component that generated the event.")
    host: str = Field("", description="Host where the event occurred.")

    # Action tracking
    actions_taken: List[str] = Field(
        default_factory=list,
        description="IDs of remediation actions that were taken in response to this event.",
    )

    class Config:
        populate_by_name = True


class RemediationAction(BaseKubeWiseModel):
    """An action to be taken as part of a remediation plan."""

    id: PyObjectId = Field(default_factory=generate_objectid, alias="_id")
    action_type: ActionType = Field(
        ...,
        description="Type of action to perform (e.g. restart_pod, scale_deployment)",
    )
    parameters: Dict[str, Union[str, int, float, bool, List[str], Dict[str, Any]]] = Field(
        default_factory=dict,
        description="Parameters required for the action (e.g. name, namespace)",
    )
    description: str = Field("", description="Human-readable description of the action")
    justification: str = Field(
        "", description="Explanation of why this action should help"
    )
    impact_assessment: str = Field(
        "", description="Assessment of potential impact of the action"
    )
    estimated_recovery_time: str = Field(
        "", description="Estimated time for action to take effect"
    )
    priority: int = Field(default=1, description="Execution priority (1 highest)")

    # Parent entity information
    entity_type: str = Field(
        "", description="Type of entity this action applies to (pod, deployment, node)"
    )
    entity_id: str = Field("", description="ID of the entity this action applies to")

    # Execution tracking
    executed: bool = Field(
        default=False, description="Whether the action has been executed"
    )
    execution_timestamp: Optional[datetime.datetime] = Field(
        None, description="When this action was executed"
    )
    execution_status: str = Field(
        "pending", description="Success/failure status of execution"
    )
    execution_message: str = Field(
        "", description="Message returned from action execution"
    )
    execution_duration: float = Field(
        default=0.0, description="Duration of action execution in seconds"
    )
    retry_count: int = Field(
        default=0, description="Number of retry attempts for this action"
    )

    # Report generation
    resolved_anomaly: bool = Field(
        default=False, description="Whether this action resolved the anomaly"
    )

    @field_validator("priority")
    def validate_priority(cls, v):
        if v < 1:
            return 1
        return v


class ExecutedActionRecord(BaseKubeWiseModel):
    """Record of a remediation action that was executed."""

    id: PyObjectId = Field(default_factory=generate_objectid, alias="_id")
    anomaly_id: PyObjectId = Field(
        ..., description="ID of the anomaly that triggered this action"
    )
    plan_id: PyObjectId = Field(
        ..., description="ID of the remediation plan this action belongs to"
    )
    action_id: PyObjectId = Field(
        ..., description="ID of the original action in the remediation plan"
    )
    action_type: ActionType = Field(..., description="Type of action performed")
    parameters: Dict[str, Union[str, int, float, bool, List[str], Dict[str, Any]]] = Field(
        default_factory=dict, description="Parameters used for the action"
    )
    entity_type: str = Field(..., description="Type of entity this action applied to")
    entity_id: str = Field(..., description="ID of the entity this action applied to")
    namespace: str = Field(
        "default", description="Namespace of the entity if applicable"
    )
    timestamp: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow,
        description="When the action was executed",
    )
    success: bool = Field(..., description="Whether the action succeeded")
    message: str = Field("", description="Message returned from the action execution")
    duration_seconds: float = Field(
        ..., description="How long the action took to execute"
    )
    score: float = Field(
        default=0.0, description="Anomaly score that triggered this action"
    )
    source_type: str = Field(
        default="unknown", description="Source of the anomaly (e.g., 'metric', 'event')"
    )
    retry_attempt: int = Field(
        default=0, description="Which retry attempt this execution represents"
    )
    error_details: Optional[Dict[str, Any]] = Field(
        default=None, description="Detailed error information if action failed"
    )
    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow,
        description="When this record was created",
    )

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
    )


class RemediationPlan(BaseKubeWiseModel):
    """A plan of actions to remediate an issue."""

    id: PyObjectId = Field(default_factory=generate_objectid, alias="_id")
    anomaly_id: PyObjectId = Field(
        ..., description="ID of the anomaly that triggered this plan"
    )
    source_type: str = Field(
        default="unknown",
        description="Source of the remediation plan (e.g., 'ai_generated', 'static')",
    )
    plan_name: str = Field(..., description="Name of the remediation plan")
    description: str = Field(
        ..., description="Detailed description of the issue and remediation approach"
    )
    reasoning: str = Field(
        "", description="Reasoning behind the chosen remediation actions"
    )
    actions: List[RemediationAction] = Field(
        ..., description="List of actions to execute"
    )
    ordered: bool = Field(
        default=True, description="Whether actions must be executed in order"
    )
    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow,
        description="When this plan was created",
    )
    updated_at: Optional[datetime.datetime] = Field(
        None, description="When this plan was last updated"
    )
    completed: bool = Field(
        default=False, description="Whether this plan has been fully executed"
    )
    successful: Optional[bool] = Field(
        None, description="Whether the plan execution was successful"
    )
    completion_time: Optional[datetime.datetime] = Field(
        None, description="When this plan completed execution"
    )

    # Integration metadata
    ml_model_id: str = Field("", description="ID of ML model that generated this plan")
    trigger_source: str = Field(
        default="automatic",
        description="What triggered this plan (e.g. automatic, manual)",
    )

    # Target information
    target_entity_type: str = Field(
        "", description="Primary entity type this plan addresses"
    )
    target_entity_id: str = Field(
        "", description="Primary entity ID this plan addresses"
    )
    execution_attempts: int = Field(
        default=0, description="Number of times execution was attempted"
    )

    # Execution metadata
    exec_result_url: str = Field(
        "", description="URL to the execution result dashboard or details"
    )
    requires_approval: bool = Field(
        default=False, description="Whether this plan requires manual approval"
    )
    approved_by: str = Field(
        "", description="User who approved this plan if approval was required"
    )
    approval_time: Optional[datetime.datetime] = Field(
        None, description="When this plan was approved if required"
    )

    # Risk assessment and dry run
    dry_run_results: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Results from dry run simulation"
    )
    safety_score: float = Field(
        default=1.0, description="Score indicating safety of this plan (0.0-1.0)"
    )
    risk_assessment: str = Field(
        "", description="Assessment of risks associated with this plan"
    )

    # Additional context
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context information about the plan",
    )

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
    )


class AnomalyRecord(BaseKubeWiseModel):
    """
    Record of a detected anomaly.

    Stores all relevant information about an anomaly detection event
    including scores, thresholds, and source data.
    """

    id: PyObjectId = Field(default_factory=generate_objectid, alias="_id")
    entity_id: str
    entity_type: str = Field(
        "", description="Type of entity (e.g., 'Pod', 'Node', 'Deployment')"
    )
    namespace: str = Field("default", description="Kubernetes namespace of the entity")
    name: str = Field("", description="Name of the affected entity")
    timestamp: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow,
        description="When this anomaly was detected",
    )
    metric_name: str = Field(
        "", description="Name of the anomalous metric (if applicable)"
    )
    metric_value: float = Field(
        0.0, description="Value of the anomalous metric (if applicable)"
    )
    formatted_metric_value: str = Field(
        "", description="Human-readable formatted metric value"
    )
    anomaly_score: float = Field(0.0, description="Legacy anomaly score")
    hst_score: float = Field(0.0, description="Half-space trees anomaly score")
    river_score: float = Field(0.0, description="River ML anomaly score")
    combined_score: float = Field(0.0, description="Weighted combined anomaly score")
    threshold: float
    features_snapshot: Dict[str, float] = Field(
        default_factory=dict, description="Snapshot of feature values at detection time"
    )
    data_source: str = Field(
        "metric", description="Source of the anomaly data (e.g., 'metric', 'event')"
    )

    # Event-related data
    event_reason: str = Field(
        "", description="Reason for the event (if event-based anomaly)"
    )
    event_message: str = Field(
        "", description="Message from the event (if event-based anomaly)"
    )

    # Direct failure detection
    is_direct_failure: bool = Field(
        default=False, description="Whether this is a direct failure indicator"
    )
    failure_reason: str = Field(
        "AnomalousMetric", description="Reason code for the failure/anomaly"
    )
    failure_message: str = Field(
        "", description="Human-readable description of the failure"
    )

    # Remediation tracking
    remediated: bool = Field(
        default=False, description="Whether remediation has been attempted"
    )
    remediation_plan_id: Optional[str] = Field(
        None, description="ID of the remediation plan"
    )
    remediation_status: str = Field(
        "pending",
        description="Status of remediation (pending, executing, completed, failed)",
    )
    remediation_error: str = Field(
        "", description="Error message if remediation failed"
    )
    remediation_timestamp: Optional[datetime.datetime] = Field(
        None, description="When remediation was attempted"
    )
    requires_manual_review: bool = Field(
        default=False,
        description="Flag indicating if this anomaly requires manual review",
    )

    # Recurring issue tracking
    is_recurring: bool = Field(
        default=False, description="Whether this is a recurring issue"
    )
    similar_anomaly_ids: List[str] = Field(
        default_factory=list, description="IDs of similar anomalies detected previously"
    )
    remediation_attempt_count: int = Field(
        default=0, description="Number of remediation attempts"
    )
    last_remediation_time: Optional[datetime.datetime] = Field(
        None, description="Timestamp of the last remediation attempt"
    )
    escalation_level: int = Field(default=0, description="Current escalation level")
    recovery_history: List[Dict[str, Any]] = Field(
        default_factory=list, description="History of remediation attempts"
    )
    resource_annotations: Dict[str, str] = Field(
        default_factory=dict,
        description="Annotations applied to the resource for tracking",
    )
    cooldown_until: Optional[datetime.datetime] = Field(
        None, description="Timestamp until which remediation is in cooldown"
    )
    max_escalation_level_reached: bool = Field(
        default=False, description="Whether maximum escalation level has been reached"
    )

    # Additional metadata
    labels: Dict[str, str] = Field(
        default_factory=dict, description="Labels from the affected resource"
    )
    detection_method: str = Field(
        "model",
        description="Method used to detect this anomaly (e.g., 'model', 'direct', 'rule')",
    )
    creator: str = Field(
        "system", description="Who or what created this anomaly record"
    )

    @field_validator("remediation_plan_id", mode="before")
    def validate_remediation_plan_id(cls, v):
        """Convert ObjectId to string if needed."""
        if isinstance(v, ObjectId):
            return str(v)
        return v
        
    @field_validator("similar_anomaly_ids", mode="before")
    def validate_similar_anomaly_ids(cls, v):
        """Convert any ObjectId elements to strings."""
        if isinstance(v, list):
            return [str(item) if isinstance(item, ObjectId) else item for item in v]
        return v

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
        json_schema_extra={
            "example": {
                "entity_id": "default/nginx-deployment-7fb96c846b-gdtkv",
                "entity_type": "Pod",
                "namespace": "default",
                "name": "nginx-deployment-7fb96c846b-gdtkv",
                "timestamp": "2023-02-10T15:30:00Z",
                "metric_name": "cpu_utilization_pct",
                "metric_value": 95.2,
                "formatted_metric_value": "95.2%",
                "anomaly_score": 0.87,
                "hst_score": 0.85,
                "river_score": 0.9,
                "combined_score": 0.87,
                "threshold": 0.7,
                "features_snapshot": {"cpu_rate": 95.2, "memory_rate": 80.1},
                "data_source": "metric",
                "is_direct_failure": False,
                "remediated": False,
                "remediation_status": "pending",
                "is_recurring": False,
                "similar_anomaly_ids": [],
                "remediation_attempt_count": 0,
                "last_remediation_time": None,
                "escalation_level": 0,
                "recovery_history": [],
                "resource_annotations": {},
                "cooldown_until": None,
                "max_escalation_level_reached": False,
            }
        },
    )

    def safe_format(self, template: str) -> str:
        """
        Safely format a string template using fields from this object.
        Handles None values by converting them to empty strings.
        """
        try:
            return template.format(
                **{
                    k: (v if v is not None else "")
                    for k, v in self.model_dump().items()
                }
            )
        except Exception:
            return template  # Return the original template if formatting fails

    def extract_entity_info(self) -> None:
        """
        Extract namespace and name from entity_id if not already set.
        Should be called after entity_id is set but before storing.
        """
        if not self.name and "/" in self.entity_id:
            try:
                self.namespace, self.name = self.entity_id.split("/", 1)
            except ValueError:
                # If splitting fails, just use the entity_id as the name
                self.name = self.entity_id
