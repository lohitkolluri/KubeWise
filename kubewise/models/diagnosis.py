"""
KubeWise diagnosis models.

This module provides Pydantic models for the diagnosis system.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from kubewise.models.base import BaseKubeWiseModel, PyObjectId, generate_objectid


class DiagnosisStatus(str, Enum):
    """Status of a diagnostic process."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    DIAGNOSING = "diagnosing"
    VALIDATING = "validating"
    ANALYZED = "analyzed"
    COMPLETE = "complete"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


class DiagnosticTool(str, Enum):
    """Type of diagnostic tools available."""

    POD_LOGS = "pod_logs"
    POD_DESCRIBE = "pod_describe"
    NODE_DESCRIBE = "node_describe"
    DEPLOYMENT_DESCRIBE = "deployment_describe"
    STATEFULSET_DESCRIBE = "statefulset_describe"
    RESOURCE_USAGE = "resource_usage"
    EVENT_HISTORY = "event_history"
    NODE_CONDITIONS = "node_conditions"
    PVC_STATUS = "pvc_status"


class DiagnosticPlanStatus(str, Enum):
    """Status of a diagnostic plan."""

    CREATED = "created"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"


class DiagnosticResult(BaseKubeWiseModel):
    """Result of a diagnostic tool execution."""

    id: PyObjectId = Field(default_factory=generate_objectid, alias="_id")
    plan_id: PyObjectId
    diagnosis_id: PyObjectId
    anomaly_id: PyObjectId
    tool: DiagnosticTool
    parameters: Dict[str, Any] = Field(default_factory=dict)
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_seconds: float = 0.0
    result_data: Dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    success: bool = False
    error_message: str = ""


class DiagnosticFinding(BaseKubeWiseModel):
    """A finding from diagnostic results analysis."""

    id: PyObjectId = Field(default_factory=generate_objectid, alias="_id")
    diagnosis_id: PyObjectId
    anomaly_id: PyObjectId
    finding_type: str
    description: str
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0
    severity: str = "medium"
    related_entities: List[Dict[str, str]] = Field(default_factory=list)
    source_result_ids: List[PyObjectId] = Field(default_factory=list)
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    is_root_cause: bool = False
    remediation_hints: List[str] = Field(default_factory=list)
    summary: str = ""
    details: str = ""
    source: str = "analysis"
    entity_id: str = ""
    entity_type: str = ""


class DiagnosticPlan(BaseKubeWiseModel):
    """A plan for diagnosing an anomaly."""

    id: PyObjectId = Field(default_factory=generate_objectid, alias="_id")
    diagnosis_id: PyObjectId
    anomaly_id: PyObjectId
    entity_id: str
    entity_type: str
    namespace: str = "default"
    diagnostic_tools: List[Dict[str, Any]] = Field(default_factory=list)
    status: DiagnosticPlanStatus = DiagnosticPlanStatus.CREATED
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    priority: int = 1
    reason: str = ""
    rationale: str = ""
    error_message: str = ""


class DiagnosisEntry(BaseKubeWiseModel):
    """Entry for tracking a diagnosis process."""

    id: Optional[PyObjectId] = Field(default_factory=generate_objectid, alias="_id")
    anomaly_id: PyObjectId
    entity_id: str
    entity_type: str
    namespace: str = "default"
    status: DiagnosisStatus = DiagnosisStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Diagnostic outcomes
    preliminary_diagnosis: str = ""
    validated: bool = False
    root_cause: str = ""
    root_cause_confidence: float = 0.0
    remediation_plan_id: Optional[PyObjectId] = None
    preliminary_confidence: float = 0.0

    # Validation results
    diagnosis_summary: str = ""
    diagnosis_details: str = ""
    validation_method: str = ""

    # Related findings
    findings: List[PyObjectId] = Field(default_factory=list)
    primary_finding_id: Optional[PyObjectId] = None

    # Error tracking
    error_message: str = ""
    retry_count: int = 0
    diagnosis_reasoning: str = ""
    diagnostic_plan_id: Optional[PyObjectId] = None
