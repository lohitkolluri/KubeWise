import asyncio
import datetime
import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import motor.motor_asyncio
from bson import ObjectId
from kubernetes_asyncio import client
from loguru import logger
from pydantic import ValidationError
from pydantic_ai import Agent, RunContext
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.exceptions import UnexpectedModelBehavior

from kubewise.config import settings
from kubewise.models import (
    ActionType,
    AnomalyRecord,
    ExecutedActionRecord,
    KubernetesEvent,
    MetricPoint,
    PyObjectId,
    RemediationAction,
    RemediationPlan,
)

# Define dependencies for the planner agent
@dataclass
class PlannerDependencies:
    db: motor.motor_asyncio.AsyncIOMotorDatabase
    k8s_client: client.ApiClient


# Helper function for JSON serialization of datetime objects
def datetime_serializer(obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    # Let the default encoder handle other types or raise TypeError
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Time window to fetch recent events for context
RECENT_EVENT_WINDOW = datetime.timedelta(minutes=15)
MAX_RESOURCE_FETCH_RETRIES = 3
RESOURCE_FETCH_TIMEOUT = 10.0  # seconds

# Template RemediationPlans begin here

# Template RemediationPlans without placeholder ObjectId
CPU_HIGH_UTILIZATION_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="CPU High Utilization Scale Up",
    description="Scale up deployment to handle high CPU utilization",
    reasoning="Static plan: High CPU utilization detected. Scaling up the deployment to distribute load.",
    actions=[
        RemediationAction(
            action_type="scale_deployment",
            parameters={"name": "{resource_name}", "replicas": "{current_replicas + 1}", "namespace": "{namespace}"}
        )
    ],
    # Omit anomaly_id as it will be set when the plan is applied to an anomaly
)

CPU_SPIKE_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="CPU Spike Restart",
    description="Restart deployment to address CPU spike",
    reasoning="Static plan: CPU spike detected. Restarting the deployment to address potential memory leak or runaway process.",
    actions=[
        RemediationAction(
            action_type="restart_deployment",
            parameters={"name": "{resource_name}", "namespace": "{namespace}"}
        )
    ]
)

MEMORY_HIGH_UTILIZATION_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="Memory High Utilization Scale Up",
    description="Scale up deployment to handle high memory utilization",
    reasoning="Static plan: High memory utilization detected. Scaling up the deployment to distribute memory load.",
    actions=[
        RemediationAction(
            action_type="scale_deployment",
            parameters={"name": "{resource_name}", "replicas": "{current_replicas + 1}", "namespace": "{namespace}"}
        )
    ]
)

MEMORY_LEAK_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="Memory Leak Pod Restart",
    description="Restart pods to address potential memory leak",
    reasoning="Static plan: Potential memory leak detected. Restarting the affected pods to reclaim memory.",
    actions=[
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"}
        )
    ]
)

OOMKILLED_POD_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="OOMKilled Pod Restart",
    description="Restart pod that experienced OOMKilled event",
    reasoning="Static plan: OOMKilled event detected for pod. Deleting the pod to allow rescheduling.",
    actions=[
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"}
        )
    ]
)

HIGH_RESTART_COUNT_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="High Restart Count Pod Deletion",
    description="Delete pod with high container restart count",
    reasoning="Static plan: High container restart count detected. Pod appears to be in a crash loop - attempting targeted pod deletion.",
    actions=[
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"}
        )
    ]
)

NODE_PRESSURE_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="Node Resource Pressure Remediation",
    description="Cordon and drain node experiencing resource pressure",
    reasoning="Static plan: Node is experiencing resource pressure. Cordoning and draining the node to redistribute workloads.",
    actions=[
        RemediationAction(
            action_type="drain_node",
            parameters={"name": "{node_name}", "grace_period_seconds": 300, "force": False}
        )
    ]
)

STATEFULSET_SCALE_UP_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="StatefulSet Scale Up",
    description="Scale up statefulset experiencing high load",
    reasoning="Static plan: StatefulSet experiencing high load. Scaling up to distribute workload.",
    actions=[
        RemediationAction(
            action_type="scale_statefulset", 
            parameters={"name": "{resource_name}", "replicas": "{current_replicas + 1}", "namespace": "{namespace}"}
        )
    ]
)

DEFAULT_POD_RESTART_PLAN = RemediationPlan(
    anomaly_id=ObjectId(),
    plan_name="Generic Pod Restart",
    description="Restart pod as a generic remediation attempt",
    reasoning="Static plan: No specific pattern matched. Attempting generic pod restart as a safe first action.",
    actions=[
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"}
        )
    ]
)

# Production-ready static plan mapping without ObjectIds
STATIC_PLAN_TEMPLATES = {
    "cpu_utilization_pct": {
        "high": CPU_HIGH_UTILIZATION_PLAN,
        "spike": CPU_SPIKE_PLAN
    },
    "memory_utilization_pct": {
        "high": MEMORY_HIGH_UTILIZATION_PLAN
    },
    "leak": MEMORY_LEAK_PLAN,
    "oomkilled_event": {
        "detected": OOMKILLED_POD_PLAN
    },
    "container_restart_count": {
        "high": HIGH_RESTART_COUNT_PLAN
    },
    "node_issue": {
        "resource_pressure": NODE_PRESSURE_PLAN
    },
    "statefulset_issue": {
        "high_load": STATEFULSET_SCALE_UP_PLAN
    },
    "default": {
        "unknown": DEFAULT_POD_RESTART_PLAN
    }
}

def get_static_plan_template(pattern: str, variant: str = "default") -> Optional[RemediationPlan]:
    """
    Get a static remediation plan template based on pattern and variant.
    
    Args:
        pattern: The metric or event pattern to match
        variant: The variant of the plan to use (e.g., "high", "spike")
        
    Returns:
        A copy of the template plan, or None if no matching template exists
    """
    if pattern in STATIC_PLAN_TEMPLATES:
        template_group = STATIC_PLAN_TEMPLATES[pattern]
        
        if isinstance(template_group, dict):
            # If the pattern maps to a dict of variants
            if variant in template_group:
                return template_group[variant].model_copy(deep=True)
            elif "default" in template_group:
                return template_group["default"].model_copy(deep=True)
        else:
            # If the pattern maps directly to a template
            return template_group.model_copy(deep=True)
            
    # If no matching template, try the default/unknown plan
    if "default" in STATIC_PLAN_TEMPLATES and "unknown" in STATIC_PLAN_TEMPLATES["default"]:
        return STATIC_PLAN_TEMPLATES["default"]["unknown"].model_copy(deep=True)
        
    return None


async def get_recent_events(
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    anomaly_record: AnomalyRecord,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """
    Retrieves recent Kubernetes warning events related to the anomaly's context.
    
    Args:
        db: MongoDB database instance
        anomaly_record: The anomaly record containing entity information
        limit: Maximum number of events to return
        
    Returns:
        List of recent K8s events as dictionaries
    """
    event_collection = db["events"]  # Assuming events are stored here by the collector
    query: Dict[str, Any] = {}

    # Use entity information from the anomaly record directly
    if anomaly_record.entity_id and "/" in anomaly_record.entity_id:
        # entity_id is in format "namespace/name"
        ns, name = anomaly_record.entity_id.split("/", 1)
        
        query = {
            "involvedObjectNamespace": ns,
            "involvedObjectName": name,
        }
        
        # Add entity type if available
        if anomaly_record.entity_type:
            query["involvedObjectKind"] = anomaly_record.entity_type

    # Add time window constraint
    cutoff_time = anomaly_record.timestamp - RECENT_EVENT_WINDOW
    query["lastTimestamp"] = {"$gte": cutoff_time}
    query["type"] = "Warning"  # Focus on warnings for remediation context

    try:
        events_cursor = event_collection.find(query).sort("lastTimestamp", -1).limit(limit)
        events = await events_cursor.to_list(length=limit)
        
        # Convert ObjectId and datetime for JSON serialization in prompt
        for event in events:
            if "_id" in event:
                event["_id"] = str(event["_id"])
            if isinstance(event.get("firstTimestamp"), datetime.datetime):
                event["firstTimestamp"] = event["firstTimestamp"].isoformat()
            if isinstance(event.get("lastTimestamp"), datetime.datetime):
                event["lastTimestamp"] = event["lastTimestamp"].isoformat()
                
        logger.debug(f"Found {len(events)} recent related events for anomaly {anomaly_record.id}")
        return events
    except Exception as e:
        logger.error(f"Error fetching recent events for anomaly {anomaly_record.id}: {e}")
        return []


async def get_resource_metadata(
    api_client: client.ApiClient, anomaly_record: AnomalyRecord
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Fetch metadata about the resource that experienced the anomaly.
    
    Args:
        api_client: Kubernetes API client
        anomaly_record: The anomaly record to fetch metadata for
        
    Returns:
        Tuple of (resource metadata, owner metadata) if fetching is successful,
        otherwise (None, None)
    """
    entity_id = anomaly_record.entity_id or ""
    if not entity_id:
        logger.warning("Cannot fetch resource metadata without entity_id")
        return None, None
        
    # Parse entity_id to extract kind, namespace, name
    # Format is typically: kind/namespace/name or kind_namespace_name
    parts = entity_id.split("/")
    if len(parts) >= 3:
        # The format is kind/namespace/name
        current_kind = parts[0].lower()
        namespace = parts[1]
        name = parts[2]
    elif len(parts) == 1 and "_" in entity_id:
        # Try fallback format kind_namespace_name
        parts = entity_id.split("_")
        if len(parts) >= 3:
            current_kind = parts[0].lower()
            namespace = parts[1]
            name = "_".join(parts[2:])  # Handle names with underscores
        else:
            logger.warning(f"Unable to parse entity_id: {entity_id}")
            return None, None
    else:
        # Simple kind/name format without namespace (e.g., node/name)
        if len(parts) == 2:
            current_kind = parts[0].lower()
            namespace = None
            name = parts[1]
        else:
            logger.warning(f"Unable to parse entity_id: {entity_id}")
            return None, None
    
    resource_metadata = None
    owner_metadata = None
    
    for _ in range(MAX_RESOURCE_FETCH_RETRIES):
        try:
            # Fetch the resource based on its kind
            if current_kind == "pod":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_pod(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
                # Get owner references if available
                owner_refs = resource.metadata.owner_references
                if owner_refs and len(owner_refs) > 0:
                    owner_ref = owner_refs[0]
                    owner_kind = owner_ref.kind.lower()
                    owner_name = owner_ref.name
                    
                    # Fetch owner metadata
                    if owner_kind == "replicaset":
                        apps_v1 = client.AppsV1Api(api_client)
                        owner = await apps_v1.read_namespaced_replica_set(
                            name=owner_name, namespace=namespace
                        )
                        owner_metadata = owner.to_dict()
                        
                        # If ReplicaSet is owned by a Deployment, get that too
                        owner_refs = owner.metadata.owner_references
                        if owner_refs and len(owner_refs) > 0:
                            depl_ref = owner_refs[0]
                            if depl_ref.kind.lower() == "deployment":
                                depl = await apps_v1.read_namespaced_deployment(
                                    name=depl_ref.name, namespace=namespace
                                )
                                owner_metadata = depl.to_dict()
                    
                    elif owner_kind == "deployment":
                        apps_v1 = client.AppsV1Api(api_client)
                        owner = await apps_v1.read_namespaced_deployment(
                            name=owner_name, namespace=namespace
                        )
                        owner_metadata = owner.to_dict()
                    
                    elif owner_kind == "statefulset":
                        apps_v1 = client.AppsV1Api(api_client)
                        owner = await apps_v1.read_namespaced_stateful_set(
                            name=owner_name, namespace=namespace
                        )
                        owner_metadata = owner.to_dict()
                    
                    elif owner_kind == "daemonset":
                        apps_v1 = client.AppsV1Api(api_client)
                        owner = await apps_v1.read_namespaced_daemon_set(
                            name=owner_name, namespace=namespace
                        )
                        owner_metadata = owner.to_dict()
                    
                    elif owner_kind == "job":
                        batch_v1 = client.BatchV1Api(api_client)
                        owner = await batch_v1.read_namespaced_job(
                            name=owner_name, namespace=namespace
                        )
                        owner_metadata = owner.to_dict()
                    
                    elif owner_kind == "cronjob":
                        batch_v1 = client.BatchV1Api(api_client)
                        owner = await batch_v1.read_namespaced_cron_job(
                            name=owner_name, namespace=namespace
                        )
                        owner_metadata = owner.to_dict()
                    else:
                        logger.info(f"Owner kind '{owner_kind}' not handled explicitly for metadata fetching")
            
            elif current_kind == "deployment":
                apps_v1 = client.AppsV1Api(api_client)
                resource = await apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
            elif current_kind == "replicaset":
                apps_v1 = client.AppsV1Api(api_client)
                resource = await apps_v1.read_namespaced_replica_set(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
                # Get owner references if available (typically a Deployment)
                owner_refs = resource.metadata.owner_references
                if owner_refs and len(owner_refs) > 0:
                    owner_ref = owner_refs[0]
                    if owner_ref.kind.lower() == "deployment":
                        owner = await apps_v1.read_namespaced_deployment(
                            name=owner_ref.name, namespace=namespace
                        )
                        owner_metadata = owner.to_dict()
                
            elif current_kind == "statefulset":
                apps_v1 = client.AppsV1Api(api_client)
                resource = await apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
            elif current_kind == "daemonset":
                apps_v1 = client.AppsV1Api(api_client)
                resource = await apps_v1.read_namespaced_daemon_set(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
            elif current_kind == "node":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_node(name=name)
                resource_metadata = resource.to_dict()
                
            elif current_kind == "namespace":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespace(name=name)
                resource_metadata = resource.to_dict()
                
            elif current_kind == "service":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_service(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
            elif current_kind == "persistentvolumeclaim" or current_kind == "pvc":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_persistent_volume_claim(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()
                
            elif current_kind == "job":
                batch_v1 = client.BatchV1Api(api_client)
                resource = await batch_v1.read_namespaced_job(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
                # Get owner references if available (typically a CronJob)
                owner_refs = resource.metadata.owner_references
                if owner_refs and len(owner_refs) > 0:
                    owner_ref = owner_refs[0]
                    if owner_ref.kind.lower() == "cronjob":
                        owner = await batch_v1.read_namespaced_cron_job(
                            name=owner_ref.name, namespace=namespace
                        )
                        owner_metadata = owner.to_dict()
                
            elif current_kind == "cronjob":
                batch_v1 = client.BatchV1Api(api_client)
                resource = await batch_v1.read_namespaced_cron_job(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
            elif current_kind == "configmap":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_config_map(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
            elif current_kind == "secret":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_secret(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                # Remove actual secret data for security
                if resource_metadata and 'data' in resource_metadata:
                    resource_metadata['data'] = {
                        k: f"<{len(v) if v else 0} bytes>" for k, v in resource_metadata['data'].items()
                    }
                
            elif current_kind == "hpa" or current_kind == "horizontalpodautoscaler":
                autoscaling_v1 = client.AutoscalingV1Api(api_client)
                resource = await autoscaling_v1.read_namespaced_horizontal_pod_autoscaler(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()
                
            elif current_kind == "ingress":
                networking_v1 = client.NetworkingV1Api(api_client)
                resource = await networking_v1.read_namespaced_ingress(name=name, namespace=namespace)
                resource_metadata = resource.to_dict()
                
            else:
                logger.info(f"Fetching resource kind '{current_kind}' using generic approach")
                # Generic approach using dynamic client for other resource types
                resource_metadata = {
                    "kind": current_kind.capitalize(),
                    "metadata": {
                        "name": name,
                        "namespace": namespace
                    },
                    "note": f"Limited metadata for {current_kind} - use specific API for full details"
                }
            
            # Successfully fetched, break retry loop
            break
            
        except client.rest.ApiException as e:
            if e.status == 404:
                logger.warning(f"Resource not found: {current_kind}/{namespace}/{name}")
                return None, None
            elif e.status == 403:
                logger.warning(f"Permission denied fetching {current_kind}/{namespace}/{name}")
                return None, None
            else:
                logger.warning(f"API error fetching {current_kind}/{namespace}/{name}: {e.status} - {e.reason}")
                await asyncio.sleep(1.0)  # Brief backoff before retry
        except Exception as e:
            logger.warning(f"Error fetching {current_kind}/{namespace}/{name}: {e}")
            await asyncio.sleep(1.0)  # Brief backoff before retry

    return resource_metadata, owner_metadata


async def generate_remediation_plan( # Renamed k8s_client to k8s_api_client for clarity
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    k8s_api_client: client.ApiClient, # Ensure parameter name is k8s_api_client
    agent: Optional[Agent[PlannerDependencies, RemediationPlan]] = None, # Added agent typing
) -> Optional[RemediationPlan]:
    """
    Generates a remediation plan using the following strategy:
    1. First attempt to generate a dynamic plan using an AI Agent (if provided and configured correctly).
       The agent is expected to be initialized with PlannerDependencies and RemediationPlan output type.
    2. If Gemini fails or returns empty, fall back to static pre-configured plans
    Args:
        anomaly_record: The detected anomaly record.
        db: AsyncIOMotorDatabase instance.
        k8s_api_client: Initialized Kubernetes ApiClient.
        agent: Optional initialized Agent instance, expected to be Agent[PlannerDependencies, RemediationPlan].

    Returns:
        A RemediationPlan object or None if generation fails.
    """
    if not anomaly_record.id:
        logger.error("Cannot generate plan: AnomalyRecord is missing database ID.")
        return None

    anomaly_id_str = str(anomaly_record.id)
    anomaly_collection = db["anomalies"]

    logger.info(f"Generating remediation plan for anomaly: {anomaly_id_str}")
    
    # Track whether primary (AI) or fallback (static) strategy was used
    used_primary_strategy = False
    plan = None

    # --- PRIMARY STRATEGY: Gemini AI plan generation ---
    if agent:
        try:
            logger.info(f"Using PRIMARY strategy (Gemini AI) to generate plan for anomaly {anomaly_id_str}")
            logger.debug(f"generate_remediation_plan received arguments: db={db is not None}, k8s_api_client={k8s_api_client is not None}, agent={agent is not None}")

            # 1. Prepare dependencies and gather context for the AI
            planner_deps = PlannerDependencies(db=db, k8s_client=k8s_api_client)
            recent_events = await get_recent_events(planner_deps.db, anomaly_record)
            resource_data, controller_data = await get_resource_metadata(planner_deps.k8s_client, anomaly_record)

            # Convert anomaly record to dict, handling potential ObjectId
            anomaly_dict = anomaly_record.model_dump(mode="json")
            anomaly_dict["id"] = anomaly_id_str
            
            # 2. Construct comprehensive AI prompt with all available info
            prompt = f"""
            Context:
            An anomaly has been detected in the Kubernetes cluster requiring remediation.

            Anomaly Details:
            {json.dumps(anomaly_dict, indent=2, default=datetime_serializer)}

            Recent Related Warning Events (last {RECENT_EVENT_WINDOW.total_seconds() / 60:.0f} mins):
            {json.dumps(recent_events, indent=2, default=datetime_serializer) if recent_events else "No recent related warning events found."}

            Resource Details:
            {json.dumps(resource_data, indent=2, default=datetime_serializer) if resource_data else "No resource metadata available."}

            Controller Details (if applicable):
            {json.dumps(controller_data, indent=2, default=datetime_serializer) if controller_data else "No controller metadata available."}

            Available Remediation Actions:
            - action_type: "scale_deployment", parameters: {{ "name": "<deployment_name>", "replicas": <integer>, "namespace": "<namespace>" }}
            - action_type: "delete_pod", parameters: {{ "name": "<pod_name>", "namespace": "<namespace>" }}
            - action_type: "restart_deployment", parameters: {{ "name": "<deployment_name>", "namespace": "<namespace>" }}
            - action_type: "drain_node", parameters: {{ "name": "<node_name>", "grace_period_seconds": <integer>, "force": <boolean> }}
            - action_type: "scale_statefulset", parameters: {{ "name": "<statefulset_name>", "replicas": <integer>, "namespace": "<namespace>" }}

            Instructions:
            You are a Kubernetes remediation planning assistant. Your goal is to analyze the provided anomaly context and generate a concise, actionable RemediationPlan.

            Task:
            1. Analyze the Anomaly Details, Recent Events, Resource Details, and Controller Details.
            2. Determine the most likely cause and the best course of action using ONLY the available Remediation Actions.
            3. Construct a RemediationPlan containing:
                - `reasoning`: A brief explanation for the chosen actions based *only* on the provided context.
                - `actions`: A list of one or more actions from the available types with parameters filled using data from the context (e.g., actual resource names, namespaces). Use the minimum effective actions.
            4. If no clear action is suitable based *only* on the context, return a plan with an empty `actions` list and reasoning explaining why no action is recommended.

            Constraints:
            - Base your reasoning and actions *strictly* on the provided context. Do not infer external information.
            - Use only the specified `action_type` values.
            - Ensure parameter values like names and namespaces match the context exactly.
            """

            # 3. Call the AI agent (expected to be configured with output_type=RemediationPlan)
            try:
                logger.info(f"Calling AI agent for anomaly {anomaly_id_str} with low temperature...")
                # Pass dependencies via 'deps' argument and set model temperature
                result = await agent.run(
                    prompt,
                    deps=planner_deps,
                    model_settings={'temperature': 0.2} # Lower temperature for more deterministic plans
                )
                
                

                # 4. Process and validate the AI agent's result
                if isinstance(result, AgentRunResult) and result.output:
                    try:
                        # Extract JSON string from markdown code block
                        json_match = re.search(r"```json\n(.*)\n```", result.output, re.DOTALL)
                        if json_match:
                            json_plan_str = json_match.group(1)
                            # Parse JSON string into RemediationPlan object
                            plan_data = json.loads(json_plan_str)
                            plan = RemediationPlan(**plan_data)
                            used_primary_strategy = True
                            logger.info(f"Gemini planner generated plan for anomaly {anomaly_id_str}. Actions: {len(plan.actions)}, Reasoning: {plan.reasoning[:100]}...")
                        else:
                            logger.warning(f"AI agent output did not contain a JSON plan for anomaly {anomaly_id_str}. Output: {result.output[:200]}...")
                            plan = None # Ensure plan is None if no JSON is found
                    except (json.JSONDecodeError, ValidationError, Exception) as parse_error:
                        logger.error(f"Error parsing AI agent output for anomaly {anomaly_id_str}: {parse_error}")
                        logger.debug(f"Failed output: {result.output}")
                        plan = None # Ensure plan is None on parsing error
                elif isinstance(result, RemediationPlan): # Handle cases where agent might return the model directly
                     plan = result
                     used_primary_strategy = True
                     logger.info(f"Gemini planner generated plan for anomaly {anomaly_id_str}. Actions: {len(plan.actions)}, Reasoning: {plan.reasoning[:100]}...")
                else:
                    logger.warning(f"AI agent did not return a valid RemediationPlan or AgentRunResult with output for anomaly {anomaly_id_str}. Returned type: {type(result)}")
                    plan = None # Ensure plan is None if result is unexpected type

            except UnexpectedModelBehavior as agent_error: # Catch specific PydanticAI exception
                logger.error(f"AI agent encountered unexpected behavior for anomaly {anomaly_id_str}: {agent_error}")
                # Log potentially useful context from the exception if available
                if hasattr(agent_error, '__cause__') and agent_error.__cause__:
                    logger.error(f"  Cause: {agent_error.__cause__}")
                # Consider using capture_run_messages here if debugging is needed, but it adds complexity.
                plan = None # Ensure plan is None on model behavior error
            except Exception as agent_error: # Catch other potential errors during agent run
                logger.exception(f"General AI agent error for anomaly {anomaly_id_str}: {agent_error}")
                plan = None # Ensure plan is None on general error

        except Exception as primary_error:
            logger.exception(f"Error during PRIMARY strategy setup/context gathering for anomaly {anomaly_id_str}: {primary_error}")
            # Ensure plan is None if the primary strategy itself errors out
            plan = None

    else:
        logger.warning(f"AI agent not available or not configured for anomaly {anomaly_id_str}, using fallback strategy")

    # --- FALLBACK STRATEGY: Static pre-configured plans ---
    # Use fallback if primary strategy wasn't used, failed, or returned an empty/invalid plan
    if not used_primary_strategy or not plan or not isinstance(plan, RemediationPlan) or not plan.actions:
        try:
            logger.info(f"Using FALLBACK strategy (static plans) for anomaly {anomaly_id_str}")

            # Call static plan generator
            fallback_plan = await load_static_plan(anomaly_record, db, k8s_api_client) # Pass correct client

            if fallback_plan:
                logger.info(
                    f"Generated static fallback plan for anomaly {anomaly_id_str}. "
                    f"Actions: {len(fallback_plan.actions)}, Reasoning: {fallback_plan.reasoning[:100]}..."
                )
                plan = fallback_plan
            else:
                logger.warning(f"Failed to generate static fallback plan for anomaly {anomaly_id_str}")

                # If fallback also failed, create an empty plan with reasoning
                if not plan: # If AI didn't produce a plan either
                     plan = RemediationPlan(
                         reasoning=(
                             "No remediation plan could be generated. Primary (AI Agent) "
                             "strategy failed or was skipped, and the fallback (static) "
                         ),
                         actions=[]
                     )
                # If AI produced an empty plan, keep its reasoning but log fallback failure
                elif not plan.actions:
                    logger.warning(f"AI produced an empty plan, and fallback static plan generation also failed for {anomaly_id_str}.")
                    # Keep the AI's reasoning in the 'plan' object

        except Exception as fallback_error:
            logger.exception(f"Error in FALLBACK strategy for anomaly {anomaly_id_str}: {fallback_error}")
            # If even static plan fails, ensure plan is empty
            plan = RemediationPlan(
                reasoning=f"All plan generation strategies failed. Fallback error: {str(fallback_error)}",
                actions=[]
            )

    # --- Update Anomaly Record with Final Plan ---
    try:
        # Determine source based on whether the primary strategy yielded a valid, non-empty plan
        plan_source = "AI generated" if used_primary_strategy and plan and plan.actions else "static fallback"

        # Ensure plan is not None before dumping
        plan_dict = plan.model_dump() if plan else None

        status_update = {
            "$set": {
                "remediation_plan": plan_dict,
                "remediation_status": "generated",
                "remediation_source": plan_source,
                "remediation_timestamp": datetime.datetime.now(datetime.timezone.utc)
            }
        }
        
        await anomaly_collection.update_one({"_id": anomaly_record.id}, status_update)
        
        # Update the passed-in record as well
        anomaly_record.remediation_plan = plan
        anomaly_record.remediation_status = "generated"
        
        return plan
    except Exception as update_error:
        logger.exception(f"Failed to update anomaly record with plan: {update_error}")
        return plan  # Still return the plan even if DB update fails


async def load_static_plan(
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    k8s_client: Optional[client.ApiClient] = None
) -> Optional[RemediationPlan]:
    """
    Load a static predefined remediation plan based on anomaly pattern matching.
    
    This is a faster alternative to AI-based planning that uses pre-defined templates
    for common failure scenarios.
    
    Args:
        anomaly_record: The anomaly record from the detector
        db: MongoDB database instance
        k8s_client: Optional Kubernetes client (if resource info is needed)
        
    Returns:
        A RemediationPlan if a matching static plan is found, otherwise None
    """
    if not anomaly_record.id:
        logger.error("Cannot load static plan: AnomalyRecord is missing database ID")
        return None
        
    # Extract relevant information from the anomaly
    metric_name = anomaly_record.metric_name or ""
    event_reason = anomaly_record.event_reason or ""
    entity_type = anomaly_record.entity_type or "Pod"  # Default to Pod if unknown
    entity_id = anomaly_record.entity_id or ""
    namespace, name = entity_id.split("/", 1) if "/" in entity_id else (None, None)
    
    # Check for direct failure flag from detector 
    if anomaly_record.is_direct_failure and anomaly_record.failure_reason:
        logger.info(f"Processing direct failure: {anomaly_record.failure_reason} for {entity_id}")
        
        # Select plan based on failure reason
        if anomaly_record.failure_reason in ("OOMKilled", "EVENT_OOMKILLED_COUNT"):
            # Handle OOM failures - get the template and add the anomaly ID
            plan = get_static_plan_template("oomkilled_event", "detected")
            if plan:
                plan.anomaly_id = anomaly_record.id
                plan.context = {
                    "anomaly_type": "direct_failure",
                    "failure_reason": anomaly_record.failure_reason,
                    "failure_message": anomaly_record.failure_message
                }
                return _format_static_plan(plan, entity_id)
        
        elif anomaly_record.failure_reason in ("CrashLoopBackOff", "EVENT_CRASHLOOPBACKOFF_COUNT"):
            # Handle crash loops - get high restart count plan
            plan = get_static_plan_template("container_restart_count", "high")
            if plan:
                plan.anomaly_id = anomaly_record.id
                plan.context = {
                    "anomaly_type": "direct_failure",
                    "failure_reason": anomaly_record.failure_reason,
                    "failure_message": anomaly_record.failure_message
                }
                return _format_static_plan(plan, entity_id)
        
        elif anomaly_record.failure_reason in ("ImagePullBackOff", "ErrImagePull", "EVENT_IMAGEPULLBACKOFF_COUNT"):
            # Image pull issues - use default pod restart plan
            plan = get_static_plan_template("default", "unknown")
            if plan:
                plan.anomaly_id = anomaly_record.id
                plan.plan_name = "ImagePullBackOff Pod Restart"
                plan.description = "Restart pod with image pull issues"
                plan.reasoning = f"Static plan for ImagePullBackOff failure detected on {entity_id}"
                plan.context = {
                    "anomaly_type": "direct_failure",
                    "failure_reason": anomaly_record.failure_reason,
                    "failure_message": anomaly_record.failure_message
                }
                return _format_static_plan(plan, entity_id)
        
        elif anomaly_record.failure_reason in ("NodeNotReady", "EVENT_UNHEALTHY_COUNT"):
            # Node issues - if we're handling a pod, try to delete to reschedule
            if entity_type.lower() == "pod":
                plan = get_static_plan_template("default", "unknown")
                if plan:
                    plan.anomaly_id = anomaly_record.id
                    plan.plan_name = "Pod on Unhealthy Node"
                    plan.description = "Restart pod on unhealthy node"
                    plan.reasoning = f"Static plan for pod on unhealthy node: {entity_id}"
                    plan.context = {
                        "anomaly_type": "direct_failure",
                        "failure_reason": anomaly_record.failure_reason,
                        "failure_message": anomaly_record.failure_message
                    }
                    return _format_static_plan(plan, entity_id)
            # For node issues with node entity_type, use the node pressure plan
            elif entity_type.lower() == "node":
                plan = get_static_plan_template("node_issue", "resource_pressure")
                if plan:
                    plan.anomaly_id = anomaly_record.id
                    plan.context = {
                        "anomaly_type": "direct_failure",
                        "failure_reason": anomaly_record.failure_reason,
                        "failure_message": anomaly_record.failure_message
                    }
                    return _format_static_plan(plan, entity_id)
        
        elif "Critical_" in anomaly_record.failure_reason:
            # Parse critical metric pattern
            pattern = anomaly_record.failure_reason.replace("Critical_", "")
            
            if "deployment_replicas_available_ratio" in pattern:
                # This is a deployment with insufficient available replicas
                if entity_type.lower() in ("deployment", "deploy"):
                    # Scale up the deployment using CPU high utilization plan (similar action)
                    plan = get_static_plan_template("cpu_utilization_pct", "high")
                    if plan:
                        plan.anomaly_id = anomaly_record.id
                        plan.plan_name = "Deployment Scale Up"
                        plan.description = "Scale up deployment with insufficient replicas"
                        plan.reasoning = f"Static plan for deployment with insufficient replicas: {entity_id}"
                        plan.context = {
                            "anomaly_type": "direct_failure",
                            "failure_reason": anomaly_record.failure_reason,
                            "failure_message": anomaly_record.failure_message
                        }
                        return _format_static_plan(plan, entity_id)
            
            elif "pod_crashloopbackoff" in pattern or "pod_imagepullbackoff" in pattern:
                # Pod in crash loop or image pull backoff
                plan = get_static_plan_template("default", "unknown")
                if plan:
                    plan.anomaly_id = anomaly_record.id
                    plan.plan_name = f"Pod {pattern} Restart"
                    plan.description = f"Restart pod with {pattern} issue"
                    plan.reasoning = f"Static plan for pod in {pattern}: {entity_id}"
                    plan.context = {
                        "anomaly_type": "direct_failure",
                        "failure_reason": anomaly_record.failure_reason,
                        "failure_message": anomaly_record.failure_message
                    }
                    return _format_static_plan(plan, entity_id)
    
    # Match based on metric name pattern
    for pattern in STATIC_PLAN_TEMPLATES.keys():
        if pattern in metric_name.lower():
            # For utilization metrics, determine if it's "high" or "spike"
            if "utilization" in metric_name.lower() or "usage" in metric_name.lower():
                # Check for both high (sustained) and spike patterns
                metric_value = anomaly_record.metric_value
                if metric_value is not None:
                    if metric_value > 90:  # Very high
                        variant = "high"
                    else:
                        variant = "spike"
                else:
                    variant = "high"  # Default if no value
                
                plan = get_static_plan_template(pattern, variant)
                if plan:
                    plan.anomaly_id = anomaly_record.id
                    return _format_static_plan(plan, entity_id)
            else:
                # For non-utilization metrics, use default variant if available
                plan = get_static_plan_template(pattern)
                if plan:
                    plan.anomaly_id = anomaly_record.id
                    return _format_static_plan(plan, entity_id)
    
    # Fall back to a default plan when no specific pattern matches
    default_plan = get_static_plan_template("default", "unknown")
    if default_plan:
        default_plan.anomaly_id = anomaly_record.id
        return _format_static_plan(default_plan, entity_id)
    
    return None


def _format_static_plan(plan: RemediationPlan, entity_id: str) -> RemediationPlan:
    """
    Format a static plan template, resolving placeholders like {resource_name} in actions.
    
    Args:
        plan: The template RemediationPlan
        entity_id: The entity ID in namespace/name format
        
    Returns:
        The formatted RemediationPlan with resolved placeholders
    """
    # Make a deep copy to avoid modifying the template
    formatted_plan = RemediationPlan(
        anomaly_id=plan.anomaly_id,  # Will be replaced later with actual anomaly ID
        plan_name=plan.plan_name,
        description=plan.description,
        reasoning=plan.reasoning,
        actions=[action.model_copy(deep=True) for action in plan.actions],
        ordered=plan.ordered,
        source_type=plan.source_type,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        completed=plan.completed,
        successful=plan.successful,
        completion_time=plan.completion_time,
        ml_model_id=plan.ml_model_id,
        trigger_source=plan.trigger_source,
        target_entity_type=plan.target_entity_type,
        target_entity_id=plan.target_entity_id,
        execution_attempts=plan.execution_attempts,
        exec_result_url=plan.exec_result_url,
        requires_approval=plan.requires_approval,
        approved_by=plan.approved_by,
        approval_time=plan.approval_time
    )
    
    # Extract namespace and name from entity_id
    namespace, name = entity_id.split("/", 1) if "/" in entity_id else (None, None)
    
    # Update actions with context
    for action in formatted_plan.actions:
        for key, value in list(action.parameters.items()):
            if isinstance(value, str):
                # Replace {resource_name} with the entity name
                if "{resource_name}" in value:
                    action.parameters[key] = value.replace("{resource_name}", name or "")
                
                # Replace {namespace} with the extracted namespace
                if "{namespace}" in value:
                    action.parameters[key] = value.replace("{namespace}", namespace or "default")
                    
                # Replace {pod_name} with the entity name for pod actions
                if "{pod_name}" in value:
                    action.parameters[key] = value.replace("{pod_name}", name or "")
    
    return formatted_plan
