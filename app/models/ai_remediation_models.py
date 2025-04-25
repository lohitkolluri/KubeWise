"""
AI-powered remediation models using PydanticAI for enhanced workflow processing.
This module extends the standard remediation models with AI capabilities.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent, RunContext

from app.models.anomaly_event import AnomalyStatus
from app.models.k8s_models import RemediationAction


class RemediationContext(BaseModel):
    """Context data for AI-powered remediation processes"""
    
    anomaly_id: str = Field(..., description="ID of the anomaly being remediated")
    entity_type: str = Field(..., description="Type of entity with the anomaly (pod, node, etc.)")
    entity_id: str = Field(..., description="ID of the entity with the anomaly")
    namespace: Optional[str] = Field(None, description="Kubernetes namespace of the affected resource")
    anomaly_score: float = Field(..., description="Severity score of the anomaly (0.0-1.0)")
    metric_data: List[Dict[str, Any]] = Field(default_factory=list, description="Relevant metric data for the anomaly")
    logs_excerpt: Optional[str] = Field(None, description="Relevant logs from the affected resource")
    status: AnomalyStatus = Field(..., description="Current status of the anomaly")
    previous_attempts: List[Dict[str, Any]] = Field(default_factory=list, description="Previous remediation attempts")
    cluster_state: Optional[Dict[str, Any]] = Field(None, description="Current state of the relevant cluster components")


class RemediationPlan(BaseModel):
    """AI-generated remediation plan with multiple steps"""
    
    anomaly_id: str = Field(..., description="ID of the anomaly being remediated")
    diagnosis: str = Field(..., description="AI diagnosis of the anomaly root cause")
    severity: str = Field(..., description="Assessed severity level (Low, Medium, High, Critical)")
    actions: List[RemediationAction] = Field(
        ..., description="Ordered list of remediation actions to take"
    )
    verification_steps: List[str] = Field(
        ..., description="Steps to verify successful remediation"
    )
    rollback_plan: Optional[List[RemediationAction]] = Field(
        None, description="Actions to take if remediation fails"
    )
    estimated_impact: str = Field(
        ..., description="Expected impact of the remediation actions"
    )
    confidence: float = Field(
        ..., description="Overall confidence in the remediation plan (0.0-1.0)",
        ge=0.0,
        le=1.0,
    )


class WorkflowStep(str, Enum):
    """Steps in the remediation workflow"""
    
    METRICS_COLLECTION = "MetricsCollection"
    ANOMALY_DETECTION = "AnomalyDetection"
    ROOT_CAUSE_ANALYSIS = "RootCauseAnalysis"
    PLAN_GENERATION = "PlanGeneration"
    REMEDIATION_EXECUTION = "RemediationExecution"
    VERIFICATION = "Verification"
    ROLLBACK = "Rollback"
    COMPLETED = "Completed"
    FAILED = "Failed"


class WorkflowState(BaseModel):
    """Current state of the remediation workflow"""
    
    anomaly_id: str = Field(..., description="ID of the anomaly being remediated")
    current_step: WorkflowStep = Field(..., description="Current step in the workflow")
    started_at: datetime = Field(default_factory=datetime.utcnow, description="When the workflow was started")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="When the workflow was last updated")
    completed_steps: List[WorkflowStep] = Field(default_factory=list, description="Steps that have been completed")
    remediation_plan: Optional[RemediationPlan] = Field(None, description="Generated remediation plan")
    logs: List[Dict[str, Any]] = Field(default_factory=list, description="Workflow execution logs")
    result: Optional[Dict[str, Any]] = Field(None, description="Final result of the workflow")
    
    def update_step(self, step: WorkflowStep) -> None:
        """Update the workflow to a new step"""
        if self.current_step not in self.completed_steps and self.current_step != step:
            self.completed_steps.append(self.current_step)
        self.current_step = step
        self.updated_at = datetime.utcnow()
        
    def add_log(self, message: str, step: Optional[WorkflowStep] = None, details: Optional[Dict[str, Any]] = None) -> None:
        """Add a log entry to the workflow"""
        self.logs.append({
            "timestamp": datetime.utcnow(),
            "step": step or self.current_step,
            "message": message,
            "details": details or {}
        })
        self.updated_at = datetime.utcnow()


# Initialize the AI-powered remediation agent using PydanticAI
# Using the correct format for Gemini model
remediation_agent = Agent(
    'google-gla:gemini-2.0-flash', 
    deps_type=RemediationContext,
    output_type=RemediationPlan,
    system_prompt="""
    You are KubeWise's AI Remediation Assistant. Your job is to:
    
    1. Analyze Kubernetes anomaly data and metrics
    2. Diagnose the root cause of the issue
    3. Generate a comprehensive remediation plan
    
    Focus on accurate diagnosis and efficient remediation while minimizing service disruption.
    Your remediation actions should follow Kubernetes best practices and be ordered from
    least to most disruptive.
    
    For each anomaly context, provide:
    - A clear diagnosis of the root cause
    - A severity assessment
    - A step-by-step remediation plan using the RemediationAction format
    - Verification steps to confirm successful resolution
    - A rollback plan for safety
    - Impact assessment and confidence score
    """
)


@remediation_agent.tool
def analyze_metrics(ctx: RunContext[RemediationContext], metric_names: List[str]) -> Dict[str, Any]:
    """
    Analyze specific metrics from the context to identify patterns and anomalies.
    
    Args:
        metric_names: List of specific metrics to analyze
        
    Returns:
        Dictionary with analysis results
    """
    results = {}
    relevant_metrics = []
    
    # Filter metrics based on requested names
    for metric_data in ctx.deps.metric_data:
        for name in metric_names:
            if name in metric_data.get("name", ""):
                relevant_metrics.append(metric_data)
                break
    
    # Simple statistical analysis (would be enhanced in production)
    for name in metric_names:
        name_metrics = [m for m in relevant_metrics if name in m.get("name", "")]
        if name_metrics:
            values = [m.get("value", 0) for m in name_metrics]
            results[name] = {
                "count": len(values),
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "avg": sum(values) / len(values) if values else None,
                "latest": values[-1] if values else None
            }
    
    return {
        "analyzed_metrics": results,
        "anomaly_indicators": [name for name, data in results.items() 
                              if data["latest"] and data["avg"] and data["latest"] > data["avg"] * 1.5]
    }


@remediation_agent.tool
def get_previous_remediation_success_rate(ctx: RunContext[RemediationContext], action_type: str) -> float:
    """
    Calculate the success rate of previous remediation attempts of a specific type.
    
    Args:
        action_type: Type of remediation action to analyze
        
    Returns:
        Success rate from 0.0 to 1.0
    """
    if not ctx.deps.previous_attempts:
        return 0.0
    
    relevant_attempts = [
        attempt for attempt in ctx.deps.previous_attempts
        if attempt.get("command", "").startswith(action_type)
    ]
    
    if not relevant_attempts:
        return 0.0
    
    successful = sum(1 for attempt in relevant_attempts if attempt.get("success", False))
    return successful / len(relevant_attempts)


@remediation_agent.tool
def analyze_logs(ctx: RunContext[RemediationContext], search_terms: List[str]) -> Dict[str, Any]:
    """
    Search logs for specific patterns or error messages.
    
    Args:
        search_terms: List of terms or patterns to search for
        
    Returns:
        Dictionary with log analysis results
    """
    if not ctx.deps.logs_excerpt:
        return {"error": "No logs available for analysis"}
    
    results = {}
    logs = ctx.deps.logs_excerpt.split("\n")
    
    for term in search_terms:
        matching_lines = [line for line in logs if term.lower() in line.lower()]
        results[term] = {
            "count": len(matching_lines),
            "lines": matching_lines[:5],  # Return up to 5 matching lines
            "has_matches": len(matching_lines) > 0
        }
    
    return {
        "total_logs": len(logs),
        "matches": results,
        "common_errors": [term for term, data in results.items() if data["count"] > 2]
    }


# Factory function to create a remediation workflow
def create_remediation_workflow(anomaly_id: str) -> WorkflowState:
    """
    Create a new remediation workflow for an anomaly.
    
    Args:
        anomaly_id: ID of the anomaly to remediate
        
    Returns:
        New workflow state initialized to the first step
    """
    return WorkflowState(
        anomaly_id=anomaly_id,
        current_step=WorkflowStep.METRICS_COLLECTION,
        started_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        completed_steps=[],
        logs=[]
    )