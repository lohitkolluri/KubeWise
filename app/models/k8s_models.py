from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RemediationAction(BaseModel):
    """Structured template-based remediation action"""

    action_type: str = Field(
        ...,
        description="Type of remediation action (e.g., restart_deployment, scale_deployment)",
    )
    resource_type: str = Field(
        ..., description="Resource type to act on (e.g., deployment, pod, node)"
    )
    resource_name: str = Field(..., description="Name of the resource to act on")
    namespace: Optional[str] = Field(
        None, description="Kubernetes namespace of the resource"
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="Additional parameters for the action"
    )
    command: Optional[str] = Field(
        None, description="Formatted command for the action"
    )
    confidence: Optional[float] = Field(
        0.5, description="Confidence score for this remediation action (0.0-1.0)"
    )
