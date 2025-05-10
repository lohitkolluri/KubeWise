"""
KubeWise data models module.

This module provides Pydantic models for KubeWise data types and validation.
"""

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
