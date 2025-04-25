"""
AI-powered remediation service using PydanticAI for streamlined
workflow from metrics collection to remediation execution.
"""
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from loguru import logger

from app.core.database import get_async_db  # Fixed import to use the correct function
from app.core.exceptions import ResourceNotFoundError
from app.models.ai_remediation_models import (
    RemediationContext, 
    RemediationPlan,
    WorkflowState,
    WorkflowStep,
    create_remediation_workflow,
    remediation_agent
)
from app.models.anomaly_event import AnomalyEvent, AnomalyStatus, RemediationAttempt
from app.services.anomaly_event_service import AnomalyEventService
from app.utils.k8s_executor import k8s_executor
from app.utils.metric_extractor import MetricExtractor


class AIRemediationService:
    """
    Service for AI-powered remediation workflows.
    Streamlines the process from metrics collection to remediation execution.
    """

    def __init__(self, anomaly_event_service: Optional[AnomalyEventService] = None):
        """Initialize the AI remediation service."""
        self.anomaly_event_service = anomaly_event_service or AnomalyEventService()
        self.metric_extractor = MetricExtractor()
        self._active_workflows: Dict[str, WorkflowState] = {}
    
    async def get_workflow(self, anomaly_id: str) -> WorkflowState:
        """
        Get an existing workflow or create a new one.
        
        Args:
            anomaly_id: ID of the anomaly
            
        Returns:
            WorkflowState object for the requested anomaly
            
        Raises:
            ResourceNotFoundError: If the anomaly does not exist
        """
        # Return existing workflow if available
        if anomaly_id in self._active_workflows:
            return self._active_workflows[anomaly_id]
        
        # Check if the anomaly exists
        event = await self.anomaly_event_service.get_event(anomaly_id)
        if not event:
            raise ResourceNotFoundError(f"Anomaly {anomaly_id} not found")
        
        # Create a new workflow
        workflow = create_remediation_workflow(anomaly_id)
        self._active_workflows[anomaly_id] = workflow
        workflow.add_log("Created new remediation workflow")
        return workflow

    async def list_workflows(self) -> List[Dict[str, Any]]:
        """
        List all active remediation workflows.
        
        Returns:
            List of workflow summary dictionaries
        """
        return [
            {
                "anomaly_id": workflow.anomaly_id,
                "current_step": workflow.current_step,
                "started_at": workflow.started_at,
                "updated_at": workflow.updated_at,
                "completed_steps": workflow.completed_steps,
                "has_plan": workflow.remediation_plan is not None,
            }
            for workflow in self._active_workflows.values()
        ]

    async def start_workflow(self, anomaly_id: str) -> WorkflowState:
        """
        Start or restart a remediation workflow for an anomaly.
        
        Args:
            anomaly_id: ID of the anomaly to remediate
            
        Returns:
            Updated workflow state
            
        Raises:
            ResourceNotFoundError: If the anomaly does not exist
        """
        # Get or create a workflow
        workflow = await self.get_workflow(anomaly_id)
        
        # Reset workflow if it exists
        workflow.current_step = WorkflowStep.METRICS_COLLECTION
        workflow.completed_steps = []
        workflow.updated_at = datetime.utcnow()
        workflow.add_log("Started remediation workflow")
        
        # Start processing asynchronously
        asyncio.create_task(self._process_workflow(workflow))
        
        return workflow

    async def execute_workflow(self, anomaly_id: str) -> Dict[str, Any]:
        """
        Execute a full remediation workflow from start to finish.
        
        Args:
            anomaly_id: ID of the anomaly to remediate
            
        Returns:
            Result of the workflow execution
        """
        workflow = await self.start_workflow(anomaly_id)
        
        # Wait for workflow to complete (with timeout)
        MAX_WAIT_TIME = 300  # 5 minutes
        start_time = datetime.utcnow()
        
        while (
            workflow.current_step not in [WorkflowStep.COMPLETED, WorkflowStep.FAILED]
            and (datetime.utcnow() - start_time).total_seconds() < MAX_WAIT_TIME
        ):
            await asyncio.sleep(1)
        
        # Return result or timeout
        if workflow.current_step in [WorkflowStep.COMPLETED, WorkflowStep.FAILED]:
            return workflow.result or {"status": "unknown", "message": "No result available"}
        else:
            workflow.add_log(
                f"Workflow execution timed out after {MAX_WAIT_TIME} seconds",
                details={"last_step": workflow.current_step}
            )
            return {
                "status": "timeout",
                "message": f"Workflow execution timed out after {MAX_WAIT_TIME} seconds",
                "last_step": workflow.current_step
            }

    async def get_remediation_plan(self, anomaly_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the current remediation plan for an anomaly.
        
        Args:
            anomaly_id: ID of the anomaly
            
        Returns:
            Remediation plan dictionary or None if no plan exists
        """
        workflow = await self.get_workflow(anomaly_id)
        
        if workflow.remediation_plan:
            return workflow.remediation_plan.model_dump()
        return None

    async def execute_remediation_plan(self, anomaly_id: str) -> Dict[str, Any]:
        """
        Execute the existing remediation plan for an anomaly.
        
        Args:
            anomaly_id: ID of the anomaly
            
        Returns:
            Result of the execution
        """
        workflow = await self.get_workflow(anomaly_id)
        
        # Check if plan exists
        if not workflow.remediation_plan:
            return {
                "status": "error",
                "message": "No remediation plan exists for this anomaly"
            }
        
        # Update workflow step
        workflow.update_step(WorkflowStep.REMEDIATION_EXECUTION)
        workflow.add_log("Starting remediation plan execution")
        
        # Execute the plan
        results = []
        event = await self.anomaly_event_service.get_event(anomaly_id)
        
        if not event:
            workflow.update_step(WorkflowStep.FAILED)
            workflow.add_log("Failed to get anomaly event for remediation")
            return {"status": "error", "message": "Anomaly event not found"}
        
        # Execute each action in sequence
        for i, action in enumerate(workflow.remediation_plan.actions):
            workflow.add_log(
                f"Executing remediation action {i+1}/{len(workflow.remediation_plan.actions)}",
                details={"action_type": action.action_type}
            )
            
            try:
                # Execute the action using k8s_executor
                result = await k8s_executor.execute_remediation_action(action)
                
                # Record the attempt
                attempt = RemediationAttempt(
                    command=action.action_type,
                    parameters=action.parameters,
                    executor="AI",
                    success="error" not in result,
                    result=str(result),
                )
                
                await self.anomaly_event_service.add_remediation_attempt(anomaly_id, attempt)
                results.append({
                    "action_type": action.action_type,
                    "resource_name": action.resource_name,
                    "success": "error" not in result,
                    "result": result
                })
                
                # Stop if action failed
                if "error" in result:
                    workflow.add_log(
                        f"Remediation action {i+1} failed, stopping execution",
                        details={"error": result.get("error")}
                    )
                    break
                    
            except Exception as e:
                logger.error(f"Error executing remediation action: {e}")
                workflow.add_log(
                    f"Error executing remediation action: {str(e)}",
                    details={"exception": str(e)}
                )
                results.append({
                    "action_type": action.action_type,
                    "resource_name": action.resource_name,
                    "success": False,
                    "error": str(e)
                })
                break
        
        # Update workflow with results
        all_successful = all(r.get("success", False) for r in results)
        
        if all_successful:
            workflow.update_step(WorkflowStep.VERIFICATION)
            workflow.add_log("All remediation actions completed successfully")
            
            # Update anomaly event status
            await self.anomaly_event_service.update_event(
                anomaly_id,
                {"status": AnomalyStatus.VERIFICATION_PENDING}
            )
        else:
            workflow.update_step(WorkflowStep.FAILED)
            workflow.add_log("Remediation execution failed")
            
            # Update anomaly event status
            await self.anomaly_event_service.update_event(
                anomaly_id,
                {"status": AnomalyStatus.REMEDIATION_FAILED}
            )
        
        workflow.result = {
            "status": "success" if all_successful else "error",
            "message": "Remediation completed successfully" if all_successful else "Remediation failed",
            "actions_executed": len(results),
            "actions_successful": sum(1 for r in results if r.get("success", False)),
            "results": results
        }
        
        return workflow.result

    async def _process_workflow(self, workflow: WorkflowState) -> None:
        """
        Process a remediation workflow asynchronously.
        
        Args:
            workflow: Workflow state to process
        """
        try:
            # Step 1: Collect metrics
            await self._collect_metrics(workflow)
            
            # Step 2: Analyze and generate plan
            await self._generate_remediation_plan(workflow)
            
            # Step 3: Execute remediation if confidence is high enough
            if (
                workflow.remediation_plan
                and workflow.remediation_plan.confidence >= 0.75
            ):
                await self.execute_remediation_plan(workflow.anomaly_id)
            else:
                workflow.update_step(WorkflowStep.COMPLETED)
                workflow.add_log(
                    "Workflow completed without execution - low confidence",
                    details={"confidence": workflow.remediation_plan.confidence if workflow.remediation_plan else 0}
                )
                workflow.result = {
                    "status": "success",
                    "message": "Remediation plan generated but not executed due to low confidence",
                    "plan": workflow.remediation_plan.model_dump() if workflow.remediation_plan else None
                }
        
        except Exception as e:
            logger.error(f"Error processing workflow: {e}")
            workflow.update_step(WorkflowStep.FAILED)
            workflow.add_log(
                f"Workflow processing error: {str(e)}",
                details={"exception": str(e)}
            )
            workflow.result = {
                "status": "error",
                "message": f"Workflow processing error: {str(e)}"
            }

    async def _collect_metrics(self, workflow: WorkflowState) -> None:
        """
        Collect metrics and context for an anomaly.
        
        Args:
            workflow: Current workflow state
        """
        workflow.add_log("Collecting metrics and context")
        
        try:
            # Get the anomaly event
            event = await self.anomaly_event_service.get_event(workflow.anomaly_id)
            if not event:
                raise ResourceNotFoundError(f"Anomaly {workflow.anomaly_id} not found")
            
            # Collect additional metrics if needed
            additional_metrics = []
            if event.entity_type == "pod" and event.namespace:
                # Get pod metrics
                pod_metrics = await self.metric_extractor.get_pod_metrics(
                    event.entity_id, event.namespace, lookback_minutes=30
                )
                additional_metrics.extend(pod_metrics)
                
                # Get logs if available
                logs = await self._collect_logs(event)
                
            elif event.entity_type == "node":
                # Get node metrics
                node_metrics = await self.metric_extractor.get_node_metrics(
                    event.entity_id, lookback_minutes=30
                )
                additional_metrics.extend(node_metrics)
            
            # Get cluster-wide metrics for context
            cluster_metrics = await self.metric_extractor.get_cluster_metrics(lookback_minutes=10)
            
            # Update workflow to next step
            workflow.update_step(WorkflowStep.ROOT_CAUSE_ANALYSIS)
            workflow.add_log("Metrics collection complete", details={
                "metrics_collected": len(additional_metrics)
            })
            
        except Exception as e:
            logger.error(f"Error collecting metrics: {e}")
            workflow.update_step(WorkflowStep.FAILED)
            workflow.add_log(f"Metrics collection error: {str(e)}")
            raise

    async def _collect_logs(self, event: AnomalyEvent) -> Optional[str]:
        """
        Collect logs for a pod anomaly.
        
        Args:
            event: Anomaly event
            
        Returns:
            Log excerpt or None if logs cannot be collected
        """
        if event.entity_type != "pod" or not event.namespace:
            return None
        
        try:
            # Get logs from the pod
            result = await k8s_executor.execute_validated_command(
                "view_logs",
                {
                    "name": event.entity_id,
                    "namespace": event.namespace,
                    "tail": 100  # Get the last 100 lines
                }
            )
            
            if "error" in result:
                logger.warning(f"Failed to get logs: {result.get('error')}")
                return None
                
            return result.get("logs", "")
        except Exception as e:
            logger.error(f"Error getting logs: {e}")
            return None

    async def _generate_remediation_plan(self, workflow: WorkflowState) -> None:
        """
        Generate a remediation plan using PydanticAI.
        
        Args:
            workflow: Current workflow state
        """
        workflow.update_step(WorkflowStep.ROOT_CAUSE_ANALYSIS)
        workflow.add_log("Starting root cause analysis and plan generation")
        
        try:
            # Get the anomaly event
            event = await self.anomaly_event_service.get_event(workflow.anomaly_id)
            if not event:
                raise ResourceNotFoundError(f"Anomaly {workflow.anomaly_id} not found")
            
            # Create context for the agent
            context = RemediationContext(
                anomaly_id=event.anomaly_id,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                namespace=event.namespace,
                anomaly_score=event.anomaly_score,
                metric_data=event.metric_snapshot,
                status=event.status,
                previous_attempts=[
                    attempt.model_dump() for attempt in (event.remediation_attempts or [])
                ],
            )
            
            # Add logs if available for pods
            if event.entity_type == "pod" and event.namespace:
                context.logs_excerpt = await self._collect_logs(event)
            
            # Generate prompt based on event data
            prompt = self._generate_prompt(event)
            
            # Update workflow
            workflow.update_step(WorkflowStep.PLAN_GENERATION)
            workflow.add_log("Generating remediation plan")
            
            # Execute agent
            result = await remediation_agent.run(prompt, deps=context)
            
            # Store the plan
            plan = result.output
            workflow.remediation_plan = plan
            
            # Update anomaly event with remediation suggestions
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                {"status": AnomalyStatus.REMEDIATION_SUGGESTED,
                 "suggested_remediation_commands": plan.actions,
                 "ai_analysis": {
                     "diagnosis": plan.diagnosis,
                     "severity": plan.severity,
                     "estimated_impact": plan.estimated_impact,
                     "confidence": plan.confidence
                 }}
            )
            
            workflow.add_log("Remediation plan generated successfully", details={
                "severity": plan.severity,
                "confidence": plan.confidence,
                "action_count": len(plan.actions)
            })
            
        except Exception as e:
            logger.error(f"Error generating remediation plan: {e}")
            workflow.update_step(WorkflowStep.FAILED)
            workflow.add_log(f"Plan generation error: {str(e)}")
            raise

    def _generate_prompt(self, event: AnomalyEvent) -> str:
        """
        Generate a prompt for the remediation agent based on event data.
        
        Args:
            event: Anomaly event
            
        Returns:
            Prompt string for the agent
        """
        entity_details = f"{event.entity_type} '{event.entity_id}'"
        if event.namespace:
            entity_details += f" in namespace '{event.namespace}'"
        
        status_info = f"Current status: {event.status.value}"
        
        # Add specific details based on entity type
        entity_specific_details = ""
        if event.entity_type == "pod":
            entity_specific_details = (
                "Check for issues like CrashLoopBackOff, ImagePullBackOff, "
                "resource constraints, or container failures."
            )
        elif event.entity_type == "node":
            entity_specific_details = (
                "Check for issues like resource exhaustion, kubelet problems, "
                "or network connectivity issues."
            )
        elif event.entity_type == "deployment":
            entity_specific_details = (
                "Check for issues like update failures, replica problems, "
                "or resource constraints affecting all pods."
            )
        
        # Build the full prompt
        prompt = (
            f"Analyze this Kubernetes anomaly for {entity_details}. {status_info}. "
            f"Anomaly score: {event.anomaly_score}. {entity_specific_details} "
            f"Create a remediation plan with appropriate actions, verification steps, and contingency plans."
        )
        
        return prompt


# Global service instance
ai_remediation_service = AIRemediationService()