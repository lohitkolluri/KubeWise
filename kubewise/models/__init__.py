"""
KubeWise data models module.

This module provides Pydantic models for KubeWise data types and validation.
"""

from __future__ import annotations

# Base types
from kubewise.models.base import BaseKubeWiseModel, PyObjectId, generate_objectid

# Import all schema models directly from schema.py (avoids circular imports)
from kubewise.models.schema import (
    ActionType,
    MetricPoint,
    KubernetesEvent,
    AnomalyRecord,
    RemediationAction,
    RemediationPlan,
    ExecutedActionRecord,
)

# Import diagnosis models from diagnosis module
from kubewise.models.diagnosis import (
    DiagnosticTool,
    DiagnosticResult,
    DiagnosticFinding,
    DiagnosticPlan,
    DiagnosticPlanStatus,
    DiagnosisEntry,
    DiagnosisStatus,
)

# Re-export all imports
__all__ = [
    # Base types
    "BaseKubeWiseModel",
    "PyObjectId",
    "generate_objectid",
    # Schema models
    "ActionType",
    "MetricPoint",
    "KubernetesEvent",
    "AnomalyRecord",
    "RemediationAction",
    "RemediationPlan",
    "ExecutedActionRecord",
    # Diagnosis models
    "DiagnosticTool",
    "DiagnosticResult",
    "DiagnosticFinding",
    "DiagnosticPlan",
    "DiagnosticPlanStatus",
    "DiagnosisEntry",
    "DiagnosisStatus",
]

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    """Type of remediation action to perform."""

    # Existing action types
    SCALE_DEPLOYMENT = "scale_deployment"
    DELETE_POD = "delete_pod"
    RESTART_DEPLOYMENT = "restart_deployment"
    RESTART_STATEFULSET = "restart_statefulset"
    RESTART_DAEMONSET = "restart_daemonset"
    DRAIN_NODE = "drain_node"
    SCALE_STATEFULSET = "scale_statefulset"
    TAINT_NODE = "taint_node"
    EVICT_POD = "evict_pod"
    VERTICAL_SCALE_DEPLOYMENT = "vertical_scale_deployment"
    VERTICAL_SCALE_STATEFULSET = "vertical_scale_statefulset"
    CORDON_NODE = "cordon_node"
    UNCORDON_NODE = "uncordon_node"
    MANUAL_INTERVENTION = "manual_intervention"
    
    # New action types for enhanced functionality
    RESTART_CRONJOB = "restart_cronjob"
    RESTART_JOB = "restart_job" 
    UPDATE_CONFIG_MAP = "update_configmap"
    UPDATE_SECRET = "update_secret"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    ROLLBACK_STATEFULSET = "rollback_statefulset"
    ROLLBACK_DAEMONSET = "rollback_daemonset"
    FIX_SERVICE_SELECTORS = "fix_service_selectors"
    ADD_NETWORK_POLICY = "add_network_policy"
    MODIFY_NETWORK_POLICY = "modify_network_policy"
    DELETE_NETWORK_POLICY = "delete_network_policy"
    FIX_PVC_BINDING = "fix_pvc_binding"
    SCALE_HPA = "scale_hpa"
    REPAIR_INGRESS = "repair_ingress"
    UPGRADE_CLUSTER = "upgrade_cluster"
    ADD_TOLERATION = "add_toleration"
    MODIFY_NODE_SELECTOR = "modify_node_selector"
    REMOVE_FINALIZER = "remove_finalizer"
    UPDATE_RESOURCE_LIMITS = "update_resource_limits"
    APPLY_PATCH = "apply_patch"
    REBALANCE_PODS = "rebalance_pods"
    FORCE_DELETE_POD = "force_delete_pod"
    CLEAR_TERMINATING_NAMESPACE = "clear_terminating_namespace"
    UPDATE_CONTAINER_IMAGE = "update_container_image"
    FIX_DNS_CONFIG = "fix_dns_config"
    RUN_JOB = "run_job"
    APPLY_YAML = "apply_yaml"


class RemediationAction(BaseModel):
    """Represents a single remediation action to be taken."""

    action_type: str  # Using string instead of ActionType for extensibility
    parameters: Dict[str, Any]
    description: Optional[str] = None
    justification: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    priority: Optional[int] = 1  # 1 is highest priority, higher numbers = lower priority
    
    # New fields for enhanced functionality
    depends_on: Optional[List[str]] = None  # IDs of actions this depends on
    timeout_seconds: Optional[int] = 300  # Default timeout for action execution
    retry_strategy: Optional[Dict[str, Any]] = None  # Settings for retry if action fails
    rollback_action: Optional[Dict[str, Any]] = None  # Action to execute if this fails
    validation_checks: Optional[List[Dict[str, Any]]] = None  # List of checks to validate success

    class Config:
        """Pydantic model configuration."""

        json_encoders = {datetime: lambda v: v.isoformat()}
        # Allow arbitrary types for ActionType from strings not in enum
        use_enum_values = True
