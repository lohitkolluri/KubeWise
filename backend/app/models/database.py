from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class IssueType(str, Enum):
    """Types of issues that can be detected."""
    ANOMALY = "ANOMALY"
    PREDICTION = "PREDICTION"


class IssueSeverity(str, Enum):
    """Severity levels for issues."""
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class IssueStatus(str, Enum):
    """Status of detected issues."""
    NEW = "NEW"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"
    REMEDIATION_ATTEMPTED = "REMEDIATION_ATTEMPTED"


class K8sObjectType(str, Enum):
    """Types of Kubernetes objects that can have issues."""
    NODE = "Node"
    POD = "Pod"
    DEPLOYMENT = "Deployment"
    SERVICE = "Service"
    PVC = "PersistentVolumeClaim"
    STATEFULSET = "StatefulSet"
    DAEMONSET = "DaemonSet"
    INGRESS = "Ingress"
    OTHER = "Other"


class IssueBase(BaseModel):
    """Base model for issues with common fields."""
    type: IssueType
    severity: IssueSeverity
    k8s_object_type: K8sObjectType
    k8s_object_name: str
    k8s_object_namespace: Optional[str] = None
    description: str
    remediation_suggestion: str
    remediation_command: Optional[str] = None
    ai_confidence_score: Optional[float] = None


class IssueCreate(IssueBase):
    """Model for creating a new issue."""
    pass


class IssueDB(IssueBase):
    """Model representing an issue in the database."""
    id: UUID
    created_at: datetime
    updated_at: datetime
    status: IssueStatus = IssueStatus.NEW
    last_detected_at: datetime

    class Config:
        orm_mode = True


class IssueUpdate(BaseModel):
    """Model for updating an issue."""
    status: Optional[IssueStatus] = None
    last_detected_at: Optional[datetime] = None
    description: Optional[str] = None
    remediation_suggestion: Optional[str] = None
    remediation_command: Optional[str] = None
    ai_confidence_score: Optional[float] = None


class RemediationResult(BaseModel):
    """Model for remediation execution results."""
    issue_id: UUID
    command: str
    stdout: str
    stderr: str
    return_code: int
    executed_at: datetime = Field(default_factory=datetime.utcnow)
    success: bool
