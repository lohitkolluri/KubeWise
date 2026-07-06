# -*- coding: utf-8 -*-
"""
Diagnosis engine for Kubernetes anomalies using a structured, evidence-based approach.
"""

import asyncio
import datetime
import json
import re
import time
from typing import Any, Dict, List, Optional, Union, Tuple

import motor.motor_asyncio
from bson import ObjectId
from kubernetes import client
from kubewise.logging import get_logger
from pydantic import ValidationError
from pymongo import ReplaceOne
from pydantic_ai import Agent

# Initialize logger with component name
logger = get_logger("remediation.diagnosis")

from kubewise.config import settings
from kubewise.models import (
    AnomalyRecord,
    DiagnosisEntry,
    DiagnosisStatus,
    DiagnosticFinding,
    DiagnosticPlan,
    DiagnosticPlanStatus,
    DiagnosticResult,
    DiagnosticTool,
    PyObjectId,
    RemediationPlan,
)
from kubewise.remediation.planner import (
    DiagnosticTools,
    PlannerDependencies,
    generate_remediation_plan,
)
from kubewise.remediation.engine import execute_remediation_plan
from kubewise.utils.retry import with_exponential_backoff


class DiagnosisEngine:
    """
    Manages the diagnosis process for Kubernetes anomalies.

    Coordinates preliminary analysis, diagnostic tests, finding extraction,
    root cause validation, and triggers remediation planning.
    """

    def __init__(
        self,
        db: motor.motor_asyncio.AsyncIOMotorDatabase,
        k8s_client: client.ApiClient,
        ai_agent: Optional[Agent] = None,
    ):
        """Initialize the DiagnosisEngine.

        Args:
            db: Async MongoDB database client.
            k8s_client: Async Kubernetes API client.
            ai_agent: Optional AI agent for advanced analysis.
        """
        self.db = db
        self.k8s_client = k8s_client
        self.ai_agent = ai_agent
        self.diagnostic_tools = DiagnosticTools(k8s_client)

        self._diagnosis_collection = db["diagnoses"]
        self._diagnostic_plans_collection = db["diagnostic_plans"]
        self._diagnostic_results_collection = db["diagnostic_results"]
        self._diagnostic_findings_collection = db["diagnostic_findings"]

    async def initiate_diagnosis(self, anomaly_record: AnomalyRecord) -> DiagnosisEntry:
        """
        Starts the diagnosis workflow for a given anomaly.

        Args:
            anomaly_record: The anomaly record to diagnose.

        Returns:
            The created DiagnosisEntry.

        Raises:
            ValueError: If the anomaly record does not have an ID.
        """
        if not anomaly_record.id:
            raise ValueError("Anomaly record must have an ID for diagnosis")

        diagnosis = DiagnosisEntry(
            anomaly_id=anomaly_record.id,
            entity_id=anomaly_record.entity_id,
            entity_type=anomaly_record.entity_type,
            namespace=anomaly_record.namespace,
            status=DiagnosisStatus.PENDING,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )

        result = await self._diagnosis_collection.insert_one(
            diagnosis.model_dump(mode="json")
        )
        diagnosis.id = str(result.inserted_id)  # Assign the MongoDB generated ID

        logger.info(
            f"Initiated diagnosis {diagnosis.id} for anomaly {anomaly_record.id}"
        )

        # Run the workflow sequentially and wait for its completion
        await self._diagnosis_workflow(diagnosis, anomaly_record)

        return diagnosis

    async def _diagnosis_workflow(
        self, diagnosis: DiagnosisEntry, anomaly_record: AnomalyRecord
    ) -> None:
        """Executes the full diagnosis workflow for an anomaly."""
        try:
            # 1. Initial Hypothesis
            await self._update_diagnosis_status(diagnosis.id, DiagnosisStatus.ANALYZING)
            diagnosis = await self._generate_preliminary_diagnosis(
                diagnosis, anomaly_record
            )

            # 2. Diagnostic Testing
            await self._update_diagnosis_status(
                diagnosis.id, DiagnosisStatus.DIAGNOSING
            )
            # Execute diagnostic plan and get results and findings
            results, findings = await self._execute_diagnostic_plan(
                diagnosis, anomaly_record
            )

            # 3. Validation & Root Cause Determination
            await self._update_diagnosis_status(
                diagnosis.id, DiagnosisStatus.VALIDATING
            )
            # Log diagnostic validation info in a single message to improve grouping
            findings_count = len(findings) if isinstance(findings, list) else 0
            logger.debug(
                "Validating diagnosis",
                diagnosis_id=str(diagnosis.id),
                findings_count=findings_count
            )

            # Perform validation with findings parameter
            diagnosis = await self._validate_diagnosis(
                diagnosis, anomaly_record, findings
            )

            # 4. Mark Diagnosis Complete (only if not already INCONCLUSIVE or FAILED)
            if diagnosis.status not in [
                DiagnosisStatus.INCONCLUSIVE,
                DiagnosisStatus.FAILED,
            ]:
                diagnosis.status = DiagnosisStatus.COMPLETE

            diagnosis.completed_at = datetime.datetime.now(datetime.timezone.utc)

            diagnosis_id = (
                ObjectId(diagnosis.id)
                if isinstance(diagnosis.id, str)
                else diagnosis.id
            )
            await self._diagnosis_collection.update_one(
                {"_id": diagnosis_id},
                {"$set": diagnosis.model_dump(exclude={"id"}, mode="json")},
            )

            # 5. Generate Remediation Plan (if diagnosis conclusive)
            remediation_plan = None
            if (
                diagnosis.status == DiagnosisStatus.COMPLETE
                and diagnosis.root_cause_confidence
                >= settings.diagnosis_confidence_threshold
            ):
                remediation_plan = await self._generate_remediation_plan(
                    diagnosis, anomaly_record
                )

            anomaly_id = (
                ObjectId(anomaly_record.id)
                if isinstance(anomaly_record.id, str)
                else anomaly_record.id
            )
            anomaly_update = {
                "diagnosis_id": str(diagnosis.id),
                "remediation_status": "diagnosis_complete",  # Default status after diagnosis
            }
            if remediation_plan and remediation_plan.id:
                plan_id_str = str(remediation_plan.id)
                anomaly_update["remediation_plan_id"] = plan_id_str
                anomaly_update["remediation_status"] = "planned"
                # Update diagnosis entry with the plan ID
                await self.db["anomalies"].update_one(
                    {"_id": anomaly_id},
                    {"$set": {"remediation_plan_id": plan_id_str}},
                )

            # Update the anomaly record with diagnosis/plan IDs
            await self.db["anomalies"].update_one(
                {"_id": anomaly_id}, {"$set": anomaly_update}
            )

            # 6. Execute Remediation (if plan exists and conditions met)
            if remediation_plan and remediation_plan.id:
                logger.info(
                    f"Executing remediation plan {remediation_plan.id} for anomaly {anomaly_record.id}"
                )
                planner_deps = PlannerDependencies(
                    db=self.db,
                    k8s_client=self.k8s_client,
                    diagnostic_tools=self.diagnostic_tools,
                )

                namespace = anomaly_record.namespace or "default"
                if namespace in settings.blacklisted_namespaces:
                    # Handle blacklisted namespace case
                    logger.warning(
                        f"Remediation blocked for anomaly {anomaly_record.id} in blacklisted namespace {namespace}"
                    )
                    await self.db["anomalies"].update_one(
                        {"_id": anomaly_id},
                        {
                            "$set": {
                                "remediation_status": "blocked",
                                "remediation_error": f"Namespace {namespace} is blacklisted.",
                                "remediation_message": f"Remediation blocked: Namespace {namespace} is blacklisted.",
                            }
                        },
                    )
                elif settings.remediation_disabled:
                    logger.info(
                        f"Remediation disabled globally. Skipping execution for plan {remediation_plan.id}."
                    )
                    await self.db["anomalies"].update_one(
                        {"_id": anomaly_id},
                        {
                            "$set": {
                                "remediation_status": "skipped_disabled",
                                "remediation_message": "Remediation skipped: Global remediation is disabled.",
                            }
                        },
                    )
                else:
                    # Execute the plan
                    execution_result = await execute_remediation_plan(
                        plan=remediation_plan,
                        anomaly_record=anomaly_record,
                        dependencies=planner_deps,
                        dry_run=False,  # Assuming actual execution
                    )
                    if execution_result:
                        logger.info(
                            f"Successfully executed remediation plan {remediation_plan.id} for anomaly {anomaly_record.id}"
                        )
                    else:
                        logger.error(
                            f"Failed to execute remediation plan {remediation_plan.id} for anomaly {anomaly_record.id}"
                        )

            logger.info(
                f"Completed diagnosis workflow for anomaly {anomaly_record.id} with status {diagnosis.status}"
            )

        except Exception as e:
            logger.exception(
                f"Error during diagnosis workflow for diagnosis {diagnosis.id}: {e}"
            )
            # Update diagnosis and anomaly to FAILED status
            await self._update_diagnosis_status(
                diagnosis.id,
                DiagnosisStatus.FAILED,
                error_message=f"Workflow error: {str(e)}",
            )
            anomaly_id = (
                ObjectId(anomaly_record.id)
                if isinstance(anomaly_record.id, str)
                else anomaly_record.id
            )
            await self.db["anomalies"].update_one(
                {"_id": anomaly_id},
                {
                    "$set": {
                        "diagnosis_id": str(diagnosis.id),
                        "remediation_status": "diagnosis_failed",
                        "remediation_error": f"Diagnosis workflow failed: {str(e)}",
                    }
                },
            )

    async def _update_diagnosis_status(
        self,
        diagnosis_id: Union[str, PyObjectId, ObjectId],
        status: DiagnosisStatus,
        error_message: Optional[str] = None,
        **additional_fields,
    ) -> None:
        """Atomically updates the status and optional fields of a diagnosis entry."""
        update_data = {"status": status.value}  # Use enum value
        if error_message:
            update_data["error_message"] = error_message
        if additional_fields:
            update_data.update(additional_fields)

        if isinstance(diagnosis_id, str):
            obj_id = ObjectId(diagnosis_id)
        elif isinstance(diagnosis_id, PyObjectId):
            obj_id = ObjectId(str(diagnosis_id))  # Convert custom type if needed
        else:
            obj_id = diagnosis_id  # Assume already ObjectId

        await self._diagnosis_collection.update_one(
            {"_id": obj_id}, {"$set": update_data}
        )
        logger.debug(f"Updated diagnosis {obj_id} status to {status.value}")

    async def _generate_preliminary_diagnosis(
        self, diagnosis: DiagnosisEntry, anomaly_record: AnomalyRecord
    ) -> DiagnosisEntry:
        """Generates an initial hypothesis about the anomaly's cause."""
        # Prefer AI if available and enabled, otherwise fall back to rules.
        if (
            self.ai_agent and settings.enable_diagnosis_workflow
        ):  # Check if AI workflow is enabled
            try:
                diagnosis = await self._ai_preliminary_diagnosis(
                    diagnosis, anomaly_record
                )
            except Exception as e:
                logger.warning(
                    f"AI preliminary diagnosis failed ({e}), falling back to rules-based."
                )
                diagnosis = await self._rules_based_preliminary_diagnosis(
                    diagnosis, anomaly_record
                )
        else:
            diagnosis = await self._rules_based_preliminary_diagnosis(
                diagnosis, anomaly_record
            )

        # Persist the preliminary diagnosis fields
        diagnosis_id = (
            ObjectId(diagnosis.id) if isinstance(diagnosis.id, str) else diagnosis.id
        )
        await self._diagnosis_collection.update_one(
            {"_id": diagnosis_id},
            {
                "$set": {
                    "preliminary_diagnosis": diagnosis.preliminary_diagnosis,
                    "preliminary_confidence": diagnosis.preliminary_confidence,
                    "diagnosis_reasoning": diagnosis.diagnosis_reasoning,  # Store reasoning if available
                }
            },
        )
        logger.info(
            f"Generated preliminary diagnosis for anomaly {anomaly_record.id}: "
            f"'{diagnosis.preliminary_diagnosis}' (confidence: {diagnosis.preliminary_confidence:.2f})"
        )
        return diagnosis

    async def _rules_based_preliminary_diagnosis(
        self, diagnosis: DiagnosisEntry, anomaly_record: AnomalyRecord
    ) -> DiagnosisEntry:
        """Generates a preliminary diagnosis using predefined rules based on anomaly data."""
        entity_type = anomaly_record.entity_type.lower()
        failure_reason = anomaly_record.failure_reason or ""
        metric_name = anomaly_record.metric_name or ""
        event_reason = anomaly_record.event_reason or ""
        metric_value = anomaly_record.metric_value or 0.0

        # Simple rules based on common Kubernetes failure modes
        if "OOMKilled" in event_reason:
            diagnosis.preliminary_diagnosis = "Memory limit exceeded (OOMKilled)"
            diagnosis.preliminary_confidence = 0.85
        elif "CrashLoopBackOff" in event_reason:
            diagnosis.preliminary_diagnosis = (
                "Application crashing repeatedly (CrashLoopBackOff)"
            )
            diagnosis.preliminary_confidence = 0.75
        elif "ImagePullBackOff" in event_reason or "ErrImagePull" in event_reason:
            diagnosis.preliminary_diagnosis = "Failed to pull container image"
            diagnosis.preliminary_confidence = 0.9
        elif "cpu_utilization" in metric_name and metric_value > 90:
            diagnosis.preliminary_diagnosis = "High CPU utilization detected"
            diagnosis.preliminary_confidence = 0.65
        elif "memory_utilization" in metric_name and metric_value > 90:
            diagnosis.preliminary_diagnosis = "High Memory utilization detected"
            diagnosis.preliminary_confidence = 0.65
        elif entity_type == "node" and "NotReady" in failure_reason:
            diagnosis.preliminary_diagnosis = "Node is in NotReady state"
            diagnosis.preliminary_confidence = 0.8
        elif entity_type == "node" and "Pressure" in failure_reason:
            diagnosis.preliminary_diagnosis = (
                f"Node experiencing pressure ({failure_reason})"
            )
            diagnosis.preliminary_confidence = 0.7
        else:
            diagnosis.preliminary_diagnosis = f"Unspecified issue with {entity_type} {anomaly_record.name or anomaly_record.entity_id}"
            diagnosis.preliminary_confidence = 0.3

        diagnosis.diagnosis_reasoning = (
            "Generated via rules-based preliminary analysis."
        )
        return diagnosis

    @with_exponential_backoff()
    async def _run_ai_agent_with_retry(self, prompt: str, **kwargs) -> Any:
        """Helper to run the AI agent with retry logic."""
        if not self.ai_agent:
            # This case should ideally be caught before calling, but as a safeguard:
            logger.error("AI agent not available for _run_ai_agent_with_retry")
            raise RuntimeError("AI agent not configured.")
        return await self.ai_agent.run(prompt, **kwargs)

    async def _ai_preliminary_diagnosis(
        self, diagnosis: DiagnosisEntry, anomaly_record: AnomalyRecord
    ) -> DiagnosisEntry:
        """Generates a preliminary diagnosis using the AI agent."""
        if not self.ai_agent:
            # Log and fallback or raise, but _run_ai_agent_with_retry will also check
            logger.warning("AI agent not configured for AI preliminary diagnosis. Skipping AI step.")
            diagnosis.preliminary_diagnosis = "AI agent not configured"
            diagnosis.preliminary_confidence = 0.0
            diagnosis.diagnosis_reasoning = "AI agent was not available for preliminary diagnosis."
            return diagnosis

        context = anomaly_record.model_dump(mode="json", exclude_none=True)

        try:
            context_json = json.dumps(context, indent=2)
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize anomaly context for AI: {e}")
            # Create a minimal context for the AI
            context_json = json.dumps(
                {
                    "entity_type": anomaly_record.entity_type,
                    "entity_id": anomaly_record.entity_id,
                    "anomaly_type": getattr(anomaly_record, "anomaly_type", "unknown"),
                    "score": getattr(anomaly_record, "anomaly_score", 0),
                }
            )

        prompt = f"""
        Analyze this Kubernetes anomaly and provide a preliminary diagnosis. The anomaly data contains information about the detected issue.

        Anomaly Data:
        {context_json}

        Based on the anomaly details, provide:
        1. A preliminary diagnosis of what might be causing the anomaly
        2. A confidence score (0.0-1.0) for your diagnosis
        3. Brief reasoning supporting your diagnosis

        Return your analysis in the following JSON structure:
        ```json
        {{
          "diagnosis": "Your preliminary diagnosis explanation",
          "confidence": 0.7,
          "reasoning": "Brief explanation of your reasoning"
        }}
        ```
        Your response should only contain the JSON structure.
        """

        response_text = "" # Initialize for error logging
        try:
            # result = await self.ai_agent.run(prompt)
            result = await self._run_ai_agent_with_retry(prompt)
            response_text = result.output

            # Extract JSON from the response
            json_match = re.search(r"```json\s*(.*?)\s*```", response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                json_str = response_text

            # Try to parse directly first
            try:
                ai_response = json.loads(json_str)
            except json.JSONDecodeError:
                # If fails, clean the JSON
                cleaned_json = self._clean_malformed_json(json_str)
                logger.debug(f"Cleaned JSON for preliminary diagnosis: {cleaned_json}")
                ai_response = json.loads(cleaned_json)

            diagnosis.preliminary_diagnosis = ai_response.get(
                "diagnosis", "AI analysis inconclusive"
            )
            diagnosis.preliminary_confidence = float(ai_response.get("confidence", 0.1))
            diagnosis.diagnosis_reasoning = ai_response.get(
                "reasoning", "AI generated preliminary diagnosis."
            )

        except RuntimeError as e: # Catch RuntimeError from _run_ai_agent_with_retry if agent is None after all retries
            logger.error(f"AI preliminary diagnosis failed after retries: {e}. Response: {response_text}")
            diagnosis.preliminary_diagnosis = "AI analysis failed after retries"
            diagnosis.preliminary_confidence = 0.1
            diagnosis.diagnosis_reasoning = f"AI agent call failed: {e}"
        except Exception as e: # Catch other unexpected errors during AI call or processing
            logger.error(f"Unexpected error during AI preliminary diagnosis: {e}. Response: {response_text}")
            diagnosis.preliminary_diagnosis = "AI analysis encountered an unexpected error"
            diagnosis.preliminary_confidence = 0.1
            diagnosis.diagnosis_reasoning = f"Unexpected AI error: {e}"

        return diagnosis

    async def _create_diagnostic_plan(
        self, diagnosis: DiagnosisEntry, anomaly_record: AnomalyRecord
    ) -> DiagnosticPlan:
        """Determines and creates a plan of diagnostic tools to run."""
        entity_type = anomaly_record.entity_type.lower()
        entity_id = anomaly_record.entity_id
        namespace = anomaly_record.namespace or "default"
        name = anomaly_record.name or (
            entity_id.split("/")[-1] if "/" in entity_id else entity_id
        )

        tools_config = []

        # Common: Event history
        tools_config.append(
            {
                "tool": DiagnosticTool.EVENT_HISTORY,
                "parameters": {
                    "namespace": namespace,
                    "name": name,
                    "entity_type": entity_type,
                    "limit": 20,
                },
            }
        )

        # Entity-specific tools
        if entity_type == "pod":
            tools_config.extend(
                [
                    {
                        "tool": DiagnosticTool.POD_LOGS,
                        "parameters": {
                            "namespace": namespace,
                            "name": name,
                            "tail_lines": 200,
                        },
                    },
                    {
                        "tool": DiagnosticTool.POD_DESCRIBE,
                        "parameters": {"namespace": namespace, "name": name},
                    },
                    {
                        "tool": DiagnosticTool.RESOURCE_USAGE,
                        "parameters": {
                            "namespace": namespace,
                            "name": name,
                            "entity_type": "pod",
                        },
                    },
                ]
            )
            # Add PVC check if preliminary diagnosis suggests storage issues
            if (
                "storage" in (diagnosis.preliminary_diagnosis or "").lower()
                or "volume" in (diagnosis.preliminary_diagnosis or "").lower()
            ):
                tools_config.append(
                    {
                        "tool": DiagnosticTool.PVC_STATUS,
                        "parameters": {"namespace": namespace, "name": name},
                    }
                )

        elif entity_type == "node":
            tools_config.extend(
                [
                    {
                        "tool": DiagnosticTool.NODE_DESCRIBE,
                        "parameters": {"name": name},
                    },
                    {
                        "tool": DiagnosticTool.NODE_CONDITIONS,
                        "parameters": {"name": name},
                    },
                    {
                        "tool": DiagnosticTool.RESOURCE_USAGE,
                        "parameters": {"name": name, "entity_type": "node"},
                    },
                ]
            )

        elif entity_type in ["deployment", "statefulset", "daemonset"]:
            describe_tool = {
                "deployment": DiagnosticTool.DEPLOYMENT_DESCRIBE,
                "statefulset": DiagnosticTool.STATEFULSET_DESCRIBE,
                # Add DaemonSet describe if implemented
            }.get(entity_type)

            if describe_tool:
                tools_config.append(
                    {
                        "tool": describe_tool,
                        "parameters": {"namespace": namespace, "name": name},
                    }
                )
            tools_config.append(
                {
                    "tool": DiagnosticTool.RESOURCE_USAGE,
                    "parameters": {
                        "namespace": namespace,
                        "name": name,
                        "entity_type": entity_type,
                    },
                }
            )

        plan = DiagnosticPlan(
            anomaly_id=anomaly_record.id,
            diagnosis_id=diagnosis.id,
            entity_id=entity_id,
            entity_type=entity_type,
            namespace=namespace,
            status=DiagnosticPlanStatus.PENDING,
            diagnostic_tools=tools_config,
            priority=1,
            timeout_seconds=settings.diagnostic_tool_timeout_seconds
            * len(tools_config),  # Overall plan timeout
        )

        result = await self._diagnostic_plans_collection.insert_one(
            plan.model_dump(mode="json")
        )
        plan.id = str(result.inserted_id)

        # Link the plan ID back to the diagnosis entry
        diagnosis_id = (
            ObjectId(diagnosis.id) if isinstance(diagnosis.id, str) else diagnosis.id
        )
        await self._diagnosis_collection.update_one(
            {"_id": diagnosis_id}, {"$set": {"diagnostic_plan_id": plan.id}}
        )
        logger.info(
            f"Created diagnostic plan {plan.id} with {len(tools_config)} tools for diagnosis {diagnosis.id}"
        )

        return plan

    async def _execute_diagnostic_plan(
        self, diagnosis: DiagnosisEntry, anomaly_record: AnomalyRecord
    ) -> Tuple[List[DiagnosticResult], List[DiagnosticFinding]]:
        """Executes the diagnostic tools defined in the plan."""
        plan_id = diagnosis.diagnostic_plan_id
        if not plan_id:
            plan = await self._create_diagnostic_plan(diagnosis, anomaly_record)
            plan_id = plan.id
        else:
            plan_doc = await self._diagnostic_plans_collection.find_one(
                {"_id": ObjectId(plan_id)}
            )
            if not plan_doc:
                logger.error(
                    f"Diagnostic plan {plan_id} not found for diagnosis {diagnosis.id}. Creating a new one."
                )
                plan = await self._create_diagnostic_plan(diagnosis, anomaly_record)
                plan_id = plan.id
            else:
                plan = DiagnosticPlan.model_validate(plan_doc)

        await self._diagnostic_plans_collection.update_one(
            {"_id": ObjectId(plan_id)},
            {"$set": {"status": DiagnosticPlanStatus.COMPLETED.value}},
        )

        results: List[DiagnosticResult] = []
        tool_tasks = []

        # Create tasks for each tool execution
        for tool_config in plan.diagnostic_tools:
            tool_type = tool_config.get("tool")
            parameters = tool_config.get("parameters", {})
            if not tool_type:
                logger.warning(
                    f"Skipping tool config with missing 'tool' field in plan {plan_id}: {tool_config}"
                )
                continue
            tool_tasks.append(
                self._execute_single_diagnostic_tool(plan, tool_type, parameters)
            )

        # Run tools concurrently with timeout
        try:
            completed_results = await asyncio.gather(
                *tool_tasks, return_exceptions=True
            )
        except asyncio.TimeoutError:
            logger.error(
                f"Diagnostic plan {plan_id} timed out after {plan.timeout_seconds} seconds."
            )
            # Handle timeout, potentially marking remaining tasks as failed.
            # For now, we just process the results that completed.
            completed_results = []  # Or gather results that did complete

        # Process results (including potential exceptions)
        successful_results_ids = []
        all_result_ids = []
        for result_or_exc in completed_results:
            if isinstance(result_or_exc, DiagnosticResult):
                results.append(result_or_exc)
                if result_or_exc.id:  # Ensure ID exists before appending
                    all_result_ids.append(result_or_exc.id)
                    if result_or_exc.success:
                        successful_results_ids.append(result_or_exc.id)
            elif isinstance(result_or_exc, Exception):
                # Log the error, but it's already captured in the DiagnosticResult if execution failed
                logger.error(
                    f"An unexpected error occurred during tool execution gather: {result_or_exc}"
                )
            else:
                logger.warning(
                    f"Unexpected item in gather results: {type(result_or_exc)}"
                )

        # Analyze results to generate findings
        findings = await self._analyze_diagnostic_results(
            results, diagnosis, anomaly_record
        )

        # Ensure we're working with string IDs
        finding_ids = [str(f.id) for f in findings if f.id]  # Filter out None IDs

        # Log the finding IDs for debugging
        logger.debug(f"Finding IDs to be stored: {finding_ids}")

        # Final plan update
        await self._diagnostic_plans_collection.update_one(
            {"_id": ObjectId(plan_id)},
            {
                "$set": {
                    "results": all_result_ids,
                    "findings": finding_ids,  # Ensure these are strings
                    "status": DiagnosticPlanStatus.COMPLETED.value,
                    "completed_at": datetime.datetime.now(datetime.timezone.utc),
                }
            },
        )

        # Update diagnosis with findings - ensure we're using the correct ID format
        diagnosis_id = (
            ObjectId(diagnosis.id) if isinstance(diagnosis.id, str) else diagnosis.id
        )

        logger.debug(f"Updating diagnosis {diagnosis_id} with findings: {finding_ids}")

        update_result = await self._diagnosis_collection.update_one(
            {"_id": diagnosis_id},
            {"$set": {"findings": finding_ids}},  # Ensure these are strings
        )

        logger.debug(
            f"Diagnosis update result: matched={update_result.matched_count}, modified={update_result.modified_count}"
        )

        logger.info(
            f"Completed diagnostic plan {plan_id} execution: {len(results)} results, {len(findings)} findings."
        )
        return results, findings

    async def _execute_single_diagnostic_tool(
        self,
        plan: DiagnosticPlan,
        tool_type: DiagnosticTool,
        parameters: Dict[str, Any],
    ) -> DiagnosticResult:
        """Executes a single diagnostic tool and records the result."""
        result = DiagnosticResult(
            plan_id=plan.id,
            diagnosis_id=plan.diagnosis_id,
            anomaly_id=plan.anomaly_id,
            tool=tool_type,
            parameters=parameters,
            start_time=datetime.datetime.now(datetime.timezone.utc),
            end_time=datetime.datetime.now(datetime.timezone.utc),
            duration_seconds=0.0,
            result_data={},
            success=False,
            summary="",
            error_message="",
        )
        start_time = time.monotonic()

        try:
            # Map tool enum to execution method
            execution_method = {
                DiagnosticTool.POD_LOGS: self._execute_pod_logs_diagnostic,
                DiagnosticTool.POD_DESCRIBE: self._execute_pod_describe_diagnostic,
                DiagnosticTool.NODE_DESCRIBE: self._execute_node_describe_diagnostic,
                DiagnosticTool.DEPLOYMENT_DESCRIBE: self._execute_deployment_describe_diagnostic,
                DiagnosticTool.STATEFULSET_DESCRIBE: self._execute_statefulset_describe_diagnostic,
                DiagnosticTool.RESOURCE_USAGE: self._execute_resource_usage_diagnostic,
                DiagnosticTool.EVENT_HISTORY: self._execute_event_history_diagnostic,
                DiagnosticTool.NODE_CONDITIONS: self._execute_node_conditions_diagnostic,
                DiagnosticTool.PVC_STATUS: self._execute_pvc_status_diagnostic,
            }.get(tool_type)

            if execution_method:
                logger.debug(
                    f"Executing diagnostic tool {tool_type} with parameters: {parameters}"
                )
                # Apply timeout per tool
                result.result_data = await asyncio.wait_for(
                    execution_method(parameters),
                    timeout=settings.diagnostic_tool_timeout_seconds,
                )
                result.success = True
                logger.debug(
                    f"Diagnostic tool {tool_type} executed successfully. Result data keys: {list(result.result_data.keys()) if isinstance(result.result_data, dict) else 'N/A'}"
                )
            else:
                raise NotImplementedError(
                    f"Diagnostic tool '{tool_type}' is not implemented."
                )

        except asyncio.TimeoutError:
            logger.warning(f"Diagnostic tool {tool_type} timed out for plan {plan.id}.")
            result.success = False
            result.error_message = "Tool execution timed out."
        except Exception as e:
            logger.exception(
                f"Error executing diagnostic tool {tool_type} for plan {plan.id}: {e}"
            )
            result.success = False
            result.error_message = f"Execution error: {str(e)}"
        finally:
            result.duration_seconds = time.monotonic() - start_time

        # Store result in DB and get its ID
        insert_result = await self._diagnostic_results_collection.insert_one(
            result.model_dump(mode="json")
        )
        result.id = str(insert_result.inserted_id)

        return result

    # --- Diagnostic Tool Execution Implementations ---

    async def _execute_pod_logs_diagnostic(self, params: Dict) -> Dict:
        return await self.diagnostic_tools.get_pod_logs(**params)

    async def _execute_pod_describe_diagnostic(self, params: Dict) -> Dict:
        return await self.diagnostic_tools.describe_pod(**params)

    async def _execute_node_describe_diagnostic(self, params: Dict) -> Dict:
        return await self.diagnostic_tools.describe_node(**params)

    async def _execute_deployment_describe_diagnostic(self, params: Dict) -> Dict:
        return await self.diagnostic_tools.describe_deployment(**params)

    async def _execute_statefulset_describe_diagnostic(self, params: Dict) -> Dict:
        return await self.diagnostic_tools.describe_statefulset(**params)

    async def _execute_resource_usage_diagnostic(self, params: Dict) -> Dict:
        # Assuming diagnostic_tools.get_resource_usage exists and handles filtering
        return await self.diagnostic_tools.get_resource_usage(**params)

    async def _execute_event_history_diagnostic(self, params: Dict) -> Dict:
        return await self.diagnostic_tools.get_event_history(**params)

    async def _execute_node_conditions_diagnostic(self, params: Dict) -> Dict:
        return await self.diagnostic_tools.get_node_conditions(**params)

    async def _execute_pvc_status_diagnostic(self, params: Dict) -> Dict:
        return await self.diagnostic_tools.get_pvc_status(**params)

    # --- Result Analysis and Validation ---

    async def _analyze_diagnostic_results(
        self,
        results: List[DiagnosticResult],
        diagnosis: DiagnosisEntry,
        anomaly_record: AnomalyRecord,
    ) -> List[DiagnosticFinding]:
        """Analyzes diagnostic results to generate findings."""
        findings = []

        # Track data sources
        pod_logs_available = False
        pod_describe_data_available = False
        resource_usage_available = False
        
        # Additional counter for image-related issues
        image_related_issues = 0

        for result in results:
            if not result.success:
                # Record diagnostic failure as a finding
                findings.append(
                    DiagnosticFinding(
                        diagnosis_id=str(diagnosis.id), # Ensure ID is string
                        anomaly_id=str(anomaly_record.id), # Ensure ID is string
                        finding_type="diagnostic_failure",
                        severity="medium",
                        summary=f"Diagnostic tool {result.tool} failed", # Use result.tool
                        description=result.error_message or "No error details available",
                        source=str(result.tool.value), # Use result.tool.value
                        entity_id=anomaly_record.entity_id,
                        entity_type=anomaly_record.entity_type,
                    )
                )
                
                # Check for specific error patterns that might be significant
                if result.tool == DiagnosticTool.POD_LOGS and "400 Bad Request" in (result.error_message or ""):
                    # Unable to get logs - common with ImagePullBackOff pods
                    findings.append(
                        DiagnosticFinding(
                            diagnosis_id=str(diagnosis.id), 
                            anomaly_id=str(anomaly_record.id),
                            finding_type="pod_logs_unavailable",
                            severity="high",
                            summary="Pod logs cannot be retrieved, potentially due to pod not being in Running state",
                            description=f"Failure to fetch logs might indicate pod hasn't started: {result.error_message}",
                            source=str(DiagnosticTool.POD_LOGS.value),
                            entity_id=anomaly_record.entity_id,
                            entity_type=anomaly_record.entity_type,
                        )
                    )
                continue # Move to the next result if this one failed

            # If result.success is True:
            if result.tool == DiagnosticTool.POD_DESCRIBE:
                pod_describe_data_available = True
                entity_id = anomaly_record.entity_id
                entity_type = anomaly_record.entity_type

                pod_data = result.result_data.get("pod", {})
                status = pod_data.get("status", {})
                phase = status.get("phase", "")
                
                if phase != "Running":
                    findings.append(DiagnosticFinding(
                        diagnosis_id=str(diagnosis.id),
                        anomaly_id=str(anomaly_record.id),
                        entity_id=entity_id,
                        entity_type=entity_type,
                        finding_type="pod_not_running",
                        severity="high", 
                        summary=f"Pod is in {phase} phase rather than Running",
                        description=f"Pod phase {phase} indicates the pod is not fully operational",
                        source=str(DiagnosticTool.POD_DESCRIBE.value),
                        evidence=[{"pod_phase": phase}],
                    ))
                
                container_statuses = status.get("containerStatuses", [])
                for cs in container_statuses: # Renamed status to cs to avoid conflict
                    waiting_state = cs.get("state", {}).get("waiting", {})
                    waiting_reason = waiting_state.get("reason", "")
                    waiting_message = waiting_state.get("message", "")
                    
                    if waiting_reason in ["ImagePullBackOff", "ErrImagePull"]:
                        image_related_issues += 1
                        findings.append(DiagnosticFinding(
                            diagnosis_id=str(diagnosis.id),
                            anomaly_id=str(anomaly_record.id),
                            entity_id=entity_id,
                            entity_type=entity_type,
                            finding_type="image_pull_failure",
                            severity="critical",
                            summary=f"Failed to pull image for container {cs.get('name')}",
                            description=waiting_message or f"Container {cs.get('name')} has {waiting_reason}",
                            source=str(DiagnosticTool.POD_DESCRIBE.value),
                            evidence=[{"container_status": cs}],
                        ))
                    elif waiting_reason == "CrashLoopBackOff":
                        findings.append(DiagnosticFinding(
                            diagnosis_id=str(diagnosis.id),
                            anomaly_id=str(anomaly_record.id),
                            entity_id=entity_id,
                            entity_type=entity_type,
                            finding_type="crash_loop_backoff",
                            severity="critical",
                            summary=f"Container {cs.get('name')} in CrashLoopBackOff",
                            description=waiting_message or "Container is repeatedly crashing",
                            source=str(DiagnosticTool.POD_DESCRIBE.value),
                            evidence=[{"container_status": cs}],
                        ))
                    elif waiting_reason:
                        findings.append(DiagnosticFinding(
                            diagnosis_id=str(diagnosis.id),
                            anomaly_id=str(anomaly_record.id),
                            entity_id=entity_id,
                            entity_type=entity_type,
                            finding_type=f"container_waiting_{waiting_reason.lower()}",
                            severity="high",
                            summary=f"Container {cs.get('name')} is waiting: {waiting_reason}",
                            description=waiting_message or f"Container is in waiting state with reason: {waiting_reason}",
                            source=str(DiagnosticTool.POD_DESCRIBE.value),
                            evidence=[{"container_status": cs}],
                        ))
                
                annotations = pod_data.get("metadata", {}).get("annotations", {})
                if "cluster_autoscaler_unhelpable_since" in str(annotations):
                    findings.append(DiagnosticFinding(
                        diagnosis_id=str(diagnosis.id),
                        anomaly_id=str(anomaly_record.id),
                        entity_id=entity_id,
                        entity_type=entity_type, 
                        finding_type="cluster_autoscaling_issue",
                        severity="high",
                        summary="Pod marked as unhelpable by cluster autoscaler",
                        description="The cluster autoscaler cannot schedule this pod, which may indicate resource constraints",
                        source=str(DiagnosticTool.POD_DESCRIBE.value),
                        evidence=[{"annotations": annotations}],
                    ))
                
            elif result.tool == DiagnosticTool.POD_LOGS:
                pod_logs_available = True
                # Simplified: check if logs exist. Real analysis would be more complex.
                if result.success and "logs" in result.result_data and result.result_data["logs"]:
                    # Basic check for common error patterns
                    log_content = str(result.result_data["logs"]).lower()
                    if "error" in log_content or "exception" in log_content:
                        findings.append(DiagnosticFinding(
                            diagnosis_id=str(diagnosis.id),
                            anomaly_id=str(anomaly_record.id),
                            entity_id=anomaly_record.entity_id,
                            entity_type=anomaly_record.entity_type,
                            finding_type="error_in_logs",
                            severity="medium",
                            summary="Potential errors found in pod logs",
                            description="Pod logs contain 'error' or 'exception', indicating possible issues.",
                            source=str(DiagnosticTool.POD_LOGS.value),
                            evidence=[{"log_excerpt": log_content[:500]}] 
                        ))
                elif result.success and not result.result_data.get("logs"):
                     findings.append(DiagnosticFinding(
                        diagnosis_id=str(diagnosis.id),
                        anomaly_id=str(anomaly_record.id),
                        entity_id=anomaly_record.entity_id,
                        entity_type=anomaly_record.entity_type,
                        finding_type="empty_pod_logs",
                        severity="low",
                        summary="Pod logs were retrieved but are empty.",
                        description="Empty logs might be normal for some applications or could indicate an issue.",
                        source=str(DiagnosticTool.POD_LOGS.value),
                    ))

            elif result.tool == DiagnosticTool.RESOURCE_USAGE:
                resource_usage_available = True
                if result.success:
                    pod_metrics = result.result_data.get("pod_metrics", {}).get("items", [])
                    if not pod_metrics and anomaly_record.entity_type.lower() == "pod":
                        findings.append(DiagnosticFinding(
                            diagnosis_id=str(diagnosis.id),
                            anomaly_id=str(anomaly_record.id),
                            entity_id=anomaly_record.entity_id,
                            entity_type=anomaly_record.entity_type,
                            finding_type="no_metrics_available",
                            severity="medium",
                            summary="No resource metrics available for pod",
                            description="Pod may not be running or metrics collection is failing",
                            source=str(DiagnosticTool.RESOURCE_USAGE.value),
                        ))
                    # Add more detailed metric analysis here if needed
                
            elif result.tool == DiagnosticTool.EVENT_HISTORY:
                if result.success and "events" in result.result_data:
                    events = result.result_data.get("events", [])
                    for event in events:
                        if event.get("type") == "Warning":
                            findings.append(DiagnosticFinding(
                                diagnosis_id=str(diagnosis.id),
                                anomaly_id=str(anomaly_record.id),
                                entity_id=anomaly_record.entity_id, # Or event.involvedObject if more specific
                                entity_type=event.get("involvedObject", {}).get("kind", anomaly_record.entity_type),
                                finding_type="warning_event_found",
                                severity="medium", # Could be adjusted based on event reason
                                summary=f"Warning event: {event.get('reason', 'Unknown reason')}",
                                description=event.get('message', 'No message'),
                                source=str(DiagnosticTool.EVENT_HISTORY.value),
                                evidence=[event]
                            ))
                            
            elif result.tool == DiagnosticTool.NODE_DESCRIBE: # Ensure this and PVC_STATUS are handled
                if result.success and "node" in result.result_data:
                    node_info = result.result_data.get("node", {})
                    conditions = node_info.get("status", {}).get("conditions", [])
                    for cond in conditions:
                        if cond.get("type") == "Ready" and cond.get("status") != "True":
                            findings.append(DiagnosticFinding(
                                diagnosis_id=str(diagnosis.id),
                                anomaly_id=str(anomaly_record.id),
                                entity_id=node_info.get("metadata", {}).get("name", anomaly_record.entity_id),
                                entity_type="Node",
                                finding_type="node_not_ready",
                                severity="high",
                                summary=f"Node {node_info.get('metadata', {}).get('name')} is not Ready",
                                description=f"Condition {cond.get('type')} is {cond.get('status')}: {cond.get('message', '')}",
                                source=str(DiagnosticTool.NODE_DESCRIBE.value),
                                evidence=[cond]
                            ))
                        # Add checks for other conditions like MemoryPressure, DiskPressure etc.
                        elif cond.get("status") == "True" and cond.get("type") in ["MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"]:
                             findings.append(DiagnosticFinding(
                                diagnosis_id=str(diagnosis.id),
                                anomaly_id=str(anomaly_record.id),
                                entity_id=node_info.get("metadata", {}).get("name", anomaly_record.entity_id),
                                entity_type="Node",
                                finding_type=f"node_{cond.get('type').lower()}",
                                severity="medium",
                                summary=f"Node {node_info.get('metadata', {}).get('name')} has condition {cond.get('type')}",
                                description=cond.get('message', ''),
                                source=str(DiagnosticTool.NODE_DESCRIBE.value),
                                evidence=[cond]
                            ))

            elif result.tool == DiagnosticTool.PVC_STATUS:
                if result.success and "pvc" in result.result_data: # Assuming "pvc" is the key for PVC info
                    pvc_info = result.result_data.get("pvc", {})
                    pvc_phase = pvc_info.get("status", {}).get("phase")
                    if pvc_phase and pvc_phase != "Bound":
                        findings.append(DiagnosticFinding(
                            diagnosis_id=str(diagnosis.id),
                            anomaly_id=str(anomaly_record.id),
                            entity_id=pvc_info.get("metadata", {}).get("name", anomaly_record.entity_id),
                            entity_type="PersistentVolumeClaim",
                            finding_type="pvc_not_bound",
                            severity="high",
                            summary=f"PVC {pvc_info.get('metadata', {}).get('name')} is not Bound (Phase: {pvc_phase})",
                            description=f"PVC phase {pvc_phase} indicates an issue.",
                            source=str(DiagnosticTool.PVC_STATUS.value),
                            evidence=[pvc_info.get("status", {})]
                        ))
            # Note: The 'finding' variable is not used after assignment in the original problematic edit.
            # All appends should be direct `findings.append(DiagnosticFinding(...))`
        
        # Add a meta-finding if we detect multiple indicators of image issues
        if image_related_issues > 0 and not pod_logs_available:
            findings.append(DiagnosticFinding( # Directly append
                diagnosis_id=str(diagnosis.id), 
                anomaly_id=str(anomaly_record.id),
                entity_id=anomaly_record.entity_id,
                entity_type=anomaly_record.entity_type,
                finding_type="imagecrashloop_detected",
                severity="critical",
                summary="Pod is experiencing image-related crash issues",
                description=f"Detected {image_related_issues} image-related issues and unable to retrieve logs, which is consistent with ImagePullBackOff or similar pod startup problems",
                source="meta_analysis",
            ))
        
        return findings

    async def _ai_analyze_diagnostic_results(
        self,
        results: List[DiagnosticResult],
        diagnosis: DiagnosisEntry,
        anomaly_record: AnomalyRecord,
    ) -> List[DiagnosticFinding]:
        """
        Analyze diagnostic results using an AI agent to extract findings.
        """
        response = None  # Initialize response here
        try:
            if not self.ai_agent:
                logger.warning("AI agent not configured. Cannot perform AI analysis.")
                return []

            # Prepare context, summarizing large data fields
            context = {
                "entity_type": anomaly_record.entity_type,
                "entity_id": anomaly_record.entity_id,
                "preliminary_diagnosis": diagnosis.preliminary_diagnosis,
                "anomaly_data": anomaly_record.model_dump(mode="json", exclude_none=True),
                "diagnostic_results": [
                    {
                        "tool": result.tool,
                        "success": result.success,
                        "error_message": result.error_message,
                        **self._summarize_result_data(result),
                    }
                    for result in results
                ],
            }

            # Create the prompt for the AI agent
            prompt = f"""
            Analyze the diagnostic results from Kubernetes tools and identify significant findings that could explain the anomaly.
            
            Context:
            - Entity type: {context['entity_type']}
            - Entity ID: {context['entity_id']}
            - Preliminary diagnosis: {context['preliminary_diagnosis']}
            - Anomaly data: {json.dumps(context['anomaly_data'], indent=2)}
            - Diagnostic results: {json.dumps(context['diagnostic_results'], indent=2)}
            
            For each finding, provide:
            1. The type of finding (e.g., high_memory, crash_loop, network_issue)
            2. A severity level (critical, high, medium, low, or info)
            3. A concise summary (one sentence)
            4. Detailed explanation with technical context
            5. The diagnostic tool source
            
            Format your response as a list of findings in JSON format:
            ```json
            [
              {{
                "finding_type": "crash_loop_backoff",
                "severity": "critical",
                "summary": "Container is repeatedly crashing with exit code 1",
                "details": "The pod's main container has restarted 7 times in the last hour with exit code 1, indicating an application error.",
                "source": "pod_describe"
              }},
              ...
            ]
            ```
            
            Focus on patterns and anomalies that could explain the reported issue. Prioritize findings based on:
            1. Severity and impact on service availability
            2. Correlation with the reported anomaly time
            3. Unusual metrics or log patterns
            
            Return at least 1 finding if any issues are present, or a "no_issues_found" finding if everything appears normal.
            """

            # Set up parameters for AI agent call
            run_params = {}

            # Only add model if ai_model_inference is set
            if hasattr(settings, "ai_model_inference") and settings.ai_model_inference:
                run_params["model"] = settings.ai_model_inference

            # Add temperature and max_tokens if available
            if hasattr(settings, "ai_temperature"):
                run_params["temperature"] = settings.ai_temperature

            if hasattr(settings, "ai_max_tokens"):
                run_params["max_tokens"] = settings.ai_max_tokens

            # Use the run method with dynamic parameters
            result = await self._run_ai_agent_with_retry(prompt, **run_params)

            response = result.output
            logger.debug(f"AI analysis response: {response}")

            # Extract JSON from the response
            raw_findings = await self._extract_json(response)

            # Convert the raw findings to DiagnosticFinding objects
            findings = []
            for raw in raw_findings:
                try:
                    # Validate each finding schema
                    finding = DiagnosticFinding(
                        # Required fields from model:
                        diagnosis_id=diagnosis.id,
                        anomaly_id=anomaly_record.id,
                        finding_type=raw.get("finding_type", "unknown"),
                        description=raw.get(
                            "details", "No details"
                        ),  # Map AI 'details' to 'description'
                        evidence=[
                            {"ai_generated": True, "raw_finding": raw}
                        ],  # Populate evidence as List[Dict]
                        # Fields from model, mapped from AI output:
                        summary=raw.get("summary", "No summary"),
                        severity=raw.get("severity", "info"),
                        source=raw.get("source", "ai_analysis"),
                        # Fields from model with default values or derived:
                        id=str(ObjectId()),
                        entity_id=anomaly_record.entity_id,
                        entity_type=anomaly_record.entity_type.lower(),
                        confidence=raw.get(
                            "confidence", 0.0
                        ),  # Assuming AI might provide confidence, default to 0.0
                        related_entities=raw.get(
                            "related_entities", []
                        ),  # Assuming AI might provide related_entities, default to empty list
                        source_result_ids=[],  # This is tricky, how to link AI findings to specific tool results? For now, leave empty.
                        detected_at=datetime.datetime.now(datetime.timezone.utc),
                        is_root_cause=False,  # AI might suggest root cause, but let validation determine this
                        remediation_hints=raw.get(
                            "remediation_hints", []
                        ),  # Assuming AI might provide hints, default to empty list
                    )
                    findings.append(finding)
                except ValidationError as e:
                    logger.error(
                        f"Invalid finding schema from AI: {e}. Raw data: {raw}"
                    )  # Log raw data on validation error
                    continue

            return findings
        except RuntimeError as e: # Catch RuntimeError from _run_ai_agent_with_retry
            logger.error(f"AI diagnostic analysis failed after retries: {e}. Response: {response if isinstance(response, str) else 'N/A'}")
            return []
        except Exception as e:
            # Log the raw response if it's available and was a string
            raw_response_excerpt = ""
            if response and isinstance(response, str): # Check if response is not None
                raw_response_excerpt = response[:500] + ("..." if len(response) > 500 else "")

            logger.error(
                f"Error during AI diagnostic analysis: {e}. Raw response excerpt: {raw_response_excerpt}"
            )  # Catch and log other exceptions
            return []

    async def _extract_json(self, text: Union[str, Dict, List]) -> List[Dict[str, Any]]:
        """Extract JSON from potential markdown, code blocks, or plain text."""
        if not text:
            return []

        # If already parsed as a list of dicts, return directly
        if isinstance(text, list) and all(isinstance(item, dict) for item in text):
            return text

        # If already parsed as a dict, wrap in list
        if isinstance(text, dict):
            return [text]

        # Convert to string if not already
        if not isinstance(text, str):
            text = str(text)

        logger.debug(f"Extracting JSON from text: {text[:100]}...")

        # Try various extraction patterns

        # 1. Look for JSON array/object in a code block
        matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, re.MULTILINE)
        for match in matches:
            try:
                logger.debug(f"Found code block match of length {len(match)}")
                # Try parsing directly first
                result = json.loads(match)
                if isinstance(result, list):
                    logger.debug(
                        f"Successfully extracted JSON array from code block with {len(result)} items"
                    )
                    return result
                elif isinstance(result, dict):
                    logger.debug("Successfully extracted JSON object from code block")
                    return [result]
            except json.JSONDecodeError as e:
                logger.debug(f"Failed to parse code block JSON directly: {e}")
                try:
                    # If direct parse fails, try cleaning and parsing
                    json_content = self._clean_malformed_json(match)
                    result = json.loads(json_content)
                    if isinstance(result, list):
                        logger.debug(
                            f"Successfully extracted JSON array from cleaned code block with {len(result)} items"
                        )
                        return result
                    elif isinstance(result, dict):
                        logger.debug(
                            "Successfully extracted JSON object from cleaned code block"
                        )
                        return [result]
                except json.JSONDecodeError as clean_e:
                    logger.debug(f"Failed to parse cleaned code block JSON: {clean_e}")
                    continue  # Try next match

        # 2. Try to find JSON without code block markers
        try:
            # Try the whole text directly
            result = json.loads(text)
            if isinstance(result, list):
                logger.debug(
                    f"Successfully extracted JSON array from full text with {len(result)} items"
                )
                return result
            elif isinstance(result, dict):
                logger.debug("Successfully extracted JSON object from full text")
                return [result]
        except json.JSONDecodeError as e:
            logger.debug(f"Failed to parse full text as JSON directly: {e}")
            try:
                # If direct parse fails, try cleaning and parsing
                json_content = self._clean_malformed_json(text)
                result = json.loads(json_content)
                if isinstance(result, list):
                    logger.debug(
                        f"Successfully extracted JSON array from cleaned full text with {len(result)} items"
                    )
                    return result
                elif isinstance(result, dict):
                    logger.debug(
                        "Successfully extracted JSON object from cleaned full text"
                    )
                    return [result]
            except json.JSONDecodeError as clean_e:
                logger.debug(f"Failed to parse cleaned full text as JSON: {clean_e}")

        # If everything fails before manual extraction, try to manually extract finding patterns
        logger.warning(
            "All JSON extraction attempts failed, trying manual pattern extraction"
        )
        try:
            findings = []
            # Look for finding_type patterns
            finding_types = re.findall(r'"finding_type":\s*"([^"]+)"', text)
            severities = re.findall(r'"severity":\s*"([^"]+)"', text)
            summaries = re.findall(r'"summary":\s*"([^"]+)"', text)
            details = re.findall(r'"details":\s*"([^"]+)"', text)
            sources = re.findall(r'"source":\s*"([^"]+)"', text)

            # Create as many findings as we can from the matched patterns
            for i in range(min(len(finding_types), len(severities), len(summaries))):
                finding = {
                    "finding_type": finding_types[i]
                    if i < len(finding_types)
                    else "unknown",
                    "severity": severities[i] if i < len(severities) else "info",
                    "summary": summaries[i]
                    if i < len(summaries)
                    else "Unknown finding",
                    "details": details[i]
                    if i < len(details)
                    else "No details available",
                    "source": sources[i] if i < len(sources) else "ai_analysis",
                }
                findings.append(finding)

            if findings:
                logger.debug(
                    f"Extracted {len(findings)} findings using pattern matching"
                )
                return findings
        except Exception as e:
            logger.error(f"Manual pattern extraction failed: {e}")

        # If everything fails, return empty list
        logger.error(f"All JSON extraction methods failed for text: {text[:100]}...")
        return []

    def _clean_malformed_json(self, json_str: str) -> str:
        """Clean potentially malformed JSON to make it valid."""
        # Log original JSON for debugging
        logger.debug(f"Cleaning potentially malformed JSON: {json_str[:100]}...")

        cleaned_str = json_str.strip()

        # Replace single quotes with double quotes for keys and string values
        cleaned_str = re.sub(r"'([^']+)':", r'"\1":', cleaned_str)
        cleaned_str = re.sub(r":\s*\'([^\']+)\'", r': "\1"', cleaned_str)

        # Add double quotes around unquoted keys (words followed by a colon)
        # This pattern looks for a word preceded by '{' or ',' and optional whitespace, followed by ':'
        # It's not perfect but covers common cases of malformed JSON from LLMs.
        cleaned_str = re.sub(r"([{,]\s*)(\w+)\s*:", r'\1"\2":', cleaned_str)

        # Fix trailing commas in arrays and objects
        cleaned_str = re.sub(r",\s*}", "}", cleaned_str)
        cleaned_str = re.sub(r",\s*\]", "]", cleaned_str)

        # Remove control and non-printable characters
        cleaned_str = re.sub(r"[\x00-\x1F\x7F-\x9F]", "", cleaned_str)

        # Attempt to parse and log success/failure
        try:
            json.loads(cleaned_str)
            logger.debug("Successfully cleaned and validated JSON")
        except json.JSONDecodeError as e:
            logger.debug(f"Could not fully clean JSON: {e}")

        return cleaned_str

    def _summarize_result_data(self, result: DiagnosticResult) -> Dict[str, Any]:
        """Summarize the result data for AI analysis."""
        if result.success:
            return {"data_summary": self._summarize_data_for_ai(result.result_data)}
        else:
            return {"error_message": result.error_message}

    def _summarize_data_for_ai(self, data: Any, max_len: int = 1000) -> str:
        """Crude summarization of diagnostic data to fit AI context limits."""
        try:
            # Attempt full JSON serialization first
            json_str = json.dumps(data, default=str)
            if len(json_str) <= max_len:
                return json_str

            # If too long, try summarizing specific known large fields
            if isinstance(data, dict):
                summary_dict = {}
                for k, v in data.items():
                    if k == "logs" and isinstance(v, str):
                        lines = v.splitlines()
                        # Prioritize error/warning lines
                        err_lines = [
                            line
                            for line in lines
                            if any(
                                e in line.lower()
                                for e in ["error", "warn", "fail", "except", "panic"]
                            )
                        ]
                        summary_dict[k] = "\n".join(
                            err_lines[:10] + lines[-10:]
                        )  # First/last error/warn lines + last few lines
                    elif k == "events" and isinstance(v, list):
                        summary_dict[k] = (
                            f"{len(v)} events found. Recent: {json.dumps(v[:3], default=str)}"  # First few events
                        )
                    elif isinstance(v, (dict, list)):
                        # Recursively summarize, but limit depth or total length
                        summary_dict[k] = self._summarize_data_for_ai(
                            v, max_len // 2
                        )  # Rough split
                    else:
                        summary_dict[k] = v
                json_str = json.dumps(summary_dict, default=str)

            # Final truncation if still too long
            return (
                (json_str[: max_len - 3] + "...")
                if len(json_str) > max_len
                else json_str
            )

        except Exception as e:
            logger.warning(f"Failed to summarize data for AI: {e}")
            return f"Error summarizing data: {e}"

    async def _validate_diagnosis(
        self,
        diagnosis: DiagnosisEntry,
        anomaly_record: AnomalyRecord,
        findings: List[DiagnosticFinding],
    ) -> DiagnosisEntry:
        """Validates the diagnosis using findings and determines the final root cause."""
        # Set initial validation state
        diagnosis.status = DiagnosisStatus.COMPLETE
        
        # Log diagnostic findings for debugging
        findings_available = findings is not None
        findings_count = len(findings) if findings_available else 0
        logger.info(f"Validating diagnosis with {findings_count} findings")
        
        if not findings_available or not findings:
            # Before marking as inconclusive, check if we have diagnostic tool failures
            # that might actually provide insight into the problem
            diagnostic_failures = []
            pod_logs_unavailable = False
            no_metrics_available = False
            imagecrashloop_detected = False
            
            if findings_available:
                for finding in findings:
                    if finding.finding_type == "diagnostic_failure":
                        diagnostic_failures.append(finding)
                    elif finding.finding_type == "pod_logs_unavailable":
                        pod_logs_unavailable = True
                    elif finding.finding_type == "no_metrics_available":
                        no_metrics_available = True
                    elif finding.finding_type == "imagecrashloop_detected":
                        imagecrashloop_detected = True
            
            # If we have specific diagnostic failures that are actually informative,
            # use them instead of marking the diagnosis as inconclusive
            if imagecrashloop_detected or (pod_logs_unavailable and no_metrics_available):
                # This is a strong signal of an image-related issue
                diagnosis.root_cause = "Image pull or container startup failure detected"
                diagnosis.root_cause_confidence = 0.8
                diagnosis.diagnosis_summary = "Pod cannot start due to image-related issues"
                diagnosis.diagnosis_details = (
                    "Pod logs are unavailable and no metrics are reported, which is consistent "
                    "with a pod that cannot start due to image pull failures"
                )
                diagnosis.diagnosis_reasoning = (
                    "Derived from diagnostic tool failures that indicate pod cannot start properly"
                )
                diagnosis.validated = True
                return diagnosis
            
            # Standard inconclusive case when we have no useful findings at all
            logger.warning(
                f"No findings available or found for diagnosis {diagnosis.id}. Marking as inconclusive."
            )
            diagnosis.status = DiagnosisStatus.INCONCLUSIVE
            diagnosis.root_cause = "Inconclusive - No diagnostic findings"
            diagnosis.root_cause_confidence = 0.1
            diagnosis.diagnosis_summary = (
                "No diagnostic findings were available to determine a root cause"
            )
            diagnosis.diagnosis_details = (
                "Diagnostic tools did not return any findings to analyze"
            )
            diagnosis.diagnosis_reasoning = "Automated diagnosis with no findings"
            diagnosis.validated = False
        else:
            logger.info(f"Found {len(findings)} findings for validation")
            # Prefer AI validation if available and enabled
            if self.ai_agent and settings.enable_diagnosis_workflow:
                try:
                    diagnosis = await self._ai_validate_diagnosis(
                        diagnosis, anomaly_record, findings
                    )
                except Exception as e:
                    logger.warning(
                        f"AI diagnosis validation failed ({e}), falling back to rules-based."
                    )
                    diagnosis = await self._rules_based_validate_diagnosis(
                        diagnosis, anomaly_record, findings
                    )
            else:
                diagnosis = await self._rules_based_validate_diagnosis(
                    diagnosis, anomaly_record, findings
                )
            diagnosis.validated = True

        # Ensure we always have a summary before setting status to COMPLETE
        if (
            diagnosis.status == DiagnosisStatus.COMPLETE
            and not diagnosis.diagnosis_summary
        ):
            diagnosis.diagnosis_summary = (
                diagnosis.root_cause
                or "Diagnosis completed without clear summary"
            )

        # Persist the validation results
        diagnosis_id = (
            ObjectId(diagnosis.id) if isinstance(diagnosis.id, str) else diagnosis.id
        )
        await self._diagnosis_collection.update_one(
            {"_id": diagnosis_id},
            {
                "$set": {
                    "root_cause": diagnosis.root_cause,
                    "root_cause_confidence": diagnosis.root_cause_confidence,
                    "diagnosis_summary": diagnosis.diagnosis_summary,
                    "diagnosis_details": diagnosis.diagnosis_details,
                    "diagnosis_reasoning": diagnosis.diagnosis_reasoning,
                    "validated": diagnosis.validated,
                    "validation_method": diagnosis.validation_method,
                    "status": diagnosis.status.value,
                }
            },
        )
        logger.info(
            f"Validated diagnosis for anomaly {anomaly_record.id}. Root cause: '{diagnosis.root_cause}' "
            f"(Confidence: {diagnosis.root_cause_confidence:.2f}, Validated: {diagnosis.validated})"
        )
        return diagnosis

    async def _rules_based_validate_diagnosis(
        self,
        diagnosis: DiagnosisEntry,
        anomaly_record: AnomalyRecord,
        findings: List[DiagnosticFinding],
    ) -> DiagnosisEntry:
        """Validates diagnosis by prioritizing critical/high severity findings."""
        diagnosis.validation_method = "rules_based"

        critical_findings = sorted(
            [f for f in findings if f.severity == "critical"],
            key=lambda f: f.detected_at,
            reverse=True,
        )
        high_findings = sorted(
            [f for f in findings if f.severity == "high"],
            key=lambda f: f.detected_at,
            reverse=True,
        )

        primary_finding = None
        confidence = 0.5  # Base confidence

        if critical_findings:
            primary_finding = critical_findings[0]  # Most recent critical finding
            confidence = 0.8 + min(
                0.1, (len(critical_findings) - 1) * 0.05
            )  # Increase confidence slightly with more critical findings
        elif high_findings:
            primary_finding = high_findings[0]  # Most recent high finding
            confidence = 0.65 + min(0.1, (len(high_findings) - 1) * 0.05)

        if primary_finding:
            diagnosis.root_cause = (
                f"{primary_finding.finding_type}: {primary_finding.summary}"
            )
            diagnosis.root_cause_confidence = confidence
            diagnosis.diagnosis_summary = primary_finding.summary
            diagnosis.diagnosis_details = (
                primary_finding.details
                or "Details derived from rules-based finding analysis."
            )
            diagnosis.diagnosis_reasoning = f"Determined by highest severity finding ({primary_finding.severity}: {primary_finding.finding_type}) from rules-based analysis."
        else:
            # Fallback if only medium/low/info findings
            other_findings = sorted(
                findings,
                key=lambda f: {"medium": 2, "low": 1, "info": 0}.get(f.severity, 0),
                reverse=True,
            )
            diagnosis.root_cause = (
                diagnosis.preliminary_diagnosis or "Undetermined based on findings"
            )
            diagnosis.root_cause_confidence = max(
                0.3, (diagnosis.preliminary_confidence or 0.4) - 0.1
            )  # Use preliminary but reduce confidence
            diagnosis.diagnosis_summary = "No critical or high severity findings. Root cause based on preliminary assessment or is unclear."

            if other_findings:
                # Add some details from the medium/low findings if available
                diagnosis.diagnosis_details = f"Diagnostic tests found only lower severity issues: {', '.join([f.summary for f in other_findings[:3]])}..."
            else:
                diagnosis.diagnosis_details = (
                    "Diagnostic tests did not reveal high-impact issues."
                )

            diagnosis.diagnosis_reasoning = (
                "Validation based on absence of critical/high findings."
            )

        # Ensure diagnosis_summary is always set
        if not diagnosis.diagnosis_summary:
            diagnosis.diagnosis_summary = (
                diagnosis.root_cause
                or "Inconclusive diagnosis based on rules-based analysis"
            )

        return diagnosis

    async def _ai_validate_diagnosis(
        self,
        diagnosis: DiagnosisEntry,
        anomaly_record: AnomalyRecord,
        findings: List[DiagnosticFinding],
    ) -> DiagnosisEntry:
        """Validates the diagnosis using the AI agent for a more nuanced conclusion."""
        if not self.ai_agent:
            raise RuntimeError("AI agent not configured for AI diagnosis validation.")

        diagnosis.validation_method = "ai"

        # Convert findings to a simplified context format for AI
        findings_context = [
            {
                "finding_type": f.finding_type,
                "severity": f.severity,
                "summary": f.summary,
                "details": f.details,
                "source": f.source,
            }
            for f in findings
        ]

        # Create context for validation
        context = {
            "entity_type": anomaly_record.entity_type,
            "entity_id": anomaly_record.entity_id,
            "anomaly_data": anomaly_record.model_dump(mode="json", exclude_none=True),
            "preliminary_diagnosis": diagnosis.preliminary_diagnosis,
            "preliminary_confidence": diagnosis.preliminary_confidence,
            "diagnostic_findings": findings_context,
        }

        # Serialize context
        try:
            context_json = json.dumps(context, indent=2, default=str)
        except TypeError as e:
            logger.error(f"Failed to serialize validation context: {e}")
            # Fallback serialization
            context_json = json.dumps(
                {
                    "error": "Context serialization failed",
                    "preliminary_diagnosis": diagnosis.preliminary_diagnosis,
                }
            )

        prompt = f"""
        Validate and refine the diagnosis for a Kubernetes anomaly based on the provided context and findings.

        Context & Anomaly Data:
        {context_json}

        Your Tasks:
        1. Synthesize the preliminary diagnosis and all diagnostic findings.
        2. Determine the most probable root cause.
        3. Assign a final confidence score (0.0-1.0) for the root cause.
        4. Provide a concise summary and detailed explanation.
        5. Explain your reasoning, referencing specific findings.

        Respond ONLY with the following JSON structure:
        ```json
        {{
          "root_cause": "Precise root cause description",
          "confidence": 0.85,
          "summary": "Concise one-sentence summary of the validated diagnosis",
          "details": "Detailed technical explanation, linking findings to the root cause",
          "reasoning": "Explanation of how findings support the conclusion"
        }}
        ```
        Ensure the confidence score accurately reflects the certainty based on the evidence.
        """

        try:
            # Set up parameters for AI agent call
            run_params = {}

            # Only add model if ai_model_inference is set
            if hasattr(settings, "ai_model_inference") and settings.ai_model_inference:
                run_params["model"] = settings.ai_model_inference

            # Add temperature and max_tokens if available
            if hasattr(settings, "ai_temperature"):
                run_params["temperature"] = settings.ai_temperature

            if hasattr(settings, "ai_max_tokens"):
                run_params["max_tokens"] = settings.ai_max_tokens

            # Use the run method with dynamic parameters
            result = await self._run_ai_agent_with_retry(prompt, **run_params)

            response_text = result.output

            # Extract JSON from response_text
            json_match = re.search(r"```json\s*(.*?)\s*```", response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                json_str = response_text

            # Clean up potential invalid JSON by removing any unexpected text
            # 1. First try to parse directly
            try:
                ai_response = json.loads(json_str)
            except json.JSONDecodeError as e:
                # 2. If that fails, attempt to fix common issues
                logger.warning(
                    f"Initial JSON parsing failed in validation: {e}. Attempting to clean the response."
                )

                # Use our _clean_malformed_json method to fix it
                cleaned_json = self._clean_malformed_json(json_str)
                logger.debug(f"Cleaned JSON for validation: {cleaned_json}")
                ai_response = json.loads(cleaned_json)

            diagnosis.root_cause = ai_response.get(
                "root_cause", "AI validation inconclusive"
            )
            diagnosis.root_cause_confidence = float(ai_response.get("confidence", 0.1))
            diagnosis.diagnosis_summary = ai_response.get("summary", "")
            diagnosis.diagnosis_details = ai_response.get("details", "")
            diagnosis.diagnosis_reasoning = ai_response.get(
                "reasoning", "AI generated validation."
            )

            # Ensure we have a summary
            if not diagnosis.diagnosis_summary:
                diagnosis.diagnosis_summary = (
                    diagnosis.root_cause
                    or "AI validation completed without clear summary"
                )

        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(
                f"Error parsing AI validation response: {e}. Response: {response_text}"
            )
            # Fallback: Use rules-based validation result as a safety measure
            logger.warning(
                "Falling back to rules-based validation due to AI response parsing error."
            )
            diagnosis = await self._rules_based_validate_diagnosis(
                diagnosis, anomaly_record, findings
            )
            diagnosis.diagnosis_reasoning += (
                f" AI validation failed ({e})."  # Append failure reason
            )

        diagnosis.status = DiagnosisStatus.COMPLETE  # Mark as complete once validated
        diagnosis.validation_method = "ai"  # Record validation method

        return diagnosis

    async def _generate_remediation_plan(
        self, diagnosis: DiagnosisEntry, anomaly_record: AnomalyRecord
    ) -> Optional[RemediationPlan]:
        """Generates a remediation plan based on the validated diagnosis."""
        logger.info(
            f"Attempting to generate remediation plan for validated diagnosis {diagnosis.id}"
        )

        if diagnosis.status != DiagnosisStatus.COMPLETE or not diagnosis.validated:
            logger.warning(
                f"Cannot generate remediation plan: Diagnosis {diagnosis.id} is not complete or validated."
            )
            return None
        if diagnosis.root_cause_confidence < settings.diagnosis_confidence_threshold:
            logger.warning(
                f"Cannot generate remediation plan: Diagnosis confidence {diagnosis.root_cause_confidence:.2f} is below threshold {settings.diagnosis_confidence_threshold}"
            )
            return None

        planner_deps = PlannerDependencies(
            db=self.db,
            k8s_client=self.k8s_client,
            diagnostic_tools=self.diagnostic_tools,
        )

        # Use the validated diagnosis as the primary input for the planner
        # We might need to adjust how `generate_remediation_plan` uses this info.
        # For now, update the anomaly record copy passed to the planner.
        enhanced_anomaly = anomaly_record.model_copy(deep=True)
        enhanced_anomaly.failure_reason = (
            diagnosis.root_cause or "Unknown (Diagnosis Validated)"
        )

        # Create a remediation context dictionary instead of setting it directly on the model
        remediation_context = {
            "diagnosis_id": str(diagnosis.id),
            "diagnosis_summary": diagnosis.diagnosis_summary,
            "diagnosis_details": diagnosis.diagnosis_details,
            "diagnosis_confidence": diagnosis.root_cause_confidence,
        }

        try:
            # Pass the remediation context as a separate parameter
            plan = await generate_remediation_plan(
                anomaly_record=enhanced_anomaly,
                dependencies=planner_deps,
                agent=self.ai_agent,  # Pass AI agent if available for plan generation
                context=remediation_context,  # Pass the context as a parameter
            )

            if plan:
                # Store the plan and link it back
                plan.context.update(
                    remediation_context
                )  # Add diagnosis context to plan
                plan.reasoning = (
                    f"Based on diagnosis (Confidence: {diagnosis.root_cause_confidence:.2f}): {diagnosis.diagnosis_summary}. "
                    + plan.reasoning
                )
                plan.risk_assessment = (
                    f"Confidence level: {diagnosis.root_cause_confidence:.2f}. "
                    + plan.risk_assessment
                )

                plan_dict = plan.model_dump(mode="json")
                result = await self.db["remediation_plans"].insert_one(plan_dict)
                plan.id = str(result.inserted_id)
                logger.info(
                    f"Generated and stored remediation plan {plan.id} based on diagnosis {diagnosis.id}"
                )
                return plan
            else:
                logger.warning(
                    f"Remediation planner returned no plan for diagnosis {diagnosis.id}"
                )
                return None

        except Exception as e:
            logger.exception(
                f"Error during remediation plan generation for diagnosis {diagnosis.id}: {e}"
            )
            return None
