"""
KubeWise audit logging system.

This module defines the data models and core functionality for comprehensive 
audit logging of all detection, diagnosis, and remediation actions.
"""

import datetime
from enum import Enum
from typing import Dict, List, Optional, Any, Union

from bson import ObjectId
from pydantic import ConfigDict, Field

from kubewise.models.base import BaseKubeWiseModel, PyObjectId, generate_objectid


class AuditEventType(str, Enum):
    """Types of events that can be audited."""
    
    # Anomaly Detection Events
    ANOMALY_DETECTED = "anomaly_detected"
    ANOMALY_UPDATED = "anomaly_updated"
    ANOMALY_CLASSIFIED = "anomaly_classified"
    FALSE_POSITIVE_DETECTED = "false_positive_detected"
    THRESHOLD_ADJUSTED = "threshold_adjusted"
    
    # Diagnosis Events
    DIAGNOSIS_STARTED = "diagnosis_started"
    DIAGNOSIS_COMPLETED = "diagnosis_completed"
    DIAGNOSIS_FAILED = "diagnosis_failed"
    DIAGNOSIS_FINDING_ADDED = "diagnosis_finding_added"
    
    # Remediation Planning Events
    PLAN_GENERATED = "plan_generated"
    PLAN_SIMULATED = "plan_simulated"
    PLAN_APPROVED = "plan_approved"
    PLAN_REJECTED = "plan_rejected"
    PLAN_MODIFIED = "plan_modified"
    
    # Remediation Execution Events
    ACTION_EXECUTED = "action_executed"
    ACTION_SUCCEEDED = "action_succeeded"
    ACTION_FAILED = "action_failed"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    
    # Cross-Cluster Events
    CLUSTER_CONNECTED = "cluster_connected"
    CLUSTER_DISCONNECTED = "cluster_disconnected"
    CROSS_CLUSTER_CORRELATION = "cross_cluster_correlation"
    
    # System Events
    SYSTEM_STARTUP = "system_startup"
    SYSTEM_SHUTDOWN = "system_shutdown"
    CONFIG_CHANGED = "config_changed"
    COMPONENT_ERROR = "component_error"


class AuditEventSeverity(str, Enum):
    """Severity levels for audit events."""
    
    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditRecord(BaseKubeWiseModel):
    """
    Comprehensive audit record for system events.
    
    Stores detailed information about all significant events within KubeWise,
    enabling complete traceability and compliance reporting.
    """
    
    id: PyObjectId = Field(default_factory=generate_objectid, alias="_id")
    
    # Basic event information
    event_type: AuditEventType = Field(..., description="Type of the audit event")
    timestamp: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow, 
        description="When this event occurred"
    )
    severity: AuditEventSeverity = Field(
        AuditEventSeverity.INFO, 
        description="Severity level of the event"
    )
    
    # Context information
    user: str = Field(
        "system", 
        description="User or component that triggered the event"
    )
    cluster_id: str = Field(
        "default", 
        description="ID of the cluster where the event occurred"
    )
    namespace: str = Field(
        "", 
        description="Kubernetes namespace related to the event (if applicable)"
    )
    
    # Entity references
    entity_type: str = Field(
        "", 
        description="Type of the primary entity involved (e.g., 'Pod', 'Node')"
    )
    entity_id: str = Field(
        "", 
        description="ID of the primary entity involved"
    )
    related_entity_ids: List[str] = Field(
        default_factory=list,
        description="IDs of related entities"
    )
    
    # Event-specific details
    anomaly_id: Optional[str] = Field(
        None, 
        description="ID of the related anomaly (if applicable)"
    )
    diagnosis_id: Optional[str] = Field(
        None, 
        description="ID of the related diagnosis (if applicable)"
    )
    plan_id: Optional[str] = Field(
        None, 
        description="ID of the related remediation plan (if applicable)"
    )
    action_id: Optional[str] = Field(
        None, 
        description="ID of the related remediation action (if applicable)"
    )
    
    # Event description
    message: str = Field(
        ..., 
        description="Human-readable description of the event"
    )
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Detailed information specific to the event type"
    )
    
    # Impact assessment
    impact: str = Field(
        "", 
        description="Assessment of the event's impact on the system"
    )
    priority: int = Field(
        5, 
        description="Priority of the event (1-10, lower is higher priority)"
    )
    
    # Cross-cluster tracking
    is_cross_cluster: bool = Field(
        False,
        description="Whether this event spans multiple clusters"
    )
    related_cluster_ids: List[str] = Field(
        default_factory=list,
        description="IDs of related clusters for cross-cluster events"
    )
    
    # Simulation information
    is_simulation: bool = Field(
        False,
        description="Whether this event was part of a simulation"
    )
    simulation_id: Optional[str] = Field(
        None,
        description="ID of the simulation this event belongs to (if applicable)"
    )
    
    # Additional metadata
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for categorizing and filtering audit events"
    )
    
    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={datetime.datetime: lambda v: v.isoformat()},
        arbitrary_types_allowed=True,
    )


class AuditService:
    """
    Service for creating and managing audit records.
    
    Provides a centralized interface for creating audit records for all system events,
    with methods for different event types and automatic severity assignment.
    """
    
    def __init__(self, db=None):
        """Initialize the audit service with a database connection."""
        self.db = db
    
    async def create_audit_record(self, record: AuditRecord) -> str:
        """
        Create a new audit record in the database.
        
        Args:
            record: The AuditRecord to store
            
        Returns:
            The ID of the created record
        """
        if not self.db:
            # Log but don't fail if DB is not available
            return str(record.id)
            
        try:
            result = await self.db.audit_records.insert_one(record.model_dump(by_alias=True))
            return str(result.inserted_id)
        except Exception as e:
            # Log the error but don't raise (audit failures shouldn't break core functionality)
            print(f"Error creating audit record: {e}")
            return str(record.id)
    
    async def create_anomaly_detection_event(
        self,
        event_type: AuditEventType,
        anomaly_id: str,
        entity_type: str,
        entity_id: str,
        message: str,
        namespace: str = "",
        details: Dict[str, Any] = None,
        severity: AuditEventSeverity = AuditEventSeverity.INFO,
    ) -> str:
        """
        Create an audit record for an anomaly detection event.
        
        Args:
            event_type: Type of the anomaly detection event
            anomaly_id: ID of the detected anomaly
            entity_type: Type of entity with the anomaly
            entity_id: ID of the entity with the anomaly
            message: Human-readable description
            namespace: Kubernetes namespace
            details: Additional event details
            severity: Event severity level
            
        Returns:
            The ID of the created audit record
        """
        record = AuditRecord(
            event_type=event_type,
            severity=severity,
            entity_type=entity_type,
            entity_id=entity_id,
            namespace=namespace,
            anomaly_id=anomaly_id,
            message=message,
            details=details or {},
        )
        
        return await self.create_audit_record(record)
    
    async def create_diagnosis_event(
        self,
        event_type: AuditEventType,
        diagnosis_id: str,
        anomaly_id: str,
        entity_type: str,
        entity_id: str,
        message: str,
        namespace: str = "",
        details: Dict[str, Any] = None,
        severity: AuditEventSeverity = AuditEventSeverity.INFO,
    ) -> str:
        """
        Create an audit record for a diagnosis event.
        
        Args:
            event_type: Type of the diagnosis event
            diagnosis_id: ID of the diagnosis
            anomaly_id: ID of the related anomaly
            entity_type: Type of entity being diagnosed
            entity_id: ID of the entity being diagnosed
            message: Human-readable description
            namespace: Kubernetes namespace
            details: Additional event details
            severity: Event severity level
            
        Returns:
            The ID of the created audit record
        """
        record = AuditRecord(
            event_type=event_type,
            severity=severity,
            entity_type=entity_type,
            entity_id=entity_id,
            namespace=namespace,
            diagnosis_id=diagnosis_id,
            anomaly_id=anomaly_id,
            message=message,
            details=details or {},
        )
        
        return await self.create_audit_record(record)
    
    async def create_remediation_plan_event(
        self,
        event_type: AuditEventType,
        plan_id: str,
        diagnosis_id: Optional[str],
        anomaly_id: str,
        entity_type: str,
        entity_id: str,
        message: str,
        namespace: str = "",
        details: Dict[str, Any] = None, 
        severity: AuditEventSeverity = AuditEventSeverity.INFO,
        is_simulation: bool = False,
    ) -> str:
        """
        Create an audit record for a remediation planning event.
        
        Args:
            event_type: Type of the remediation plan event
            plan_id: ID of the remediation plan
            diagnosis_id: ID of the related diagnosis (if any)
            anomaly_id: ID of the related anomaly
            entity_type: Type of entity being remediated
            entity_id: ID of the entity being remediated
            message: Human-readable description
            namespace: Kubernetes namespace
            details: Additional event details
            severity: Event severity level
            is_simulation: Whether this is a simulation event
            
        Returns:
            The ID of the created audit record
        """
        record = AuditRecord(
            event_type=event_type,
            severity=severity,
            entity_type=entity_type,
            entity_id=entity_id,
            namespace=namespace,
            plan_id=plan_id, 
            diagnosis_id=diagnosis_id,
            anomaly_id=anomaly_id,
            message=message,
            details=details or {},
            is_simulation=is_simulation,
        )
        
        return await self.create_audit_record(record)
    
    async def create_remediation_action_event(
        self,
        event_type: AuditEventType,
        action_id: str,
        plan_id: str,
        anomaly_id: str,
        entity_type: str,
        entity_id: str,
        message: str,
        namespace: str = "",
        details: Dict[str, Any] = None,
        severity: AuditEventSeverity = AuditEventSeverity.INFO,
        is_simulation: bool = False,
    ) -> str:
        """
        Create an audit record for a remediation action event.
        
        Args:
            event_type: Type of the remediation action event
            action_id: ID of the remediation action
            plan_id: ID of the parent remediation plan
            anomaly_id: ID of the related anomaly
            entity_type: Type of entity being remediated
            entity_id: ID of the entity being remediated
            message: Human-readable description
            namespace: Kubernetes namespace
            details: Additional event details  
            severity: Event severity level
            is_simulation: Whether this is a simulation event
            
        Returns:
            The ID of the created audit record
        """
        record = AuditRecord(
            event_type=event_type,
            severity=severity,
            entity_type=entity_type,
            entity_id=entity_id,
            namespace=namespace,
            action_id=action_id,
            plan_id=plan_id,
            anomaly_id=anomaly_id,
            message=message,
            details=details or {},
            is_simulation=is_simulation,
        )
        
        return await self.create_audit_record(record)
    
    async def create_cross_cluster_event(
        self,
        event_type: AuditEventType,
        cluster_id: str,
        related_cluster_ids: List[str],
        message: str,
        entity_type: str = "",
        entity_id: str = "",
        details: Dict[str, Any] = None,
        severity: AuditEventSeverity = AuditEventSeverity.INFO,
    ) -> str:
        """
        Create an audit record for a cross-cluster event.
        
        Args:
            event_type: Type of the cross-cluster event
            cluster_id: ID of the primary cluster
            related_cluster_ids: IDs of related clusters
            message: Human-readable description
            entity_type: Type of entity (if applicable)
            entity_id: ID of the entity (if applicable) 
            details: Additional event details
            severity: Event severity level
            
        Returns:
            The ID of the created audit record
        """
        record = AuditRecord(
            event_type=event_type,
            severity=severity,
            cluster_id=cluster_id,
            related_cluster_ids=related_cluster_ids,
            is_cross_cluster=True,
            entity_type=entity_type,
            entity_id=entity_id,
            message=message,
            details=details or {},
        )
        
        return await self.create_audit_record(record)
    
    async def create_system_event(
        self,
        event_type: AuditEventType,
        message: str,
        details: Dict[str, Any] = None,
        severity: AuditEventSeverity = AuditEventSeverity.INFO,
    ) -> str:
        """
        Create an audit record for a system-level event.
        
        Args:
            event_type: Type of the system event
            message: Human-readable description
            details: Additional event details
            severity: Event severity level
            
        Returns:
            The ID of the created audit record
        """
        record = AuditRecord(
            event_type=event_type,
            severity=severity,
            message=message,
            details=details or {},
        )
        
        return await self.create_audit_record(record) 