from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.database import IssueDB, IssueStatus, IssueSeverity, K8sObjectType, RemediationResult


class IssueResponse(IssueDB):
    """API response model for issue."""
    pass


class IssuesListParams(BaseModel):
    """Query parameters for listing issues."""
    status: Optional[List[IssueStatus]] = None
    severity: Optional[List[IssueSeverity]] = None
    k8s_object_type: Optional[List[K8sObjectType]] = None
    namespace: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class IssueStatusUpdate(BaseModel):
    """Request model for updating issue status."""
    status: IssueStatus


class ClusterStatusResponse(BaseModel):
    """Response model for cluster status summary."""
    nodes_total: int
    nodes_ready: int
    pods_total: int
    pods_running: int
    deployments_total: int
    deployments_ready: int
    services_total: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def nodes_percentage(self) -> float:
        """Calculate percentage of ready nodes."""
        return (self.nodes_ready / self.nodes_total * 100) if self.nodes_total > 0 else 0

    @property
    def pods_percentage(self) -> float:
        """Calculate percentage of running pods."""
        return (self.pods_running / self.pods_total * 100) if self.pods_total > 0 else 0

    @property
    def deployments_percentage(self) -> float:
        """Calculate percentage of ready deployments."""
        return (self.deployments_ready / self.deployments_total * 100) if self.deployments_total > 0 else 0


class RemediationRequest(BaseModel):
    """Request model for executing remediation."""
    confirm: bool = False


class RemediationResponse(RemediationResult):
    """Response model for remediation results."""
    pass


class HealthCheckResponse(BaseModel):
    """Response model for health check."""
    status: str = "ok"
    version: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
