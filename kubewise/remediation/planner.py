from __future__ import annotations

import asyncio
import datetime
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import motor.motor_asyncio
from bson import ObjectId
from kubernetes_asyncio import client
from kubewise.logging import get_logger
from pydantic import ValidationError
from pydantic_ai import Agent

# Initialize logger with component name
logger = get_logger("remediation.planner")
from pydantic_ai.exceptions import UnexpectedModelBehavior

from kubewise.models import (
    AnomalyRecord,
    RemediationAction,
    RemediationPlan,
    ActionType,
)
from kubewise.utils.retry import with_exponential_backoff # Import the decorator


# Define dependencies for the planner agent
@dataclass
class PlannerDependencies:
    db: motor.motor_asyncio.AsyncIOMotorDatabase
    k8s_client: client.ApiClient
    diagnostic_tools: Optional["DiagnosticTools"] = None

    def __post_init__(self):
        """Initialize diagnostic tools if not provided."""
        if self.diagnostic_tools is None and self.k8s_client is not None:
            self.diagnostic_tools = DiagnosticTools(self.k8s_client)


# Helper function for JSON serialization of datetime objects
def datetime_serializer(obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    # Let the default encoder handle other types or raise TypeError
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Constants
RECENT_EVENT_WINDOW = datetime.timedelta(minutes=15)
MAX_RESOURCE_FETCH_RETRIES = 3
RESOURCE_FETCH_TIMEOUT = 10.0  # seconds

# Template RemediationPlans

CPU_HIGH_UTILIZATION_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="CPU High Utilization Scale Up",
    description="Scale up deployment to handle high CPU utilization",
    reasoning="Static plan: High CPU utilization detected. Scaling up the deployment to distribute load.",
    actions=[
        RemediationAction(
            action_type="scale_deployment",
            parameters={
                "name": "{resource_name}",
                "replicas": "{current_replicas + 1}",
                "namespace": "{namespace}",
            },
            description="Scale up deployment replicas",
            justification="Increase capacity to handle high CPU load",
            entity_type="deployment",
            entity_id="{namespace}/{resource_name}",
        ).model_dump()
    ],
)

CPU_SPIKE_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="CPU Spike Restart",
    description="Restart deployment to address CPU spike",
    reasoning="Static plan: CPU spike detected. Restarting the deployment to address potential memory leak or runaway process.",
    actions=[
        RemediationAction(
            action_type="restart_deployment",
            parameters={"name": "{resource_name}", "namespace": "{namespace}"},
            description="Restart deployment to clear CPU spike",
            justification="Restarting may clear memory leaks or runaway processes",
            entity_type="deployment",
            entity_id="{namespace}/{resource_name}",
        ).model_dump()
    ],
)

def get_container_name_from_pod_metadata(resource_meta: Dict[str, Any], default_container_name: str = "") -> str:
    """
    Extract the actual container name from pod metadata to avoid container name mismatches.
    
    Args:
        resource_meta: Resource metadata from Kubernetes API
        default_container_name: Default container name to return if no container found
        
    Returns:
        Actual container name from the pod spec
    """
    if not resource_meta:
        return default_container_name
        
    try:
        # Extract containers from pod spec
        containers = []
        if "spec" in resource_meta and "containers" in resource_meta["spec"]:
            containers = resource_meta["spec"]["containers"]
            
        if not containers:
            return default_container_name
            
        # If there's only one container, use that
        if len(containers) == 1:
            return containers[0]["name"]
            
        # If there are multiple containers, try to find one with a name similar to the pod name
        pod_name = resource_meta.get("metadata", {}).get("name", "")
        
        # Try to match the container name to pod name (without the hash/suffix part)
        if pod_name:
            # Remove the hash/suffix (e.g., "ama-metrics-79bdc6b4b4-xg2kk" -> "ama-metrics")
            base_name = pod_name.split("-")[0] if "-" in pod_name else pod_name
            
            # Look for a container with a similar name
            for container in containers:
                container_name = container.get("name", "")
                if base_name.lower() in container_name.lower() or container_name.lower() in base_name.lower():
                    return container_name
        
        # If no match found, use the first container
        if containers:
            return containers[0]["name"]
            
    except Exception as e:
        logger.warning(f"Error extracting container name from pod metadata: {e}")
        
    return default_container_name


MEMORY_HIGH_UTILIZATION_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="Memory High Utilization Remediation",
    description="Update resource limits and scale up to handle high memory utilization",
    reasoning="Static plan: High memory utilization detected. Updating resource limits and scaling up the deployment to handle memory pressure.",
    actions=[
        RemediationAction(
            action_type="update_resource_limits",
            parameters={
                "name": "{resource_name}",
                "namespace": "{namespace}",
                "kind": "Deployment",
                "container": "{resource_name.split('-')[0] if '-' in resource_name else resource_name}",
                "limits": {"memory": "512Mi"},
                "requests": {"memory": "256Mi"}
            },
            description="Update memory limits and requests",
            justification="Increase memory allocation to prevent OOM kills",
            entity_type="deployment",
            entity_id="{namespace}/{resource_name}",
        ).model_dump(),
        RemediationAction(
            action_type="scale_deployment",
            parameters={
                "name": "{resource_name}",
                "replicas": "{current_replicas + 1}",
                "namespace": "{namespace}",
            },
            description="Scale up deployment replicas",
            justification="Distribute memory load across more instances",
            entity_type="deployment",
            entity_id="{namespace}/{resource_name}",
        ).model_dump()
    ],
)

MEMORY_LEAK_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="Memory Leak Remediation",
    description="Update resource limits and restart pods to address memory leak",
    reasoning="Static plan: Potential memory leak detected. Updating resource limits and restarting the affected pods to reclaim memory.",
    actions=[
        RemediationAction(
            action_type="update_resource_limits",
            parameters={
                "name": "{pod_name}", 
                "namespace": "{namespace}",
                "kind": "Pod",
                "container": "{pod_name.split('-')[0] if '-' in pod_name else pod_name}",
                "limits": {"memory": "512Mi"},
                "requests": {"memory": "256Mi"}
            },
            description="Update memory limits to provide more headroom",
            justification="Increasing memory limits to prevent immediate OOM kills while the leak is investigated",
            entity_type="pod",
            entity_id="{namespace}/{pod_name}",
        ).model_dump(),
        RemediationAction(
            action_type="force_delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"},
            description="Force delete pod to trigger recreation",
            justification="Force restarting pod will reclaim leaked memory, even if the pod is stuck in Terminating state",
            entity_type="pod",
            entity_id="{namespace}/{pod_name}",
        ).model_dump()
    ],
)

OOMKILLED_POD_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="OOMKilled Pod Remediation",
    description="Increase memory limits and restart pod that experienced OOMKilled event",
    reasoning="Static plan: OOMKilled event detected for pod. Increasing memory limits and restarting the pod to prevent recurrence.",
    actions=[
        RemediationAction(
            action_type="update_resource_limits",
            parameters={
                "name": "{pod_name}", 
                "namespace": "{namespace}",
                "kind": "Pod",
                "container": "{pod_name.split('-')[0] if '-' in pod_name else pod_name}",
                "limits": {"memory": "512Mi"},
                "requests": {"memory": "256Mi"}
            },
            description="Increase memory limits to prevent OOMKilled",
            justification="Pod was killed due to memory constraints, increasing limits to prevent recurrence",
            entity_type="pod",
            entity_id="{namespace}/{pod_name}",
        ).model_dump(),
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"},
            description="Delete pod that experienced OOMKilled",
            justification="Pod was killed due to out of memory, needs restart with new limits",
            entity_type="pod",
            entity_id="{namespace}/{pod_name}",
        ).model_dump()
    ],
)

HIGH_RESTART_COUNT_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="High Restart Count Pod Deletion",
    description="Delete pod with high container restart count",
    reasoning="Static plan: High container restart count detected. Pod appears to be in a crash loop - attempting targeted pod deletion.",
    actions=[
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"},
            description="Delete pod in crash loop",
            justification="Pod is in crash loop, needs manual intervention",
            entity_type="pod",
            entity_id="{namespace}/{pod_name}",
        ).model_dump()
    ],
)

NODE_PRESSURE_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="Node Resource Pressure Remediation",
    description="Cordon and drain node experiencing resource pressure",
    reasoning="Static plan: Node is experiencing resource pressure. Cordoning and draining the node to redistribute workloads.",
    actions=[
        RemediationAction(
            action_type="drain_node",
            parameters={
                "name": "{node_name}",
                "grace_period_seconds": 300,
                "force": False,
            },
            description="Drain node under resource pressure",
            justification="Node is experiencing resource pressure, needs workload redistribution",
            entity_type="node",
            entity_id="{node_name}",
        ).model_dump()
    ],
)

STATEFULSET_SCALE_UP_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="StatefulSet Scale Up",
    description="Scale up statefulset experiencing high load",
    reasoning="Static plan: StatefulSet experiencing high load. Scaling up to distribute workload.",
    actions=[
        RemediationAction(
            action_type="scale_statefulset",
            parameters={
                "name": "{resource_name}",
                "replicas": "{current_replicas + 1}",
                "namespace": "{namespace}",
            },
            description="Scale up StatefulSet replicas",
            justification="Increase capacity to handle high load",
            entity_type="statefulset",
            entity_id="{namespace}/{resource_name}",
        ).model_dump()
    ],
)

DEFAULT_POD_RESTART_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="Generic Pod Restart",
    description="Restart pod as a generic remediation attempt",
    reasoning="Static plan: No specific pattern matched. Attempting generic pod restart as a safe first action.",
    actions=[
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"},
            description="Delete pod to trigger restart",
            justification="Generic remediation attempt",
            entity_type="pod",
            entity_id="{namespace}/{pod_name}",
        ).model_dump()
    ],
)

CONFIG_ERROR_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="ConfigMap Update",
    description="Update ConfigMap to fix configuration errors",
    reasoning="Static plan: Configuration errors detected in logs. Updating ConfigMap to fix the issues.",
    actions=[
        RemediationAction(
            action_type="update_configmap",
            parameters={
                "name": "{config_name}",
                "namespace": "{namespace}",
                "data": {
                    "log_level": "info",
                    "max_connections": "100"
                }
            },
            description="Update ConfigMap with corrected values",
            justification="Configuration errors in logs suggest incorrect ConfigMap values",
            entity_type="configmap",
            entity_id="{namespace}/{config_name}",
        ).model_dump(),
        RemediationAction(
            action_type="restart_deployment",
            parameters={
                "name": "{resource_name}",
                "namespace": "{namespace}"
            },
            description="Restart deployment to apply new config",
            justification="Deployment must be restarted to pick up ConfigMap changes",
            entity_type="deployment",
            entity_id="{namespace}/{resource_name}",
        ).model_dump()
    ],
)

CONTAINER_IMAGE_UPDATE_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="Container Image Update",
    description="Update container image to fix known issues",
    reasoning="Static plan: Container is running an outdated or buggy image. Updating to the latest stable version.",
    actions=[
        RemediationAction(
            action_type="update_container_image",
            parameters={
                "name": "{resource_name}",
                "namespace": "{namespace}",
                "kind": "Deployment",
                "container": "{resource_name.split('-')[0] if '-' in resource_name else resource_name}",
                "image": "{repository}/{image_name}:{latest_tag}"
            },
            description="Update container image to latest stable version",
            justification="Container is running a version with known issues or vulnerabilities",
            entity_type="deployment",
            entity_id="{namespace}/{resource_name}",
        ).model_dump()
    ],
)

# Static plan mapping
STATIC_PLAN_TEMPLATES = {
    "cpu_utilization_pct": {"high": CPU_HIGH_UTILIZATION_PLAN, "spike": CPU_SPIKE_PLAN},
    "memory_utilization_pct": {"high": MEMORY_HIGH_UTILIZATION_PLAN},
    "leak": MEMORY_LEAK_PLAN,
    "oomkilled_event": {"detected": OOMKILLED_POD_PLAN},
    "container_restart_count": {"high": HIGH_RESTART_COUNT_PLAN},
    "node_issue": {"resource_pressure": NODE_PRESSURE_PLAN},
    "statefulset_issue": {"high_load": STATEFULSET_SCALE_UP_PLAN},
    "config_error": {"detected": CONFIG_ERROR_PLAN},
    "outdated_image": {"detected": CONTAINER_IMAGE_UPDATE_PLAN},
    "default": {"unknown": DEFAULT_POD_RESTART_PLAN},
}


def get_static_plan_template(
    pattern: str, variant: str = "default"
) -> Optional[RemediationPlan]:
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
    if (
        "default" in STATIC_PLAN_TEMPLATES
        and "unknown" in STATIC_PLAN_TEMPLATES["default"]
    ):
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
        events_cursor = (
            event_collection.find(query).sort("lastTimestamp", -1).limit(limit)
        )
        events = await events_cursor.to_list(length=limit)

        # Convert ObjectId and datetime for JSON serialization in prompt
        for event in events:
            if "_id" in event:
                event["_id"] = str(event["_id"])
            if isinstance(event.get("firstTimestamp"), datetime.datetime):
                event["firstTimestamp"] = event["firstTimestamp"].isoformat()
            if isinstance(event.get("lastTimestamp"), datetime.datetime):
                event["lastTimestamp"] = event["lastTimestamp"].isoformat()

        logger.debug(
            f"Found {len(events)} recent related events for anomaly {anomaly_record.id}"
        )
        return events
    except Exception as e:
        logger.error(
            f"Error fetching recent events for anomaly {anomaly_record.id}: {e}"
        )
        return []


async def get_resource_metadata(
    api_client: client.ApiClient, name: str, kind: str, namespace: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Fetch metadata about the resource that experienced the anomaly.

    Args:
        api_client: Kubernetes API client
        name: Name of the resource
        kind: Kind of the resource (e.g., "pod", "deployment", "node")
        namespace: Optional namespace of the resource
    Returns:
        Tuple of (resource metadata, owner metadata) if fetching is successful,
        otherwise (None, None)
    """
    if not name or not kind:
        logger.warning("Cannot fetch resource metadata without name and kind")
        return None, None

    current_kind = kind.lower()
    resource_metadata = None
    owner_metadata = None

    # Ensure namespace is provided for namespaced resources
    namespaced_kinds = [
        "pod", "deployment", "replicaset", "statefulset", "daemonset", 
        "job", "cronjob", "service", "persistentvolumeclaim", "pvc",
        "configmap", "secret", "hpa", "horizontalpodautoscaler", "ingress"
    ]
    if current_kind in namespaced_kinds and not namespace:
        logger.warning(f"Namespace required for fetching metadata of kind '{current_kind}', but not provided. Resource: {name}")
        # Attempt to use 'default' if it's a common namespaced resource, otherwise fail
        if current_kind in ["pod", "deployment", "statefulset", "daemonset", "service"]:
            logger.warning(f"Assuming 'default' namespace for {current_kind}/{name}")
            namespace = "default"
        else:
            return None, None


    for _ in range(MAX_RESOURCE_FETCH_RETRIES):
        try:
            # Fetch the resource based on its kind
            if current_kind == "pod":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_pod(
                    name=name, namespace=namespace
                )
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
                        logger.info(
                            f"Owner kind '{owner_kind}' not handled explicitly for metadata fetching"
                        )

            elif current_kind == "deployment":
                apps_v1 = client.AppsV1Api(api_client)
                resource = await apps_v1.read_namespaced_deployment(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()

            elif current_kind == "replicaset":
                apps_v1 = client.AppsV1Api(api_client)
                resource = await apps_v1.read_namespaced_replica_set(
                    name=name, namespace=namespace
                )
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
                resource = await apps_v1.read_namespaced_stateful_set(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()

            elif current_kind == "daemonset":
                apps_v1 = client.AppsV1Api(api_client)
                resource = await apps_v1.read_namespaced_daemon_set(
                    name=name, namespace=namespace
                )
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
                resource = await core_v1.read_namespaced_service(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()

            elif current_kind == "persistentvolumeclaim" or current_kind == "pvc":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_persistent_volume_claim(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()

            elif current_kind == "job":
                batch_v1 = client.BatchV1Api(api_client)
                resource = await batch_v1.read_namespaced_job(
                    name=name, namespace=namespace
                )
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
                resource = await batch_v1.read_namespaced_cron_job(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()

            elif current_kind == "configmap":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_config_map(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()

            elif current_kind == "secret":
                core_v1 = client.CoreV1Api(api_client)
                resource = await core_v1.read_namespaced_secret(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()
                # Remove actual secret data for security
                if resource_metadata and "data" in resource_metadata:
                    resource_metadata["data"] = {
                        k: f"<{len(v) if v else 0} bytes>"
                        for k, v in resource_metadata["data"].items()
                    }

            elif current_kind == "hpa" or current_kind == "horizontalpodautoscaler":
                autoscaling_v1 = client.AutoscalingV1Api(api_client)
                resource = (
                    await autoscaling_v1.read_namespaced_horizontal_pod_autoscaler(
                        name=name, namespace=namespace
                    )
                )
                resource_metadata = resource.to_dict()

            elif current_kind == "ingress":
                networking_v1 = client.NetworkingV1Api(api_client)
                resource = await networking_v1.read_namespaced_ingress(
                    name=name, namespace=namespace
                )
                resource_metadata = resource.to_dict()

            else:
                logger.info(
                    f"Fetching resource kind '{current_kind}' using generic approach"
                )
                # Generic approach using dynamic client for other resource types
                resource_metadata = {
                    "kind": current_kind.capitalize(),
                    "metadata": {"name": name, "namespace": namespace},
                    "note": f"Limited metadata for {current_kind} - use specific API for full details",
                }

            # Successfully fetched, break retry loop
            break

        except client.rest.ApiException as e:
            if e.status == 404:
                logger.warning(f"Resource not found: {current_kind}/{namespace}/{name}")
                return None, None
            elif e.status == 403:
                logger.warning(
                    f"Permission denied fetching {current_kind}/{namespace}/{name}"
                )
                return None, None
            else:
                logger.warning(
                    f"API error fetching {current_kind}/{namespace}/{name}: {e.status} - {e.reason}"
                )
                await asyncio.sleep(1.0)  # Brief backoff before retry
        except Exception as e:
            logger.warning(f"Error fetching {current_kind}/{namespace}/{name}: {e}")
            await asyncio.sleep(1.0)  # Brief backoff before retry

    return resource_metadata, owner_metadata


async def get_pod_controller(
    api_client: client.ApiClient, pod_name: str, namespace: str
) -> Tuple[Optional[str], Optional[str]]:
    """
    Recursively traverse owner references to find the ultimate controller.
    
    Args:
        api_client: Kubernetes API client
        pod_name: Name of the pod
        namespace: Namespace of the pod
        
    Returns:
        Tuple of (controller_kind, controller_name) or (None, None) if not found
    """
    if not pod_name or not namespace:
        logger.warning("Cannot get pod controller without pod name and namespace")
        return None, None
    
    try:
        # Initialize API clients
        core_v1 = client.CoreV1Api(api_client)
        apps_v1 = client.AppsV1Api(api_client)
        
        # Get the pod
        pod = await core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        
        # Check if pod has owner references
        if not pod.metadata.owner_references or len(pod.metadata.owner_references) == 0:
            logger.info(f"Pod {namespace}/{pod_name} has no owner references, it's likely a standalone pod")
            return "Pod", pod_name
            
        # Get the immediate owner
        owner_ref = pod.metadata.owner_references[0]  # Usually just one owner
        owner_kind = owner_ref.kind
        owner_name = owner_ref.name
        
        # If owner is already a DaemonSet, Deployment, or StatefulSet, return it
        if owner_kind in ["DaemonSet", "Deployment", "StatefulSet", "Job", "CronJob"]:
            logger.info(f"Pod {namespace}/{pod_name} is directly owned by {owner_kind}/{owner_name}")
            return owner_kind, owner_name
            
        # If owner is a ReplicaSet, check if it's owned by a Deployment
        if owner_kind == "ReplicaSet":
            try:
                rs = await apps_v1.read_namespaced_replica_set(name=owner_name, namespace=namespace)
                
                # Check if ReplicaSet has owner references
                if rs.metadata.owner_references and len(rs.metadata.owner_references) > 0:
                    deployment_ref = rs.metadata.owner_references[0]
                    if deployment_ref.kind == "Deployment":
                        logger.info(f"Pod {namespace}/{pod_name} is owned by ReplicaSet {owner_name}, which is owned by Deployment {deployment_ref.name}")
                        return "Deployment", deployment_ref.name
                
                # ReplicaSet exists but has no owner, return ReplicaSet
                return "ReplicaSet", owner_name
            except client.ApiException as e:
                logger.warning(f"Failed to get ReplicaSet {namespace}/{owner_name}: {e}")
                return "ReplicaSet", owner_name  # Return what we know even if fetch failed
        
        # Handle other potential owner types if needed
        logger.info(f"Pod {namespace}/{pod_name} has owner {owner_kind}/{owner_name}, but no higher-level controller found")
        return owner_kind, owner_name
        
    except client.ApiException as e:
        if e.status == 404:
            logger.warning(f"Pod {namespace}/{pod_name} not found")
        else:
            logger.error(f"Error finding controller for pod {namespace}/{pod_name}: {e}")
        return None, None
    except Exception as e:
        logger.error(f"Unexpected error finding controller for pod {namespace}/{pod_name}: {e}")
        return None, None
        
@with_exponential_backoff()
async def find_matching_daemonset(
    api_client: client.ApiClient, namespace: str, pod_name_pattern: str
) -> Optional[str]:
    """
    Find a DaemonSet that might match a pod name pattern.
    
    Args:
        api_client: Kubernetes API client
        namespace: Namespace to search in
        pod_name_pattern: Pod name or pattern to match against
        
    Returns:
        DaemonSet name if found, None otherwise
    """
    try:
        # Get all DaemonSets first so we can do direct matches
        apps_v1 = client.AppsV1Api(api_client)
        daemonsets = await apps_v1.list_namespaced_daemon_set(namespace=namespace)
        
        # Method 1: Check if the input is directly a DaemonSet name or has a prefix match
        for ds in daemonsets.items:
            if ds.metadata.name == pod_name_pattern:
                logger.info(f"Direct match: {pod_name_pattern} is a DaemonSet name")
                return ds.metadata.name
            # Check if the DaemonSet name is a prefix of the input (for cases like 'ama-metrics-node')
            if pod_name_pattern.startswith(ds.metadata.name + "-"):
                logger.info(f"Prefix match: DaemonSet {ds.metadata.name} is a prefix of {pod_name_pattern}")
                return ds.metadata.name
            # Check if the input is a prefix of the DaemonSet (for cases like 'ama-metrics' with DaemonSets like 'ama-metrics-node') 
            if ds.metadata.name.startswith(pod_name_pattern + "-"):
                logger.info(f"Input prefix match: Input {pod_name_pattern} is a prefix of DaemonSet {ds.metadata.name}")
                return ds.metadata.name
                
        # Method 2: Check if pod exists and get its controller
        if pod_name_pattern:
            controller_kind, controller_name = await get_pod_controller(api_client, pod_name_pattern, namespace)
            if controller_kind == "DaemonSet" and controller_name:
                logger.info(f"Found DaemonSet {controller_name} through owner references of pod {pod_name_pattern}")
                return controller_name
                
        # If not found through owner references, use the DaemonSets we already listed to look for name patterns
        
        # Try to find a matching DaemonSet by common patterns
        base_name = pod_name_pattern.split('-node-')[0] if '-node-' in pod_name_pattern else pod_name_pattern
        
        # Try component-based matching - extract the first parts of the name
        # This works for many common naming patterns including Azure Monitor agents
        name_parts = pod_name_pattern.split('-')
        if len(name_parts) >= 2:
            component_name = name_parts[0] + "-" + name_parts[1]  # e.g., "ama-metrics", "kube-proxy"
            for ds in daemonsets.items:
                if ds.metadata.name.startswith(component_name):
                    logger.info(f"Found DaemonSet {ds.metadata.name} matching component pattern {component_name}")
                    return ds.metadata.name

        # Special handling for exact Azure Monitor agent prefixes
        if pod_name_pattern.startswith("ama-metrics") or pod_name_pattern.startswith("ama-logs"):
            for ds in daemonsets.items:
                if ds.metadata.name.startswith(pod_name_pattern):
                    logger.info(f"Found Azure Monitor DaemonSet {ds.metadata.name} matching {pod_name_pattern}")
                    return ds.metadata.name
                
            # Fuzzy matching for Azure Monitor agents
            for ds in daemonsets.items:
                if "ama" in ds.metadata.name.lower() and (
                    ("metrics" in pod_name_pattern.lower() and "metrics" in ds.metadata.name.lower()) or
                    ("logs" in pod_name_pattern.lower() and "logs" in ds.metadata.name.lower())
                ):
                    logger.info(f"Found Azure Monitor DaemonSet {ds.metadata.name} matching pattern {pod_name_pattern}")
                    return ds.metadata.name
        
        # General pattern matching for other DaemonSets
        for ds in daemonsets.items:
            # Check if base name is part of DaemonSet name or vice versa
            if base_name.lower() in ds.metadata.name.lower() or ds.metadata.name.lower() in base_name.lower():
                logger.info(f"Found DaemonSet {ds.metadata.name} matching pattern from {pod_name_pattern}")
                return ds.metadata.name
                
            # As a last resort, check if any pods created by this DaemonSet match our pattern
            selector = ",".join([f"{k}={v}" for k, v in ds.spec.selector.match_labels.items()])
            pods = await client.CoreV1Api(api_client).list_namespaced_pod(
                namespace=namespace, label_selector=selector, limit=1
            )
            if pods.items:
                sample_pod_name = pods.items[0].metadata.name
                if sample_pod_name.startswith(base_name) or base_name.startswith(sample_pod_name.split('-')[0]):
                    logger.info(f"Found DaemonSet {ds.metadata.name} that creates pods like {sample_pod_name}")
                    return ds.metadata.name
        
        logger.warning(f"No matching DaemonSet found for pod pattern {pod_name_pattern} in namespace {namespace}")
        return None
    except client.ApiException as e:
        logger.error(f"API error while searching for DaemonSet matching {pod_name_pattern}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error searching for DaemonSet matching {pod_name_pattern}: {e}")
        return None

class DiagnosticTools:
    """A collection of tools for gathering diagnostic information from Kubernetes."""

    def __init__(self, k8s_client: client.ApiClient):
        self.k8s_client = k8s_client
        self.core_v1_api = client.CoreV1Api(k8s_client)
        self.apps_v1_api = client.AppsV1Api(k8s_client)
        self.networking_v1_api = client.NetworkingV1Api(k8s_client)
        self.batch_v1_api = client.BatchV1Api(k8s_client)

    async def get_pod_logs(
        self,
        namespace: str,
        pod_name: Optional[str] = None,
        name: Optional[str] = None,
        container: Optional[str] = None,
        tail_lines: int = 100,
    ) -> Dict[str, Any]:
        """Get logs for a specific pod and container."""
        # Allow either pod_name or name parameter
        pod_name = pod_name or name
        if not pod_name:
            return {"error": "Pod name not provided", "logs": ""}

        try:
            logs = await self.core_v1_api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines,
            )
            return {
                "logs": logs,
                "pod_name": pod_name,
                "container": container or "default",
            }
        except client.ApiException as e:
            return {
                "error": f"{e.status} - {e.reason}",
                "logs": "",
                "pod_name": pod_name,
            }
        except Exception as e:
            return {"error": str(e), "logs": "", "pod_name": pod_name}

    async def describe_pod(
        self, namespace: str, pod_name: Optional[str] = None, name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get detailed information about a pod."""
        # Allow either pod_name or name parameter
        pod_name = pod_name or name
        if not pod_name:
            return {"error": "Pod name not provided"}

        try:
            pod = await self.core_v1_api.read_namespaced_pod(
                name=pod_name, namespace=namespace
            )

            # Get pod events
            field_selector = f"involvedObject.name={pod_name}"
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            # Organize the information
            result = {
                "pod": pod.to_dict(),
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_statefulset(self, namespace: str, name: str) -> Dict[str, Any]:
        """Get detailed information about a StatefulSet."""
        try:
            sts = await self.apps_v1_api.read_namespaced_stateful_set(
                name=name, namespace=namespace
            )

            # Get associated pods
            label_selector = self._get_selector_from_statefulset(sts)
            pods = await self.core_v1_api.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )

            # Get events for the statefulset
            field_selector = (
                f"involvedObject.name={name},involvedObject.kind=StatefulSet"
            )
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            result = {
                "statefulset": sts.to_dict(),
                "pods": [p.to_dict() for p in pods.items],
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_deployment(self, namespace: str, name: str) -> Dict[str, Any]:
        """Get detailed information about a Deployment."""
        try:
            deploy = await self.apps_v1_api.read_namespaced_deployment(
                name=name, namespace=namespace
            )

            # Get associated pods via ReplicaSets
            label_selector = self._get_selector_from_deployment(deploy)
            rs_list = await self.apps_v1_api.list_namespaced_replica_set(
                namespace=namespace, label_selector=label_selector
            )

            replicasets = []
            pods = []

            for rs in rs_list.items:
                replicasets.append(rs.to_dict())

                # Get pods for this ReplicaSet
                rs_label_selector = self._get_selector_from_replicaset(rs)
                pod_list = await self.core_v1_api.list_namespaced_pod(
                    namespace=namespace, label_selector=rs_label_selector
                )
                pods.extend([p.to_dict() for p in pod_list.items])

            # Get events for the deployment
            field_selector = (
                f"involvedObject.name={name},involvedObject.kind=Deployment"
            )
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            result = {
                "deployment": deploy.to_dict(),
                "replicasets": replicasets,
                "pods": pods,
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_node(self, name: str) -> Dict[str, Any]:
        """Get detailed information about a node."""
        try:
            node = await self.core_v1_api.read_node(name=name)

            # Get pods running on this node
            field_selector = f"spec.nodeName={name}"
            pods = await self.core_v1_api.list_pod_for_all_namespaces(
                field_selector=field_selector
            )

            # Get events for the node
            field_selector = f"involvedObject.name={name},involvedObject.kind=Node"
            events = await self.core_v1_api.list_event_for_all_namespaces(
                field_selector=field_selector
            )

            result = {
                "node": node.to_dict(),
                "pods": [p.to_dict() for p in pods.items],
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_resource_usage(
        self,
        namespace: Optional[str] = None,
        entity_type: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get resource usage information for pods, deployments, statefulsets, or nodes.

        Args:
            namespace: Optional namespace to filter metrics
            entity_type: Optional entity type (pod, deployment, node, etc.) to filter results
            name: Optional name of the specific entity to get metrics for

        Returns:
            Dictionary with resource usage metrics
        """
        try:
            metrics_api = client.CustomObjectsApi(self.k8s_client)

            # Get pod metrics
            if namespace:
                pod_metrics = await metrics_api.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods",
                )
            else:
                pod_metrics = await metrics_api.list_cluster_custom_object(
                    group="metrics.k8s.io", version="v1beta1", plural="pods"
                )

            # Get node metrics
            node_metrics = await metrics_api.list_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="nodes"
            )

            # Filter metrics by entity type and name if provided
            result = {"pod_metrics": pod_metrics, "node_metrics": node_metrics}

            # Apply filtering if name and entity_type are provided
            if name and entity_type:
                if entity_type.lower() == "pod":
                    # Filter pod metrics by name
                    if "items" in pod_metrics:
                        result["pod_metrics"]["items"] = [
                            item
                            for item in pod_metrics["items"]
                            if item.get("metadata", {}).get("name") == name
                        ]
                elif entity_type.lower() == "node":
                    # Filter node metrics by name
                    if "items" in node_metrics:
                        result["node_metrics"]["items"] = [
                            item
                            for item in node_metrics["items"]
                            if item.get("metadata", {}).get("name") == name
                        ]
                elif entity_type.lower() in ["deployment", "statefulset", "daemonset"]:
                    # For higher-level resources, we need to find associated pods
                    # Get the pods belonging to this deployment/statefulset
                    if namespace:
                        try:
                            if entity_type.lower() == "deployment":
                                deploy = (
                                    await self.apps_v1_api.read_namespaced_deployment(
                                        name=name, namespace=namespace
                                    )
                                )
                                selector = self._get_selector_from_deployment(deploy)
                            elif entity_type.lower() == "statefulset":
                                sts = (
                                    await self.apps_v1_api.read_namespaced_stateful_set(
                                        name=name, namespace=namespace
                                    )
                                )
                                selector = self._get_selector_from_statefulset(sts)
                            else:  # daemonset
                                ds = await self.apps_v1_api.read_namespaced_daemon_set(
                                    name=name, namespace=namespace
                                )
                                selector = ",".join(
                                    [
                                        f"{k}={v}"
                                        for k, v in ds.spec.selector.match_labels.items()
                                    ]
                                )

                            # Filter pod metrics by selector
                            if "items" in pod_metrics and selector:
                                # We need to cross-reference with actual pods to find matches
                                pods = await self.core_v1_api.list_namespaced_pod(
                                    namespace=namespace, label_selector=selector
                                )
                                pod_names = [p.metadata.name for p in pods.items]

                                result["pod_metrics"]["items"] = [
                                    item
                                    for item in pod_metrics["items"]
                                    if item.get("metadata", {}).get("name") in pod_names
                                ]
                        except client.ApiException as e:
                            result["error"] = (
                                f"Error finding pods for {entity_type} {name}: {e.status} - {e.reason}"
                            )

            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_event_history(
        self, namespace: str, name: str, entity_type: str, limit: int = 50
    ) -> Dict[str, Any]:
        """Get recent events for a specific Kubernetes resource.

        Args:
            namespace: Namespace of the resource
            name: Name of the resource
            entity_type: Type of resource (pod, deployment, node, etc.)
            limit: Maximum number of events to return

        Returns:
            Dictionary with events and resource information
        """
        try:
            # Map entity_type to Kubernetes API object kind
            kind_mapping = {
                "pod": "Pod",
                "deployment": "Deployment",
                "statefulset": "StatefulSet",
                "daemonset": "DaemonSet",
                "service": "Service",
                "node": "Node",
                "persistentvolumeclaim": "PersistentVolumeClaim",
                "persistentvolume": "PersistentVolume",
                "configmap": "ConfigMap",
                "secret": "Secret",
                "job": "Job",
                "cronjob": "CronJob",
                "namespace": "Namespace",
                "horizontalpodautoscaler": "HorizontalPodAutoscaler",
                "ingress": "Ingress",
                "replicaset": "ReplicaSet",
            }

            # Default to exact match if not in mapping
            kind = kind_mapping.get(entity_type.lower(), entity_type)

            # Get events for the resource
            field_selector = f"involvedObject.name={name},involvedObject.kind={kind}"

            if entity_type.lower() == "node":
                events = await self.core_v1_api.list_event_for_all_namespaces(
                    field_selector=field_selector, limit=limit
                )
            else:
                events = await self.core_v1_api.list_namespaced_event(
                    namespace=namespace, field_selector=field_selector, limit=limit
                )

            # Get resource details
            resource_info = {}
            try:
                if entity_type.lower() == "pod":
                    resource = await self.core_v1_api.read_namespaced_pod(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "deployment":
                    resource = await self.apps_v1_api.read_namespaced_deployment(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "statefulset":
                    resource = await self.apps_v1_api.read_namespaced_stateful_set(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "node":
                    resource = await self.core_v1_api.read_node(name=name)
                    resource_info = resource.to_dict()
                # Add other entity types as needed
            except client.ApiException as e:
                resource_info = {
                    "error": f"Error retrieving {entity_type} details: {e.status} - {e.reason}"
                }

            return {
                "events": [e.to_dict() for e in events.items],
                "resource_info": resource_info,
                "resource_type": kind,
                "resource_name": name,
                "namespace": namespace,
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def list_events(
        self,
        namespace: str,
        resource_name: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List recent events for a namespace or specific resource."""
        try:
            field_selector = None
            if resource_name and kind:
                field_selector = (
                    f"involvedObject.name={resource_name},involvedObject.kind={kind}"
                )
            elif resource_name:
                field_selector = f"involvedObject.name={resource_name}"

            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector, limit=limit
            )

            return {"events": [e.to_dict() for e in events.items]}
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def exec_in_pod(
        self,
        namespace: str,
        pod_name: Optional[str] = None,
        name: Optional[str] = None,
        container: Optional[str] = None,
        command: List[str] = None,
    ) -> Dict[str, Any]:
        """Execute a command inside a pod container."""
        # Allow either pod_name or name parameter
        pod_name = pod_name or name
        if not pod_name:
            return {"error": "Pod name not provided", "output": ""}

        if not command:
            command = ["cat", "/proc/meminfo"]  # Default to a safe command

        try:
            # The kubernetes-asyncio client doesn't directly support exec
            # So we'll use the core API to construct the URL and then use websockets
            import kubernetes.stream as stream

            # Create a synchronous client temporarily
            from kubernetes import client as sync_client
            from kubernetes.config import load_kube_config

            # This is a workaround as kubernetes-asyncio doesn't support exec well
            # We'll use the synchronous client for this specific operation
            load_kube_config()
            sync_api = sync_client.CoreV1Api()

            # Execute the command
            exec_result = stream.stream(
                sync_api.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                container=container,
                command=command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            # Collect the output
            output = ""
            while exec_result.is_open():
                exec_result.update(timeout=1)
                if exec_result.peek_stdout():
                    output += exec_result.read_stdout()
                if exec_result.peek_stderr():
                    output += exec_result.read_stderr()

            exec_result.close()
            return {
                "output": output,
                "pod_name": pod_name,
                "namespace": namespace,
                "container": container,
                "command": command,
            }
        except Exception as e:
            return {
                "error": str(e),
                "output": "",
                "pod_name": pod_name,
                "namespace": namespace,
            }

    async def get_node_conditions(self, name: str) -> Dict[str, Any]:
        """Get the conditions of a specific node.

        Args:
            name: Name of the node

        Returns:
            Dictionary with node conditions and status
        """
        try:
            node = await self.core_v1_api.read_node(name=name)

            # Extract conditions into a more accessible format
            conditions = {}
            if node.status and node.status.conditions:
                for condition in node.status.conditions:
                    conditions[condition.type] = {
                        "status": condition.status,
                        "reason": condition.reason,
                        "message": condition.message,
                        "last_transition_time": condition.last_transition_time.isoformat()
                        if condition.last_transition_time
                        else None,
                        "last_heartbeat_time": condition.last_heartbeat_time.isoformat()
                        if condition.last_heartbeat_time
                        else None,
                    }

            # Get resource usage
            node_metrics = None
            try:
                metrics_api = client.CustomObjectsApi(self.k8s_client)
                node_metrics_list = await metrics_api.list_cluster_custom_object(
                    group="metrics.k8s.io", version="v1beta1", plural="nodes"
                )

                # Find metrics for this specific node
                if "items" in node_metrics_list:
                    for item in node_metrics_list["items"]:
                        if item.get("metadata", {}).get("name") == name:
                            node_metrics = item
                            break
            except Exception as metrics_err:
                logger.warning(f"Error retrieving node metrics: {metrics_err}")

            # Return combined data
            return {
                "conditions": conditions,
                "allocatable": node.status.allocatable if node.status else {},
                "capacity": node.status.capacity if node.status else {},
                "node_info": node.status.node_info.to_dict()
                if node.status and node.status.node_info
                else {},
                "metrics": node_metrics,
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_pvc_status(
        self, namespace: str, name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get status of PVCs in a namespace or a specific PVC.

        Args:
            namespace: Namespace to look in
            name: Optional name of a specific PVC

        Returns:
            Dictionary with PVC details and status
        """
        try:
            pvc_details = []

            if name:
                # Get a specific PVC
                try:
                    pvc = (
                        await self.core_v1_api.read_namespaced_persistent_volume_claim(
                            name=name, namespace=namespace
                        )
                    )

                    # Get events for this PVC
                    field_selector = f"involvedObject.name={name},involvedObject.kind=PersistentVolumeClaim"
                    events = await self.core_v1_api.list_namespaced_event(
                        namespace=namespace, field_selector=field_selector
                    )

                    pvc_info = pvc.to_dict()
                    pvc_info["events"] = [e.to_dict() for e in events.items]

                    # Get associated PV if bound
                    if pvc.spec.volume_name:
                        try:
                            pv = await self.core_v1_api.read_persistent_volume(
                                name=pvc.spec.volume_name
                            )
                            pvc_info["persistent_volume"] = pv.to_dict()
                        except client.ApiException as pv_err:
                            pvc_info["persistent_volume_error"] = (
                                f"{pv_err.status} - {pv_err.reason}"
                            )

                    pvc_details.append(
                        {
                            "name": pvc.metadata.name,
                            "namespace": pvc.metadata.namespace,
                            "phase": pvc.status.phase,
                            "storage_class": pvc.spec.storage_class_name,
                            "access_modes": pvc.spec.access_modes,
                            "volume_name": pvc.spec.volume_name,
                            "capacity": pvc.status.capacity.get("storage")
                            if pvc.status.capacity
                            else None,
                            "creation_time": pvc.metadata.creation_timestamp.isoformat()
                            if pvc.metadata.creation_timestamp
                            else None,
                            "details": pvc_info,
                        }
                    )
                except client.ApiException as pvc_err:
                    pvc_details.append(
                        {
                            "name": name,
                            "namespace": namespace,
                            "error": f"{pvc_err.status} - {pvc_err.reason}",
                        }
                    )
            else:
                # List all PVCs in the namespace
                pvcs = await self.core_v1_api.list_namespaced_persistent_volume_claim(
                    namespace=namespace
                )

                for pvc in pvcs.items:
                    pvc_details.append(
                        {
                            "name": pvc.metadata.name,
                            "namespace": pvc.metadata.namespace,
                            "phase": pvc.status.phase,
                            "storage_class": pvc.spec.storage_class_name,
                            "access_modes": pvc.spec.access_modes,
                            "volume_name": pvc.spec.volume_name,
                            "capacity": pvc.status.capacity.get("storage")
                            if pvc.status.capacity
                            else None,
                            "creation_time": pvc.metadata.creation_timestamp.isoformat()
                            if pvc.metadata.creation_timestamp
                            else None,
                        }
                    )

            return {"pvc_details": pvc_details, "count": len(pvc_details)}
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    def _get_selector_from_statefulset(self, sts) -> str:
        """Extract label selector from StatefulSet spec."""
        try:
            if sts.spec and sts.spec.selector and sts.spec.selector.match_labels:
                return ",".join(
                    [f"{k}={v}" for k, v in sts.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    def _get_selector_from_deployment(self, deploy) -> str:
        """Extract label selector from Deployment spec."""
        try:
            if (
                deploy.spec
                and deploy.spec.selector
                and deploy.spec.selector.match_labels
            ):
                return ",".join(
                    [f"{k}={v}" for k, v in deploy.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    def _get_selector_from_replicaset(self, rs) -> str:
        """Extract label selector from ReplicaSet spec."""
        try:
            if rs.spec and rs.spec.selector and rs.spec.selector.match_labels:
                return ",".join(
                    [f"{k}={v}" for k, v in rs.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    async def describe_pod(
        self, namespace: str, pod_name: Optional[str] = None, name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get detailed information about a pod."""
        # Allow either pod_name or name parameter
        pod_name = pod_name or name
        if not pod_name:
            return {"error": "Pod name not provided"}

        try:
            pod = await self.core_v1_api.read_namespaced_pod(
                name=pod_name, namespace=namespace
            )

            # Get pod events
            field_selector = f"involvedObject.name={pod_name}"
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            # Organize the information
            result = {
                "pod": pod.to_dict(),
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_statefulset(self, namespace: str, name: str) -> Dict[str, Any]:
        """Get detailed information about a StatefulSet."""
        try:
            sts = await self.apps_v1_api.read_namespaced_stateful_set(
                name=name, namespace=namespace
            )

            # Get associated pods
            label_selector = self._get_selector_from_statefulset(sts)
            pods = await self.core_v1_api.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )

            # Get events for the statefulset
            field_selector = (
                f"involvedObject.name={name},involvedObject.kind=StatefulSet"
            )
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            result = {
                "statefulset": sts.to_dict(),
                "pods": [p.to_dict() for p in pods.items],
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_deployment(self, namespace: str, name: str) -> Dict[str, Any]:
        """Get detailed information about a Deployment."""
        try:
            deploy = await self.apps_v1_api.read_namespaced_deployment(
                name=name, namespace=namespace
            )

            # Get associated pods via ReplicaSets
            label_selector = self._get_selector_from_deployment(deploy)
            rs_list = await self.apps_v1_api.list_namespaced_replica_set(
                namespace=namespace, label_selector=label_selector
            )

            replicasets = []
            pods = []

            for rs in rs_list.items:
                replicasets.append(rs.to_dict())

                # Get pods for this ReplicaSet
                rs_label_selector = self._get_selector_from_replicaset(rs)
                pod_list = await self.core_v1_api.list_namespaced_pod(
                    namespace=namespace, label_selector=rs_label_selector
                )
                pods.extend([p.to_dict() for p in pod_list.items])

            # Get events for the deployment
            field_selector = (
                f"involvedObject.name={name},involvedObject.kind=Deployment"
            )
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            result = {
                "deployment": deploy.to_dict(),
                "replicasets": replicasets,
                "pods": pods,
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_node(self, name: str) -> Dict[str, Any]:
        """Get detailed information about a node."""
        try:
            node = await self.core_v1_api.read_node(name=name)

            # Get pods running on this node
            field_selector = f"spec.nodeName={name}"
            pods = await self.core_v1_api.list_pod_for_all_namespaces(
                field_selector=field_selector
            )

            # Get events for the node
            field_selector = f"involvedObject.name={name},involvedObject.kind=Node"
            events = await self.core_v1_api.list_event_for_all_namespaces(
                field_selector=field_selector
            )

            result = {
                "node": node.to_dict(),
                "pods": [p.to_dict() for p in pods.items],
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_resource_usage(
        self,
        namespace: Optional[str] = None,
        entity_type: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get resource usage information for pods, deployments, statefulsets, or nodes.

        Args:
            namespace: Optional namespace to filter metrics
            entity_type: Optional entity type (pod, deployment, node, etc.) to filter results
            name: Optional name of the specific entity to get metrics for

        Returns:
            Dictionary with resource usage metrics
        """
        try:
            metrics_api = client.CustomObjectsApi(self.k8s_client)

            # Get pod metrics
            if namespace:
                pod_metrics = await metrics_api.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods",
                )
            else:
                pod_metrics = await metrics_api.list_cluster_custom_object(
                    group="metrics.k8s.io", version="v1beta1", plural="pods"
                )

            # Get node metrics
            node_metrics = await metrics_api.list_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="nodes"
            )

            # Filter metrics by entity type and name if provided
            result = {"pod_metrics": pod_metrics, "node_metrics": node_metrics}

            # Apply filtering if name and entity_type are provided
            if name and entity_type:
                if entity_type.lower() == "pod":
                    # Filter pod metrics by name
                    if "items" in pod_metrics:
                        result["pod_metrics"]["items"] = [
                            item
                            for item in pod_metrics["items"]
                            if item.get("metadata", {}).get("name") == name
                        ]
                elif entity_type.lower() == "node":
                    # Filter node metrics by name
                    if "items" in node_metrics:
                        result["node_metrics"]["items"] = [
                            item
                            for item in node_metrics["items"]
                            if item.get("metadata", {}).get("name") == name
                        ]
                elif entity_type.lower() in ["deployment", "statefulset", "daemonset"]:
                    # For higher-level resources, we need to find associated pods
                    # Get the pods belonging to this deployment/statefulset
                    if namespace:
                        try:
                            if entity_type.lower() == "deployment":
                                deploy = (
                                    await self.apps_v1_api.read_namespaced_deployment(
                                        name=name, namespace=namespace
                                    )
                                )
                                selector = self._get_selector_from_deployment(deploy)
                            elif entity_type.lower() == "statefulset":
                                sts = (
                                    await self.apps_v1_api.read_namespaced_stateful_set(
                                        name=name, namespace=namespace
                                    )
                                )
                                selector = self._get_selector_from_statefulset(sts)
                            else:  # daemonset
                                ds = await self.apps_v1_api.read_namespaced_daemon_set(
                                    name=name, namespace=namespace
                                )
                                selector = ",".join(
                                    [
                                        f"{k}={v}"
                                        for k, v in ds.spec.selector.match_labels.items()
                                    ]
                                )

                            # Filter pod metrics by selector
                            if "items" in pod_metrics and selector:
                                # We need to cross-reference with actual pods to find matches
                                pods = await self.core_v1_api.list_namespaced_pod(
                                    namespace=namespace, label_selector=selector
                                )
                                pod_names = [p.metadata.name for p in pods.items]

                                result["pod_metrics"]["items"] = [
                                    item
                                    for item in pod_metrics["items"]
                                    if item.get("metadata", {}).get("name") in pod_names
                                ]
                        except client.ApiException as e:
                            result["error"] = (
                                f"Error finding pods for {entity_type} {name}: {e.status} - {e.reason}"
                            )

            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_event_history(
        self, namespace: str, name: str, entity_type: str, limit: int = 50
    ) -> Dict[str, Any]:
        """Get recent events for a specific Kubernetes resource.

        Args:
            namespace: Namespace of the resource
            name: Name of the resource
            entity_type: Type of resource (pod, deployment, node, etc.)
            limit: Maximum number of events to return

        Returns:
            Dictionary with events and resource information
        """
        try:
            # Map entity_type to Kubernetes API object kind
            kind_mapping = {
                "pod": "Pod",
                "deployment": "Deployment",
                "statefulset": "StatefulSet",
                "daemonset": "DaemonSet",
                "service": "Service",
                "node": "Node",
                "persistentvolumeclaim": "PersistentVolumeClaim",
                "persistentvolume": "PersistentVolume",
                "configmap": "ConfigMap",
                "secret": "Secret",
                "job": "Job",
                "cronjob": "CronJob",
                "namespace": "Namespace",
                "horizontalpodautoscaler": "HorizontalPodAutoscaler",
                "ingress": "Ingress",
                "replicaset": "ReplicaSet",
            }

            # Default to exact match if not in mapping
            kind = kind_mapping.get(entity_type.lower(), entity_type)

            # Get events for the resource
            field_selector = f"involvedObject.name={name},involvedObject.kind={kind}"

            if entity_type.lower() == "node":
                events = await self.core_v1_api.list_event_for_all_namespaces(
                    field_selector=field_selector, limit=limit
                )
            else:
                events = await self.core_v1_api.list_namespaced_event(
                    namespace=namespace, field_selector=field_selector, limit=limit
                )

            # Get resource details
            resource_info = {}
            try:
                if entity_type.lower() == "pod":
                    resource = await self.core_v1_api.read_namespaced_pod(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "deployment":
                    resource = await self.apps_v1_api.read_namespaced_deployment(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "statefulset":
                    resource = await self.apps_v1_api.read_namespaced_stateful_set(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "node":
                    resource = await self.core_v1_api.read_node(name=name)
                    resource_info = resource.to_dict()
                # Add other entity types as needed
            except client.ApiException as e:
                resource_info = {
                    "error": f"Error retrieving {entity_type} details: {e.status} - {e.reason}"
                }

            return {
                "events": [e.to_dict() for e in events.items],
                "resource_info": resource_info,
                "resource_type": kind,
                "resource_name": name,
                "namespace": namespace,
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def list_events(
        self,
        namespace: str,
        resource_name: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List recent events for a namespace or specific resource."""
        try:
            field_selector = None
            if resource_name and kind:
                field_selector = (
                    f"involvedObject.name={resource_name},involvedObject.kind={kind}"
                )
            elif resource_name:
                field_selector = f"involvedObject.name={resource_name}"

            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector, limit=limit
            )

            return {"events": [e.to_dict() for e in events.items]}
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def exec_in_pod(
        self,
        namespace: str,
        pod_name: Optional[str] = None,
        name: Optional[str] = None,
        container: Optional[str] = None,
        command: List[str] = None,
    ) -> Dict[str, Any]:
        """Execute a command inside a pod container."""
        # Allow either pod_name or name parameter
        pod_name = pod_name or name
        if not pod_name:
            return {"error": "Pod name not provided", "output": ""}

        if not command:
            command = ["cat", "/proc/meminfo"]  # Default to a safe command

        try:
            # The kubernetes-asyncio client doesn't directly support exec
            # So we'll use the core API to construct the URL and then use websockets
            import kubernetes.stream as stream

            # Create a synchronous client temporarily
            from kubernetes import client as sync_client
            from kubernetes.config import load_kube_config

            # This is a workaround as kubernetes-asyncio doesn't support exec well
            # We'll use the synchronous client for this specific operation
            load_kube_config()
            sync_api = sync_client.CoreV1Api()

            # Execute the command
            exec_result = stream.stream(
                sync_api.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                container=container,
                command=command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            # Collect the output
            output = ""
            while exec_result.is_open():
                exec_result.update(timeout=1)
                if exec_result.peek_stdout():
                    output += exec_result.read_stdout()
                if exec_result.peek_stderr():
                    output += exec_result.read_stderr()

            exec_result.close()
            return {
                "output": output,
                "pod_name": pod_name,
                "namespace": namespace,
                "container": container,
                "command": command,
            }
        except Exception as e:
            return {
                "error": str(e),
                "output": "",
                "pod_name": pod_name,
                "namespace": namespace,
            }

    async def get_node_conditions(self, name: str) -> Dict[str, Any]:
        """Get the conditions of a specific node.

        Args:
            name: Name of the node

        Returns:
            Dictionary with node conditions and status
        """
        try:
            node = await self.core_v1_api.read_node(name=name)

            # Extract conditions into a more accessible format
            conditions = {}
            if node.status and node.status.conditions:
                for condition in node.status.conditions:
                    conditions[condition.type] = {
                        "status": condition.status,
                        "reason": condition.reason,
                        "message": condition.message,
                        "last_transition_time": condition.last_transition_time.isoformat()
                        if condition.last_transition_time
                        else None,
                        "last_heartbeat_time": condition.last_heartbeat_time.isoformat()
                        if condition.last_heartbeat_time
                        else None,
                    }

            # Get resource usage
            node_metrics = None
            try:
                metrics_api = client.CustomObjectsApi(self.k8s_client)
                node_metrics_list = await metrics_api.list_cluster_custom_object(
                    group="metrics.k8s.io", version="v1beta1", plural="nodes"
                )

                # Find metrics for this specific node
                if "items" in node_metrics_list:
                    for item in node_metrics_list["items"]:
                        if item.get("metadata", {}).get("name") == name:
                            node_metrics = item
                            break
            except Exception as metrics_err:
                logger.warning(f"Error retrieving node metrics: {metrics_err}")

            # Return combined data
            return {
                "conditions": conditions,
                "allocatable": node.status.allocatable if node.status else {},
                "capacity": node.status.capacity if node.status else {},
                "node_info": node.status.node_info.to_dict()
                if node.status and node.status.node_info
                else {},
                "metrics": node_metrics,
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_pvc_status(
        self, namespace: str, name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get status of PVCs in a namespace or a specific PVC.

        Args:
            namespace: Namespace to look in
            name: Optional name of a specific PVC

        Returns:
            Dictionary with PVC details and status
        """
        try:
            pvc_details = []

            if name:
                # Get a specific PVC
                try:
                    pvc = (
                        await self.core_v1_api.read_namespaced_persistent_volume_claim(
                            name=name, namespace=namespace
                        )
                    )

                    # Get events for this PVC
                    field_selector = f"involvedObject.name={name},involvedObject.kind=PersistentVolumeClaim"
                    events = await self.core_v1_api.list_namespaced_event(
                        namespace=namespace, field_selector=field_selector
                    )

                    pvc_info = pvc.to_dict()
                    pvc_info["events"] = [e.to_dict() for e in events.items]

                    # Get associated PV if bound
                    if pvc.spec.volume_name:
                        try:
                            pv = await self.core_v1_api.read_persistent_volume(
                                name=pvc.spec.volume_name
                            )
                            pvc_info["persistent_volume"] = pv.to_dict()
                        except client.ApiException as pv_err:
                            pvc_info["persistent_volume_error"] = (
                                f"{pv_err.status} - {pv_err.reason}"
                            )

                    pvc_details.append(
                        {
                            "name": pvc.metadata.name,
                            "namespace": pvc.metadata.namespace,
                            "phase": pvc.status.phase,
                            "storage_class": pvc.spec.storage_class_name,
                            "access_modes": pvc.spec.access_modes,
                            "volume_name": pvc.spec.volume_name,
                            "capacity": pvc.status.capacity.get("storage")
                            if pvc.status.capacity
                            else None,
                            "creation_time": pvc.metadata.creation_timestamp.isoformat()
                            if pvc.metadata.creation_timestamp
                            else None,
                            "details": pvc_info,
                        }
                    )
                except client.ApiException as pvc_err:
                    pvc_details.append(
                        {
                            "name": name,
                            "namespace": namespace,
                            "error": f"{pvc_err.status} - {pvc_err.reason}",
                        }
                    )
            else:
                # List all PVCs in the namespace
                pvcs = await self.core_v1_api.list_namespaced_persistent_volume_claim(
                    namespace=namespace
                )

                for pvc in pvcs.items:
                    pvc_details.append(
                        {
                            "name": pvc.metadata.name,
                            "namespace": pvc.metadata.namespace,
                            "phase": pvc.status.phase,
                            "storage_class": pvc.spec.storage_class_name,
                            "access_modes": pvc.spec.access_modes,
                            "volume_name": pvc.spec.volume_name,
                            "capacity": pvc.status.capacity.get("storage")
                            if pvc.status.capacity
                            else None,
                            "creation_time": pvc.metadata.creation_timestamp.isoformat()
                            if pvc.metadata.creation_timestamp
                            else None,
                        }
                    )

            return {"pvc_details": pvc_details, "count": len(pvc_details)}
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    def _get_selector_from_statefulset(self, sts) -> str:
        """Extract label selector from StatefulSet spec."""
        try:
            if sts.spec and sts.spec.selector and sts.spec.selector.match_labels:
                return ",".join(
                    [f"{k}={v}" for k, v in sts.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    def _get_selector_from_deployment(self, deploy) -> str:
        """Extract label selector from Deployment spec."""
        try:
            if (
                deploy.spec
                and deploy.spec.selector
                and deploy.spec.selector.match_labels
            ):
                return ",".join(
                    [f"{k}={v}" for k, v in deploy.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    def _get_selector_from_replicaset(self, rs) -> str:
        """Extract label selector from ReplicaSet spec."""
        try:
            if rs.spec and rs.spec.selector and rs.spec.selector.match_labels:
                return ",".join(
                    [f"{k}={v}" for k, v in rs.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    async def describe_pod(
        self, namespace: str, pod_name: Optional[str] = None, name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get detailed information about a pod."""
        # Allow either pod_name or name parameter
        pod_name = pod_name or name
        if not pod_name:
            return {"error": "Pod name not provided"}

        try:
            pod = await self.core_v1_api.read_namespaced_pod(
                name=pod_name, namespace=namespace
            )

            # Get pod events
            field_selector = f"involvedObject.name={pod_name}"
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            # Organize the information
            result = {
                "pod": pod.to_dict(),
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_statefulset(self, namespace: str, name: str) -> Dict[str, Any]:
        """Get detailed information about a StatefulSet."""
        try:
            sts = await self.apps_v1_api.read_namespaced_stateful_set(
                name=name, namespace=namespace
            )

            # Get associated pods
            label_selector = self._get_selector_from_statefulset(sts)
            pods = await self.core_v1_api.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )

            # Get events for the statefulset
            field_selector = (
                f"involvedObject.name={name},involvedObject.kind=StatefulSet"
            )
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            result = {
                "statefulset": sts.to_dict(),
                "pods": [p.to_dict() for p in pods.items],
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_deployment(self, namespace: str, name: str) -> Dict[str, Any]:
        """Get detailed information about a Deployment."""
        try:
            deploy = await self.apps_v1_api.read_namespaced_deployment(
                name=name, namespace=namespace
            )

            # Get associated pods via ReplicaSets
            label_selector = self._get_selector_from_deployment(deploy)
            rs_list = await self.apps_v1_api.list_namespaced_replica_set(
                namespace=namespace, label_selector=label_selector
            )

            replicasets = []
            pods = []

            for rs in rs_list.items:
                replicasets.append(rs.to_dict())

                # Get pods for this ReplicaSet
                rs_label_selector = self._get_selector_from_replicaset(rs)
                pod_list = await self.core_v1_api.list_namespaced_pod(
                    namespace=namespace, label_selector=rs_label_selector
                )
                pods.extend([p.to_dict() for p in pod_list.items])

            # Get events for the deployment
            field_selector = (
                f"involvedObject.name={name},involvedObject.kind=Deployment"
            )
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )

            result = {
                "deployment": deploy.to_dict(),
                "replicasets": replicasets,
                "pods": pods,
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def describe_node(self, name: str) -> Dict[str, Any]:
        """Get detailed information about a node."""
        try:
            node = await self.core_v1_api.read_node(name=name)

            # Get pods running on this node
            field_selector = f"spec.nodeName={name}"
            pods = await self.core_v1_api.list_pod_for_all_namespaces(
                field_selector=field_selector
            )

            # Get events for the node
            field_selector = f"involvedObject.name={name},involvedObject.kind=Node"
            events = await self.core_v1_api.list_event_for_all_namespaces(
                field_selector=field_selector
            )

            result = {
                "node": node.to_dict(),
                "pods": [p.to_dict() for p in pods.items],
                "events": [e.to_dict() for e in events.items],
            }
            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_resource_usage(
        self,
        namespace: Optional[str] = None,
        entity_type: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get resource usage information for pods, deployments, statefulsets, or nodes.

        Args:
            namespace: Optional namespace to filter metrics
            entity_type: Optional entity type (pod, deployment, node, etc.) to filter results
            name: Optional name of the specific entity to get metrics for

        Returns:
            Dictionary with resource usage metrics
        """
        try:
            metrics_api = client.CustomObjectsApi(self.k8s_client)

            # Get pod metrics
            if namespace:
                pod_metrics = await metrics_api.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods",
                )
            else:
                pod_metrics = await metrics_api.list_cluster_custom_object(
                    group="metrics.k8s.io", version="v1beta1", plural="pods"
                )

            # Get node metrics
            node_metrics = await metrics_api.list_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="nodes"
            )

            # Filter metrics by entity type and name if provided
            result = {"pod_metrics": pod_metrics, "node_metrics": node_metrics}

            # Apply filtering if name and entity_type are provided
            if name and entity_type:
                if entity_type.lower() == "pod":
                    # Filter pod metrics by name
                    if "items" in pod_metrics:
                        result["pod_metrics"]["items"] = [
                            item
                            for item in pod_metrics["items"]
                            if item.get("metadata", {}).get("name") == name
                        ]
                elif entity_type.lower() == "node":
                    # Filter node metrics by name
                    if "items" in node_metrics:
                        result["node_metrics"]["items"] = [
                            item
                            for item in node_metrics["items"]
                            if item.get("metadata", {}).get("name") == name
                        ]
                elif entity_type.lower() in ["deployment", "statefulset", "daemonset"]:
                    # For higher-level resources, we need to find associated pods
                    # Get the pods belonging to this deployment/statefulset
                    if namespace:
                        try:
                            if entity_type.lower() == "deployment":
                                deploy = (
                                    await self.apps_v1_api.read_namespaced_deployment(
                                        name=name, namespace=namespace
                                    )
                                )
                                selector = self._get_selector_from_deployment(deploy)
                            elif entity_type.lower() == "statefulset":
                                sts = (
                                    await self.apps_v1_api.read_namespaced_stateful_set(
                                        name=name, namespace=namespace
                                    )
                                )
                                selector = self._get_selector_from_statefulset(sts)
                            else:  # daemonset
                                ds = await self.apps_v1_api.read_namespaced_daemon_set(
                                    name=name, namespace=namespace
                                )
                                selector = ",".join(
                                    [
                                        f"{k}={v}"
                                        for k, v in ds.spec.selector.match_labels.items()
                                    ]
                                )

                            # Filter pod metrics by selector
                            if "items" in pod_metrics and selector:
                                # We need to cross-reference with actual pods to find matches
                                pods = await self.core_v1_api.list_namespaced_pod(
                                    namespace=namespace, label_selector=selector
                                )
                                pod_names = [p.metadata.name for p in pods.items]

                                result["pod_metrics"]["items"] = [
                                    item
                                    for item in pod_metrics["items"]
                                    if item.get("metadata", {}).get("name") in pod_names
                                ]
                        except client.ApiException as e:
                            result["error"] = (
                                f"Error finding pods for {entity_type} {name}: {e.status} - {e.reason}"
                            )

            return result
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_event_history(
        self, namespace: str, name: str, entity_type: str, limit: int = 50
    ) -> Dict[str, Any]:
        """Get recent events for a specific Kubernetes resource.

        Args:
            namespace: Namespace of the resource
            name: Name of the resource
            entity_type: Type of resource (pod, deployment, node, etc.)
            limit: Maximum number of events to return

        Returns:
            Dictionary with events and resource information
        """
        try:
            # Map entity_type to Kubernetes API object kind
            kind_mapping = {
                "pod": "Pod",
                "deployment": "Deployment",
                "statefulset": "StatefulSet",
                "daemonset": "DaemonSet",
                "service": "Service",
                "node": "Node",
                "persistentvolumeclaim": "PersistentVolumeClaim",
                "persistentvolume": "PersistentVolume",
                "configmap": "ConfigMap",
                "secret": "Secret",
                "job": "Job",
                "cronjob": "CronJob",
                "namespace": "Namespace",
                "horizontalpodautoscaler": "HorizontalPodAutoscaler",
                "ingress": "Ingress",
                "replicaset": "ReplicaSet",
            }

            # Default to exact match if not in mapping
            kind = kind_mapping.get(entity_type.lower(), entity_type)

            # Get events for the resource
            field_selector = f"involvedObject.name={name},involvedObject.kind={kind}"

            if entity_type.lower() == "node":
                events = await self.core_v1_api.list_event_for_all_namespaces(
                    field_selector=field_selector, limit=limit
                )
            else:
                events = await self.core_v1_api.list_namespaced_event(
                    namespace=namespace, field_selector=field_selector, limit=limit
                )

            # Get resource details
            resource_info = {}
            try:
                if entity_type.lower() == "pod":
                    resource = await self.core_v1_api.read_namespaced_pod(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "deployment":
                    resource = await self.apps_v1_api.read_namespaced_deployment(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "statefulset":
                    resource = await self.apps_v1_api.read_namespaced_stateful_set(
                        name=name, namespace=namespace
                    )
                    resource_info = resource.to_dict()
                elif entity_type.lower() == "node":
                    resource = await self.core_v1_api.read_node(name=name)
                    resource_info = resource.to_dict()
                # Add other entity types as needed
            except client.ApiException as e:
                resource_info = {
                    "error": f"Error retrieving {entity_type} details: {e.status} - {e.reason}"
                }

            return {
                "events": [e.to_dict() for e in events.items],
                "resource_info": resource_info,
                "resource_type": kind,
                "resource_name": name,
                "namespace": namespace,
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def list_events(
        self,
        namespace: str,
        resource_name: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List recent events for a namespace or specific resource."""
        try:
            field_selector = None
            if resource_name and kind:
                field_selector = (
                    f"involvedObject.name={resource_name},involvedObject.kind={kind}"
                )
            elif resource_name:
                field_selector = f"involvedObject.name={resource_name}"

            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector, limit=limit
            )

            return {"events": [e.to_dict() for e in events.items]}
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def exec_in_pod(
        self,
        namespace: str,
        pod_name: Optional[str] = None,
        name: Optional[str] = None,
        container: Optional[str] = None,
        command: List[str] = None,
    ) -> Dict[str, Any]:
        """Execute a command inside a pod container."""
        # Allow either pod_name or name parameter
        pod_name = pod_name or name
        if not pod_name:
            return {"error": "Pod name not provided", "output": ""}

        if not command:
            command = ["cat", "/proc/meminfo"]  # Default to a safe command

        try:
            # The kubernetes-asyncio client doesn't directly support exec
            # So we'll use the core API to construct the URL and then use websockets
            import kubernetes.stream as stream

            # Create a synchronous client temporarily
            from kubernetes import client as sync_client
            from kubernetes.config import load_kube_config

            # This is a workaround as kubernetes-asyncio doesn't support exec well
            # We'll use the synchronous client for this specific operation
            load_kube_config()
            sync_api = sync_client.CoreV1Api()

            # Execute the command
            exec_result = stream.stream(
                sync_api.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                container=container,
                command=command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            # Collect the output
            output = ""
            while exec_result.is_open():
                exec_result.update(timeout=1)
                if exec_result.peek_stdout():
                    output += exec_result.read_stdout()
                if exec_result.peek_stderr():
                    output += exec_result.read_stderr()

            exec_result.close()
            return {
                "output": output,
                "pod_name": pod_name,
                "namespace": namespace,
                "container": container,
                "command": command,
            }
        except Exception as e:
            return {
                "error": str(e),
                "output": "",
                "pod_name": pod_name,
                "namespace": namespace,
            }

    async def get_node_conditions(self, name: str) -> Dict[str, Any]:
        """Get the conditions of a specific node.

        Args:
            name: Name of the node

        Returns:
            Dictionary with node conditions and status
        """
        try:
            node = await self.core_v1_api.read_node(name=name)

            # Extract conditions into a more accessible format
            conditions = {}
            if node.status and node.status.conditions:
                for condition in node.status.conditions:
                    conditions[condition.type] = {
                        "status": condition.status,
                        "reason": condition.reason,
                        "message": condition.message,
                        "last_transition_time": condition.last_transition_time.isoformat()
                        if condition.last_transition_time
                        else None,
                        "last_heartbeat_time": condition.last_heartbeat_time.isoformat()
                        if condition.last_heartbeat_time
                        else None,
                    }

            # Get resource usage
            node_metrics = None
            try:
                metrics_api = client.CustomObjectsApi(self.k8s_client)
                node_metrics_list = await metrics_api.list_cluster_custom_object(
                    group="metrics.k8s.io", version="v1beta1", plural="nodes"
                )

                # Find metrics for this specific node
                if "items" in node_metrics_list:
                    for item in node_metrics_list["items"]:
                        if item.get("metadata", {}).get("name") == name:
                            node_metrics = item
                            break
            except Exception as metrics_err:
                logger.warning(f"Error retrieving node metrics: {metrics_err}")

            # Return combined data
            return {
                "conditions": conditions,
                "allocatable": node.status.allocatable if node.status else {},
                "capacity": node.status.capacity if node.status else {},
                "node_info": node.status.node_info.to_dict()
                if node.status and node.status.node_info
                else {},
                "metrics": node_metrics,
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_pvc_status(
        self, namespace: str, name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get status of PVCs in a namespace or a specific PVC.

        Args:
            namespace: Namespace to look in
            name: Optional name of a specific PVC

        Returns:
            Dictionary with PVC details and status
        """
        try:
            pvc_details = []

            if name:
                # Get a specific PVC
                try:
                    pvc = (
                        await self.core_v1_api.read_namespaced_persistent_volume_claim(
                            name=name, namespace=namespace
                        )
                    )

                    # Get events for this PVC
                    field_selector = f"involvedObject.name={name},involvedObject.kind=PersistentVolumeClaim"
                    events = await self.core_v1_api.list_namespaced_event(
                        namespace=namespace, field_selector=field_selector
                    )

                    pvc_info = pvc.to_dict()
                    pvc_info["events"] = [e.to_dict() for e in events.items]

                    # Get associated PV if bound
                    if pvc.spec.volume_name:
                        try:
                            pv = await self.core_v1_api.read_persistent_volume(
                                name=pvc.spec.volume_name
                            )
                            pvc_info["persistent_volume"] = pv.to_dict()
                        except client.ApiException as pv_err:
                            pvc_info["persistent_volume_error"] = (
                                f"{pv_err.status} - {pv_err.reason}"
                            )

                    pvc_details.append(
                        {
                            "name": pvc.metadata.name,
                            "namespace": pvc.metadata.namespace,
                            "phase": pvc.status.phase,
                            "storage_class": pvc.spec.storage_class_name,
                            "access_modes": pvc.spec.access_modes,
                            "volume_name": pvc.spec.volume_name,
                            "capacity": pvc.status.capacity.get("storage")
                            if pvc.status.capacity
                            else None,
                            "creation_time": pvc.metadata.creation_timestamp.isoformat()
                            if pvc.metadata.creation_timestamp
                            else None,
                            "details": pvc_info,
                        }
                    )
                except client.ApiException as pvc_err:
                    pvc_details.append(
                        {
                            "name": name,
                            "namespace": namespace,
                            "error": f"{pvc_err.status} - {pvc_err.reason}",
                        }
                    )
            else:
                # List all PVCs in the namespace
                pvcs = await self.core_v1_api.list_namespaced_persistent_volume_claim(
                    namespace=namespace
                )

                for pvc in pvcs.items:
                    pvc_details.append(
                        {
                            "name": pvc.metadata.name,
                            "namespace": pvc.metadata.namespace,
                            "phase": pvc.status.phase,
                            "storage_class": pvc.spec.storage_class_name,
                            "access_modes": pvc.spec.access_modes,
                            "volume_name": pvc.spec.volume_name,
                            "capacity": pvc.status.capacity.get("storage")
                            if pvc.status.capacity
                            else None,
                            "creation_time": pvc.metadata.creation_timestamp.isoformat()
                            if pvc.metadata.creation_timestamp
                            else None,
                        }
                    )

            return {"pvc_details": pvc_details, "count": len(pvc_details)}
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    def _get_selector_from_statefulset(self, sts) -> str:
        """Extract label selector from StatefulSet spec."""
        try:
            if sts.spec and sts.spec.selector and sts.spec.selector.match_labels:
                return ",".join(
                    [f"{k}={v}" for k, v in sts.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    def _get_selector_from_deployment(self, deploy) -> str:
        """Extract label selector from Deployment spec."""
        try:
            if (
                deploy.spec
                and deploy.spec.selector
                and deploy.spec.selector.match_labels
            ):
                return ",".join(
                    [f"{k}={v}" for k, v in deploy.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    def _get_selector_from_replicaset(self, rs) -> str:
        """Extract label selector from ReplicaSet spec."""
        try:
            if rs.spec and rs.spec.selector and rs.spec.selector.match_labels:
                return ",".join(
                    [f"{k}={v}" for k, v in rs.spec.selector.match_labels.items()]
                )
            return ""
        except Exception:
            return ""

    async def get_cluster_status(self) -> Dict[str, Any]:
        """Get overall Kubernetes cluster health status.
        
        Returns:
            Dictionary with components status, node conditions, and control plane health
        """
        try:
            # Get component statuses for control plane components
            component_statuses = []
            try:
                # Use core API to get component statuses
                components = await self.core_v1_api.list_component_status()
                for component in components.items:
                    component_statuses.append({
                        "name": component.metadata.name,
                        "conditions": [c.to_dict() for c in component.conditions] if component.conditions else [],
                        "healthy": all(c.status == "True" for c in component.conditions) if component.conditions else False
                    })
            except Exception as comp_err:
                logger.warning(f"Error retrieving component statuses: {comp_err}")
            
            # Get node health overview
            nodes = await self.core_v1_api.list_node()
            node_health = []
            for node in nodes.items:
                node_conditions = {}
                if node.status and node.status.conditions:
                    for condition in node.status.conditions:
                        node_conditions[condition.type] = {
                            "status": condition.status,
                            "message": condition.message
                        }
                
                ready = node_conditions.get("Ready", {}).get("status") == "True"
                node_health.append({
                    "name": node.metadata.name,
                    "ready": ready,
                    "conditions": node_conditions,
                    "taints": [t.to_dict() for t in node.spec.taints] if node.spec and node.spec.taints else []
                })
            
            # Check API server health via the readiness status
            api_server_health = {
                "working": True,  # If we got here, API server is working
                "message": "API server is responsive"
            }
            
            return {
                "components": component_statuses,
                "nodes": node_health,
                "api_server": api_server_health,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
        except Exception as e:
            return {"error": str(e)}

    async def analyze_pod_scheduling_problems(self, namespace: str, pod_name: str) -> Dict[str, Any]:
        """Analyzes why a pod might not be scheduling correctly.
        
        Args:
            namespace: Pod namespace
            pod_name: Pod name
            
        Returns:
            Dictionary with potential scheduling issues and recommendations
        """
        try:
            # Get the pod details
            pod = await self.core_v1_api.read_namespaced_pod(name=pod_name, namespace=namespace)
            
            # Check for specific status conditions indicating scheduling problems
            issues = []
            recommendations = []
            
            # Check pod phase
            if pod.status.phase == "Pending":
                # Check conditions
                if pod.status.conditions:
                    for condition in pod.status.conditions:
                        if condition.type == "PodScheduled" and condition.status == "False":
                            issues.append({
                                "type": "scheduling",
                                "reason": condition.reason,
                                "message": condition.message
                            })
                            
                            # Look for specific scheduling issues
                            if "Insufficient" in condition.message:
                                issues.append({
                                    "type": "resource_constraints",
                                    "message": "Pod cannot be scheduled due to insufficient resources"
                                })
                                if "cpu" in condition.message.lower():
                                    recommendations.append("Increase available CPU in the cluster or reduce pod CPU requests")
                                if "memory" in condition.message.lower():
                                    recommendations.append("Increase available memory in the cluster or reduce pod memory requests")
                                    
                            if "node(s) had taint" in condition.message.lower():
                                issues.append({
                                    "type": "node_taints",
                                    "message": "Nodes have taints that prevent scheduling"
                                })
                                recommendations.append("Add appropriate tolerations to the pod or remove taints from nodes")
                                
                            if "node(s) didn't match Pod's node affinity" in condition.message.lower():
                                issues.append({
                                    "type": "node_affinity",
                                    "message": "Pod's node affinity requirements not met by any node"
                                })
                                recommendations.append("Modify node affinity rules or add matching labels to nodes")
            
            # Get pod events to find scheduling errors
            field_selector = f"involvedObject.name={pod_name},involvedObject.kind=Pod"
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )
            
            scheduling_events = []
            for event in events.items:
                if event.reason in ["FailedScheduling", "FailedBinding", "OutOfcpu", "OutOfmemory", "NodeNotReady"]:
                    scheduling_events.append({
                        "reason": event.reason,
                        "message": event.message,
                        "count": event.count,
                        "time": event.last_timestamp.isoformat() if event.last_timestamp else None
                    })
                    
                    # Add specific recommendations based on event reason
                    if event.reason == "FailedScheduling" and "insufficient" in event.message.lower():
                        if "cpu" in event.message.lower():
                            recommendations.append("Add nodes with more CPU resources or reduce pod CPU requests")
                        if "memory" in event.message.lower():
                            recommendations.append("Add nodes with more memory or reduce pod memory requests")
                    
            # Check if pod has too high resource requests
            if pod.spec.containers:
                high_requests = False
                for container in pod.spec.containers:
                    if container.resources and container.resources.requests:
                        cpu_request = container.resources.requests.get("cpu")
                        memory_request = container.resources.requests.get("memory")
                        
                        # This is simplistic - ideally would compare against cluster capacity
                        if cpu_request and "1000m" in cpu_request:
                            high_requests = True
                        if memory_request and "4Gi" in memory_request:
                            high_requests = True
                
                if high_requests:
                    issues.append({
                        "type": "high_resource_requests",
                        "message": "Pod has high resource requests that might be difficult to satisfy"
                    })
                    recommendations.append("Consider lowering resource requests if possible")
            
            # Get nodes to check for capacity issues
            nodes = await self.core_v1_api.list_node()
            no_schedulable_nodes = True
            for node in nodes.items:
                if node.spec.unschedulable is not True:
                    no_schedulable_nodes = False
                    break
                    
            if no_schedulable_nodes:
                issues.append({
                    "type": "no_schedulable_nodes",
                    "message": "No schedulable nodes available in the cluster"
                })
                recommendations.append("Uncordon nodes or add new nodes to the cluster")
            
            return {
                "pod_name": pod_name,
                "namespace": namespace,
                "phase": pod.status.phase,
                "issues": issues,
                "scheduling_events": scheduling_events,
                "recommendations": recommendations
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def analyze_pod_crash_loop(self, namespace: str, pod_name: str) -> Dict[str, Any]:
        """Analyze a pod in crash loop to determine the root cause.

        Args:
            namespace: Pod namespace
            pod_name: Pod name
            
        Returns:
            Dictionary with crash analysis and potential solutions
        """
        try:
            # Get the pod details
            pod = await self.core_v1_api.read_namespaced_pod(name=pod_name, namespace=namespace)
            
            # Check container statuses
            container_analysis = []
            if pod.status.container_statuses:
                for container in pod.status.container_statuses:
                    analysis = {
                        "name": container.name,
                        "ready": container.ready,
                        "restart_count": container.restart_count,
                        "state": None,
                        "last_state": None,
                        "problem": None,
                        "exit_code": None,
                        "reason": None
                    }
                    
                    # Check current state
                    if container.state:
                        if container.state.waiting:
                            analysis["state"] = "waiting"
                            analysis["reason"] = container.state.waiting.reason
                            analysis["message"] = container.state.waiting.message
                        elif container.state.terminated:
                            analysis["state"] = "terminated"
                            analysis["exit_code"] = container.state.terminated.exit_code
                            analysis["reason"] = container.state.terminated.reason
                    
                    # Check last state to see why it might have failed
                    if container.last_state and container.last_state.terminated:
                        analysis["last_state"] = "terminated"
                        analysis["exit_code"] = container.last_state.terminated.exit_code
                        analysis["last_reason"] = container.last_state.terminated.reason
                        
                        # Analyze exit code
                        if container.last_state.terminated.exit_code == 1:
                            analysis["problem"] = "Application error"
                        elif container.last_state.terminated.exit_code == 137:
                            analysis["problem"] = "OOM killed"
                        elif container.last_state.terminated.exit_code == 143:
                            analysis["problem"] = "SIGTERM (graceful shutdown)"
                        elif container.last_state.terminated.exit_code == 139:
                            analysis["problem"] = "Segmentation fault"
                        elif container.last_state.terminated.exit_code != 0:
                            analysis["problem"] = f"Error exit code: {container.last_state.terminated.exit_code}"
                            
                    container_analysis.append(analysis)
            
            # Get pod logs if available
            logs_analysis = {}
            for container in container_analysis:
                try:
                    logs = await self.core_v1_api.read_namespaced_pod_log(
                        name=pod_name,
                        namespace=namespace,
                        container=container["name"],
                        tail_lines=100,
                        previous=True  # Get logs from previous container instance
                    )
                    
                    # Look for common error patterns in logs
                    error_indicators = ["exception", "error", "fatal", "panic", "failed", "denied"]
                    error_lines = []
                    for line in logs.splitlines():
                        if any(indicator in line.lower() for indicator in error_indicators):
                            error_lines.append(line)
                    
                    logs_analysis[container["name"]] = {
                        "has_logs": bool(logs),
                        "error_lines": error_lines[:5],  # Just include first 5 error lines
                        "error_count": len(error_lines)
                    }
                except Exception as log_err:
                    logs_analysis[container["name"]] = {
                        "has_logs": False,
                        "error": str(log_err)
                    }
            
            # Get pod events
            field_selector = f"involvedObject.name={pod_name},involvedObject.kind=Pod"
            events = await self.core_v1_api.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )
            
            crash_events = []
            for event in events.items:
                if event.reason in ["BackOff", "CrashLoopBackOff", "Failed", "Unhealthy", "FailedCreatePodContainer"]:
                    crash_events.append({
                        "reason": event.reason,
                        "message": event.message,
                        "count": event.count,
                        "time": event.last_timestamp.isoformat() if event.last_timestamp else None
                    })
            
            # Generate recommendations based on analysis
            recommendations = []
            
            # Check for OOM issues
            oom_detected = any(
                container.get("problem") == "OOM killed" or
                container.get("reason") == "OOMKilled"
                for container in container_analysis
            )
            
            if oom_detected:
                recommendations.append("Container is being killed due to exceeding memory limits. Consider increasing memory limits/requests.")
                recommendations.append("Check application for memory leaks or high memory usage patterns.")
            
            # Check for application errors
            app_error_detected = any(
                container.get("problem") == "Application error" or
                container.get("exit_code") == 1
                for container in container_analysis
            )
            
            if app_error_detected:
                recommendations.append("Application is exiting with an error code. Check application logs for error messages.")
                recommendations.append("Verify application configuration and environment variables.")
            
            # Check for persistent crashes
            high_restart_detected = any(
                container.get("restart_count", 0) > 5
                for container in container_analysis
            )
            
            if high_restart_detected:
                recommendations.append("Pod has high restart count. Consider checking for application bugs or configuration issues.")
            
            return {
                "pod_name": pod_name,
                "namespace": namespace,
                "phase": pod.status.phase,
                "container_analysis": container_analysis,
                "logs_analysis": logs_analysis,
                "crash_events": crash_events,
                "recommendations": recommendations
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def analyze_network_connectivity(self, namespace: str, source_pod: str, target: str, port: int = 80) -> Dict[str, Any]:
        """Analyze network connectivity issues between pods/services.
        
        Args:
            namespace: Pod namespace
            source_pod: Source pod name
            target: Target service/pod/hostname
            port: Target port number
            
        Returns:
            Dictionary with connectivity test results and analysis
        """
        try:
            # Use the exec API to run a network test from the source pod
            command = ["sh", "-c", f"wget -T 5 -O /dev/null -q {target}:{port} && echo 'Connection successful' || echo 'Connection failed'"]
            
            # Run the connectivity test command in the pod
            result = await self.exec_in_pod(
                namespace=namespace,
                pod_name=source_pod,
                command=command
            )
            
            # Check for DNS resolution issues
            dns_command = ["sh", "-c", f"nslookup {target}"]
            dns_result = await self.exec_in_pod(
                namespace=namespace,
                pod_name=source_pod,
                command=dns_command
            )
            
            # Check if target is a service
            service_details = None
            try:
                service = await self.core_v1_api.read_namespaced_service(
                    name=target, namespace=namespace
                )
                service_details = {
                    "name": service.metadata.name,
                    "type": service.spec.type,
                    "cluster_ip": service.spec.cluster_ip,
                    "ports": [p.to_dict() for p in service.spec.ports],
                    "selector": service.spec.selector
                }
                
                # For service targets, check if service has endpoints
                endpoints = await self.core_v1_api.read_namespaced_endpoints(
                    name=target, namespace=namespace
                )
                
                has_endpoints = False
                endpoint_count = 0
                if endpoints.subsets:
                    for subset in endpoints.subsets:
                        if subset.addresses:
                            has_endpoints = True
                            endpoint_count += len(subset.addresses)
                
                service_details["has_endpoints"] = has_endpoints
                service_details["endpoint_count"] = endpoint_count
                
                if not has_endpoints:
                    service_details["warning"] = "Service has no endpoints - check selector matches pod labels"
            except client.ApiException:
                # Not a service or service not found
                pass
            
            # Check network policies
            network_policies = []
            try:
                policies = await self.networking_v1_api.list_namespaced_network_policy(namespace=namespace)
                for policy in policies.items:
                    network_policies.append({
                        "name": policy.metadata.name,
                        "spec": policy.spec.to_dict()
                    })
            except Exception as np_err:
                logger.warning(f"Error retrieving network policies: {np_err}")
            
            # Analyze the results
            connectivity_issues = []
            if "Connection failed" in result.get("output", ""):
                connectivity_issues.append("Connection to target failed")
                
                # Check DNS issues
                if "server can't find" in dns_result.get("output", "") or "NXDOMAIN" in dns_result.get("output", ""):
                    connectivity_issues.append("DNS resolution failed for target")
                    
                # Check service issues
                if service_details and not service_details.get("has_endpoints", True):
                    connectivity_issues.append("Service has no endpoints - check pod selector matches")
                
                # Check network policy issues
                if network_policies:
                    connectivity_issues.append("Network policies exist in namespace - check if they're blocking traffic")
            
            # Generate recommendations
            recommendations = []
            if connectivity_issues:
                recommendations.append("Verify target service/pod exists and is running")
                recommendations.append("Check network policies to ensure traffic is allowed")
                recommendations.append("Ensure DNS resolution is working correctly")
                recommendations.append("Verify the target port is correct and the application is listening")
                
                if service_details and not service_details.get("has_endpoints", True):
                    recommendations.append("Check that service selector matches pod labels")
            
            return {
                "source_pod": source_pod,
                "target": target,
                "port": port,
                "namespace": namespace,
                "connection_successful": "Connection successful" in result.get("output", ""),
                "dns_result": dns_result.get("output", ""),
                "service_details": service_details,
                "network_policies": network_policies,
                "connectivity_issues": connectivity_issues,
                "recommendations": recommendations
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    async def analyze_service_endpoints(self, namespace: str, service_name: str) -> Dict[str, Any]:
        """Analyze why a service might not have endpoints or isn't routing traffic.
        
        Args:
            namespace: Service namespace
            service_name: Service name
            
        Returns:
            Dictionary with service analysis and recommendations
        """
        try:
            # Get the service details
            service = await self.core_v1_api.read_namespaced_service(
                name=service_name, namespace=namespace
            )
            
            # Get the endpoints
            endpoints = await self.core_v1_api.read_namespaced_endpoints(
                name=service_name, namespace=namespace
            )
            
            # Check if service has a selector
            has_selector = bool(service.spec.selector)
            
            # Check if service has endpoints
            has_endpoints = False
            endpoint_addresses = []
            
            if endpoints.subsets:
                for subset in endpoints.subsets:
                    if subset.addresses:
                        has_endpoints = True
                        for address in subset.addresses:
                            endpoint_info = {
                                "ip": address.ip,
                                "node": address.node_name,
                                "target_ref": None
                            }
                            
                            if address.target_ref:
                                endpoint_info["target_ref"] = {
                                    "kind": address.target_ref.kind,
                                    "name": address.target_ref.name,
                                    "namespace": address.target_ref.namespace
                                }
                            
                            endpoint_addresses.append(endpoint_info)
            
            # If service has a selector but no endpoints, find out why
            selector_issues = []
            matching_pods = []
            
            if has_selector and not has_endpoints:
                # Get pods matching the selector
                selector_string = ",".join([f"{k}={v}" for k, v in service.spec.selector.items()])
                
                try:
                    pods = await self.core_v1_api.list_namespaced_pod(
                        namespace=namespace, label_selector=selector_string
                    )
                    
                    if not pods.items:
                        selector_issues.append("No pods match the service selector labels")
                    else:
                        # Pods exist but no endpoints, check if they're ready
                        for pod in pods.items:
                            pod_ready = False
                            if pod.status.conditions:
                                for condition in pod.status.conditions:
                                    if condition.type == "Ready" and condition.status == "True":
                                        pod_ready = True
                                        break
                            
                            matching_pods.append({
                                "name": pod.metadata.name,
                                "ready": pod_ready,
                                "phase": pod.status.phase,
                                "pod_ip": pod.status.pod_ip
                            })
                            
                            if pod.status.phase != "Running":
                                selector_issues.append(f"Pod {pod.metadata.name} matching selector is not running (status: {pod.status.phase})")
                            elif not pod_ready:
                                selector_issues.append(f"Pod {pod.metadata.name} matching selector is not ready")
                except Exception as pod_err:
                    selector_issues.append(f"Error retrieving matching pods: {pod_err}")
            
            # Check service ports
            port_issues = []
            if service.spec.ports:
                for port in service.spec.ports:
                    if not port.target_port:
                        port_issues.append(f"Service port {port.port} has no target port specified")
            else:
                port_issues.append("Service has no ports defined")
            
            # Get events for the service
            service_events = []
            try:
                field_selector = f"involvedObject.name={service_name},involvedObject.kind=Service"
                events = await self.core_v1_api.list_namespaced_event(
                    namespace=namespace, field_selector=field_selector
                )
                
                for event in events.items:
                    service_events.append({
                        "type": event.type,
                        "reason": event.reason,
                        "message": event.message,
                        "time": event.last_timestamp.isoformat() if event.last_timestamp else None
                    })
            except Exception as event_err:
                logger.warning(f"Error retrieving service events: {event_err}")
            
            # Generate recommendations
            recommendations = []
            
            if not has_selector:
                recommendations.append("Service has no selector. If this is intentional (for headless service or external name), ignore. Otherwise, add a selector.")
            
            if has_selector and not has_endpoints:
                recommendations.append("Ensure pods with matching labels exist and are running")
                recommendations.append("Check pod readiness - pods must be ready to become endpoints")
                
                if matching_pods:
                    non_ready_pods = [p for p in matching_pods if not p["ready"]]
                    if non_ready_pods:
                        recommendations.append("Fix readiness issues in matching pods")
            
            if port_issues:
                recommendations.append("Verify service port and targetPort configuration")
            
            return {
                "service_name": service_name,
                "namespace": namespace,
                "service_type": service.spec.type,
                "cluster_ip": service.spec.cluster_ip,
                "selector": service.spec.selector,
                "has_selector": has_selector,
                "has_endpoints": has_endpoints,
                "endpoint_count": len(endpoint_addresses),
                "endpoints": endpoint_addresses,
                "matching_pods": matching_pods,
                "selector_issues": selector_issues,
                "port_issues": port_issues,
                "events": service_events,
                "recommendations": recommendations
            }
        except client.ApiException as e:
            return {"error": f"{e.status} - {e.reason}"}
        except Exception as e:
            return {"error": str(e)}


def clean_json_response(json_str: str) -> str:
    """
    Clean potentially malformed JSON returned by Gemini AI.
    
    Args:
        json_str: The JSON string to clean
        
    Returns:
        Cleaned JSON string, or the original string if it cannot be reliably cleaned.
    """
    logger.debug(f"Original JSON string from AI (first 300 chars): {json_str[:300]}")

    # 1. Remove any non-JSON content before the first opening brace/bracket
    # and after the last closing brace/bracket.
    start_idx_brace = json_str.find('{')
    start_idx_bracket = json_str.find('[')

    if start_idx_brace == -1 and start_idx_bracket == -1:
        logger.warning("No JSON object or array found in AI response.")
        return json_str # No JSON structure detected

    if start_idx_brace == -1:
        start_idx = start_idx_bracket
    elif start_idx_bracket == -1:
        start_idx = start_idx_brace
    else:
        start_idx = min(start_idx_brace, start_idx_bracket)
    
    json_str = json_str[start_idx:]
    logger.debug(f"After removing prefix (first 300 chars): {json_str[:300]}")
    
    end_idx = json_str.rfind('}')
    end_idx_bracket = json_str.rfind(']')
    end_idx = max(end_idx, end_idx_bracket)

    if end_idx == -1 :
        logger.warning("No closing brace or bracket found in potentially trimmed AI response.")
        # If we found an opening but no closing, it's likely malformed.
        # Return original if it looks like it wasn't meant to be JSON from the start.
        if start_idx > 20 : # Heuristic: if JSON markers are deep in the string, it might be an error.
            return json_str

    json_str = json_str[:end_idx+1]
    logger.debug(f"After removing suffix (first 300 chars): {json_str[:300]}")
    
    # Fix common JSON errors
    
    # 2. Ensure property names (keys) have double quotes
    json_str = re.sub(r'([{,]\s*)(\w+)(\s*:)', r'\1"\2"\3', json_str)
    # For keys at the beginning of the object: {key: value}
    json_str = re.sub(r'({\s*)(\w+)(\s*:)', r'\1"\2"\3', json_str)
    logger.debug(f"After quoting keys (first 300 chars): {json_str[:300]}")
    
    # 3. Attempt to fix unquoted string values. This is complex.
    # Targets: `: value` (no quotes, not a number/bool/null/object/array)
    # Example: `key: unquoted string,` or `key: unquoted string}`
    json_str = re.sub(r':\s*([a-zA-Z_][\w\s.-]*[a-zA-Z\d_])\s*([,}\]])', r': "\1"\2', json_str)
    logger.debug(f"After attempting to quote string values (first 300 chars): {json_str[:300]}")

    # 4. Add missing commas between key-value pairs
    # "value" "key": -> "value", "key":
    json_str = re.sub(r'(?<![,\{\[])\s*"\s*:\s*(?:(?:".*?")|(?:\d+(?:\.\d+)?)|(?:true|false|null)|(?:\{.*?\})|(?:\[.*?\]))\s*(")\s*:', r'\g<0>'.replace(r'"\s*:', r'", " :'), json_str, flags=re.DOTALL)
    # Simplified: ending quote of a value, whitespace, opening quote of a new key
    json_str = re.sub(r'(["\w\dtruefalsenull])\s+("[\w\d\s]+":)', r'\1, \2', json_str)
    logger.debug(f"After adding missing commas between KV pairs (first 300 chars): {json_str[:300]}")

    # 5. Add missing commas between array elements
    # "elem1" "elem2" -> "elem1", "elem2"
    json_str = re.sub(r'(")\s+(")', r'\1, \2', json_str)
    # {obj1} {obj2} -> {obj1}, {obj2}
    json_str = re.sub(r'(\})\s+(\{)', r'\1, \2', json_str)
    # num1 num2 -> num1, num2
    json_str = re.sub(r'(\d)\s+(\d)', r'\1, \2', json_str)
    # "elem" {obj} -> "elem", {obj}
    json_str = re.sub(r'(")\s+(\{)', r'\1, \2', json_str)
    json_str = re.sub(r'(\})\s+(")', r'\1, \2', json_str)
    logger.debug(f"After adding missing commas in arrays (first 300 chars): {json_str[:300]}")
    
    # 6. Remove trailing commas
    json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
    logger.debug(f"After removing trailing commas (first 300 chars): {json_str[:300]}")
    
    # 7. Balance braces and brackets (simplistic)
    open_braces = json_str.count('{')
    close_braces = json_str.count('}')
    if open_braces > close_braces:
        json_str += "}" * (open_braces - close_braces)
    elif open_braces < close_braces:
        # This is harder to fix correctly with regex, might indicate severe malformation
        logger.warning(f"JSON has more closing braces ({close_braces}) than opening ({open_braces}). Cleaning might be unreliable.")
    logger.debug(f"After balancing braces (first 300 chars): {json_str[:300]}")
    
    return json_str


@with_exponential_backoff() # Apply the decorator
async def generate_remediation_plan(
    anomaly_record: AnomalyRecord,
    dependencies: PlannerDependencies,
    agent: Optional[Agent] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Optional[RemediationPlan]:
    """
    Generate a remediation plan for an anomaly using Gemini or static plans.

    Args:
        anomaly_record: The anomaly record to generate a plan for
        dependencies: Planner dependencies containing db, k8s_client, and diagnostic_tools
        agent: Optional Gemini agent (if not provided, falls back to static plans)
        context: Optional additional context about the anomaly and diagnosis

    Returns:
        RemediationPlan if successful, None if generation fails
    """
    if not anomaly_record:
        logger.error("Cannot generate remediation plan: No anomaly record provided")
        return None

    # Ensure we have a valid anomaly ID
    anomaly_id = anomaly_record.id
    if not anomaly_id:
        logger.error("Cannot generate remediation plan: Anomaly record has no ID")
        return None

    # Extract entity info for use in the plan
    entity_id = anomaly_record.entity_id or ""
    entity_type = anomaly_record.entity_type or ""
    namespace = anomaly_record.namespace or "default"
    name = anomaly_record.name or ""

    # If name is empty but entity_id contains namespace/name format, extract them
    if not name and "/" in entity_id:
        try:
            namespace, name = entity_id.split("/", 1)
        except ValueError:
            name = entity_id

    logger.info(
        f"Generating remediation plan for anomaly: {anomaly_id} ({anomaly_record.failure_reason})"
    )

    # 1. First, try to generate a plan using Gemini AI if agent is available
    if agent:
        try:
            # Fetch additional context for the anomaly
            recent_events = await get_recent_events(dependencies.db, anomaly_record)
            resource_info, owner_info = await get_resource_metadata(
                dependencies.k8s_client, 
                name=name, 
                kind=entity_type, 
                namespace=namespace
            )

            # Collect diagnostic information for analysis
            diagnostic_data = {}
            root_cause_analysis = {}
            
            if dependencies.diagnostic_tools:
                try:
                    # Identify the proper entity to diagnose
                    if entity_type.lower() == "pod":
                        diagnostic_data[
                            "pod_logs"
                        ] = await dependencies.diagnostic_tools.get_pod_logs(
                            namespace, name
                        )
                        diagnostic_data[
                            "pod_details"
                        ] = await dependencies.diagnostic_tools.describe_pod(
                            namespace, name
                        )
                        
                        # Advanced pod diagnostics
                        if anomaly_record.failure_reason in ["CrashLoopBackOff", "EVENT_CRASHLOOPBACKOFF_COUNT"]:
                            root_cause_analysis["crash_analysis"] = await dependencies.diagnostic_tools.analyze_pod_crash_loop(
                                namespace, name
                            )
                        elif anomaly_record.failure_reason in ["Pending", "ContainerCreating"]:
                            root_cause_analysis["scheduling_analysis"] = await dependencies.diagnostic_tools.analyze_pod_scheduling_problems(
                            namespace, name
                        )

                    elif entity_type.lower() in ["statefulset", "sts"]:
                        diagnostic_data[
                            "statefulset_details"
                        ] = await dependencies.diagnostic_tools.describe_statefulset(
                            namespace, name
                        )

                        # Also get logs from pods managed by this StatefulSet
                        pod_logs = {}
                        if "pods" in diagnostic_data["statefulset_details"]:
                            for pod in diagnostic_data["statefulset_details"]["pods"][
                                :3
                            ]:  # Limit to 3 pods
                                pod_name = pod.get("metadata", {}).get("name", "")
                                if pod_name:
                                    pod_logs[
                                        pod_name
                                    ] = await dependencies.diagnostic_tools.get_pod_logs(
                                        namespace, pod_name
                                    )
                        diagnostic_data["pod_logs"] = pod_logs

                    elif entity_type.lower() in ["deployment", "deploy"]:
                        diagnostic_data[
                            "deployment_details"
                        ] = await dependencies.diagnostic_tools.describe_deployment(
                            namespace, name
                        )

                        # Get logs from some pods managed by this Deployment
                        pod_logs = {}
                        if "pods" in diagnostic_data["deployment_details"]:
                            for pod in diagnostic_data["deployment_details"]["pods"][
                                :3
                            ]:  # Limit to 3 pods
                                pod_name = pod.get("metadata", {}).get("name", "")
                                if pod_name:
                                    pod_logs[
                                        pod_name
                                    ] = await dependencies.diagnostic_tools.get_pod_logs(
                                        namespace, pod_name
                                    )
                        diagnostic_data["pod_logs"] = pod_logs

                    elif entity_type.lower() == "node":
                        diagnostic_data[
                            "node_details"
                        ] = await dependencies.diagnostic_tools.describe_node(name)
                        
                        # Get node conditions for detailed analysis
                        root_cause_analysis["node_condition_analysis"] = await dependencies.diagnostic_tools.get_node_conditions(name)
                    
                    elif entity_type.lower() == "service":
                        # Get detailed service analysis
                        root_cause_analysis["service_analysis"] = await dependencies.diagnostic_tools.analyze_service_endpoints(
                            namespace, name
                        )
                    
                    # Always get cluster status for context
                    root_cause_analysis["cluster_status"] = await dependencies.diagnostic_tools.get_cluster_status()

                    # Always get recent events in the namespace
                    diagnostic_data[
                        "recent_events"
                    ] = await dependencies.diagnostic_tools.get_event_history(
                        namespace, name, entity_type
                    )

                    # Try to get resource usage metrics
                    try:
                        diagnostic_data[
                            "resource_usage"
                        ] = await dependencies.diagnostic_tools.get_resource_usage(
                            namespace, entity_type, name
                        )
                    except Exception as usage_err:
                        logger.warning(
                            f"Failed to get resource usage metrics: {usage_err}"
                        )

                    logger.info(
                        f"Successfully collected diagnostic data for {entity_type}/{name}"
                    )

                except Exception as diag_err:
                    logger.error(f"Error collecting diagnostic data: {diag_err}")
                    # Continue without diagnostic data

            # Structure the input data for the Gemini model
            model_input = {
                "anomaly": anomaly_record.model_dump(),
                "recent_events": recent_events,
                "resource_info": resource_info or {},
                "owner_info": owner_info or {},
                "diagnostic_data": diagnostic_data,
                "root_cause_analysis": root_cause_analysis
            }

            try:
                # Make the actual API call to Gemini
                start_time = time.monotonic()

                # Format the input for Gemini API properly
                # Enhanced prompt to guide namespace and name usage
                prompt_text = (
                    f"""Generate a remediation plan for a Kubernetes anomaly.
Context for the anomaly:
- Affected Entity ID: {entity_id}
- Affected Entity Name: {name}
- Affected Namespace: {namespace}
- Affected Entity Type: {entity_type}
- Anomaly Type/Failure Reason: {anomaly_record.failure_reason}
- Metric Name (if applicable): {anomaly_record.metric_name}
- Metric Value (if applicable): {anomaly_record.metric_value}

When generating actions that target the primary affected entity detailed above (Name: '{name}', Namespace: '{namespace}'),
YOU MUST use the EXACT 'Affected Entity Name' for the 'name' parameter and the
EXACT 'Affected Namespace' for the 'namespace' parameter in the action.
For example, if the action is 'delete_pod' for this affected pod, its parameters MUST be
{{ \"name\": \"{name}\", \"namespace\": \"{namespace}\" }}.
Do NOT use a different name or namespace for the primary affected entity unless explicitly
instructed by a diagnostic finding that points to a different related resource.

Available Remediation Actions (use ONLY these action_type values):
- action_type: "scale_deployment", parameters: {{ \"name\": \"<deployment_name>\", \"replicas\": <integer>, \"namespace\": \"<namespace>\" }}
- action_type: "delete_pod", parameters: {{ \"name\": \"<pod_name>\", \"namespace\": \"<namespace>\" }}
- action_type: "restart_deployment", parameters: {{ \"name\": \"<deployment_name>\", \"namespace\": \"<namespace>\" }}
- action_type: "restart_statefulset", parameters: {{ \"name\": \"<statefulset_name>\", \"namespace\": \"<namespace>\" }}
- action_type: "restart_daemonset", parameters: {{ \"name\": \"<daemonset_name>\", \"namespace\": \"<namespace>\" }}
- action_type: "drain_node", parameters: {{ \"name\": \"<node_name>\", \"grace_period_seconds\": <integer>, \"force\": <boolean> }}
- action_type: "scale_statefulset", parameters: {{ \"name\": \"<statefulset_name>\", \"replicas\": <integer>, \"namespace\": \"<namespace>\" }}
- action_type: "taint_node", parameters: {{ \"name\": \"<node_name>\", \"key\": \"<taint_key>\", \"value\": \"<taint_value>\", \"effect\": \"<NoSchedule|PreferNoSchedule|NoExecute>\" }}
- action_type: "evict_pod", parameters: {{ \"name\": \"<pod_name>\", \"namespace\": \"<namespace>\", \"grace_period_seconds\": <integer> }}
- action_type: "vertical_scale_deployment", parameters: {{ \"name\": \"<deployment_name>\", \"namespace\": \"<namespace>\", \"container\": \"<container_name>\", \"resource\": \"<cpu|memory>\", \"value\": \"<resource_value>\" }}
- action_type: "vertical_scale_statefulset", parameters: {{ \"name\": \"<statefulset_name>\", \"namespace\": \"<namespace>\", \"container\": \"<container_name>\", \"resource\": \"<cpu|memory>\", \"value\": \"<resource_value>\" }}
- action_type: "cordon_node", parameters: {{ \"name\": \"<node_name>\" }}
- action_type: "uncordon_node", parameters: {{ \"name\": \"<node_name>\" }}
- action_type: "restart_cronjob", parameters: {{ \"name\": \"<cronjob_name>\", \"namespace\": \"<namespace>\" }}
- action_type: "restart_job", parameters: {{ \"name\": \"<job_name>\", \"namespace\": \"<namespace>\" }}
- action_type: "update_configmap", parameters: {{ \"name\": \"<configmap_name>\", \"namespace\": \"<namespace>\", \"data\": {{\"key\": \"value\"}} }}
- action_type: "update_secret", parameters: {{ \"name\": \"<secret_name>\", \"namespace\": \"<namespace>\", \"data\": {{\"key\": \"base64_encoded_value\"}} }}
- action_type: "rollback_deployment", parameters: {{ \"name\": \"<deployment_name>\", \"namespace\": \"<namespace>\", \"revision\": <revision_number> }}
- action_type: "fix_service_selectors", parameters: {{ \"name\": \"<service_name>\", \"namespace\": \"<namespace>\", \"selectors\": {{\"key\": \"value\"}} }}
- action_type: "update_resource_limits", parameters: {{ \"name\": \"<resource_name>\", \"namespace\": \"<namespace>\", \"kind\": \"<Pod|Deployment|StatefulSet|DaemonSet>\", \"container\": \"<container_name>\", \"limits\": {{\"cpu\": \"<value>\", \"memory\": \"<value>\"}}, \"requests\": {{\"cpu\": \"<value>\", \"memory\": \"<value>\"}} }}
- action_type: "force_delete_pod", parameters: {{ \"name\": \"<pod_name>\", \"namespace\": \"<namespace>\" }}
- action_type: "update_container_image", parameters: {{ \"name\": \"<resource_name>\", \"namespace\": \"<namespace>\", \"kind\": \"<Deployment|StatefulSet|DaemonSet>\", \"container\": \"<container_name>\", \"image\": \"<image_path>:<tag>\" }}
- action_type: "fix_dns_config", parameters: {{ \"name\": \"<resource_name>\", \"namespace\": \"<namespace>\", \"kind\": \"<Pod|Deployment|StatefulSet|DaemonSet>\", \"dns_policy\": \"<ClusterFirst|Default|None>\", \"nameservers\": [\"<nameserver_ip>\"] }}
- action_type: "manual_intervention", parameters: {{ \"reason\": \"<reason_string>\", \"instructions\": \"<instructions_string>\" }}

IMPORTANT: Use only action_type values from the list above. Do not use any other action types not listed here.

Diagnostic Information:
The system has collected detailed diagnostic information about the affected resource including logs, events, configuration, status, and root cause analysis. This data has been analyzed to provide context about the anomaly.

Key findings from diagnostic data:
"""
                )
                # Add key findings from diagnostic data if available
                if diagnostic_data or root_cause_analysis:
                    findings = []

                    # Add pod-related findings
                    if "pod_logs" in diagnostic_data and isinstance(
                        diagnostic_data["pod_logs"], dict
                    ):
                        for pod_name, logs in diagnostic_data["pod_logs"].items():
                            if isinstance(logs, str):
                                # Look for common error patterns in the logs
                                error_lines = []
                                for line in logs.splitlines()[
                                    -20:
                                ]:  # Look at last 20 lines
                                    if any(
                                        err_term in line.lower()
                                        for err_term in [
                                            "error",
                                            "exception",
                                            "fail",
                                            "unable",
                                            "denied",
                                            "cannot",
                                            "timeout",
                                        ]
                                    ):
                                        error_lines.append(line)

                                if error_lines:
                                    findings.append(
                                        f"Pod {pod_name} logs contain errors: "
                                        + "; ".join(error_lines[:3])
                                    )

                    # Add crash analysis findings
                    if "crash_analysis" in root_cause_analysis:
                        crash_analysis = root_cause_analysis["crash_analysis"]
                        
                        if "container_analysis" in crash_analysis:
                            for container in crash_analysis["container_analysis"]:
                                if "problem" in container and container["problem"]:
                                    findings.append(
                                        f"Container {container.get('name', 'unknown')} issue: {container['problem']}, " +
                                        f"Exit code: {container.get('exit_code', 'unknown')}, " +
                                        f"Restart count: {container.get('restart_count', 0)}"
                                    )
                                
                        if "recommendations" in crash_analysis and crash_analysis["recommendations"]:
                            for recommendation in crash_analysis["recommendations"][:3]:
                                findings.append(f"Crash analysis recommends: {recommendation}")
                    
                    # Add scheduling analysis findings
                    if "scheduling_analysis" in root_cause_analysis:
                        scheduling = root_cause_analysis["scheduling_analysis"]
                        
                        if "issues" in scheduling and scheduling["issues"]:
                            for issue in scheduling["issues"]:
                                findings.append(
                                    f"Scheduling issue: {issue.get('type', 'unknown')} - {issue.get('message', 'No message')}"
                                )
                                
                        if "recommendations" in scheduling and scheduling["recommendations"]:
                            for recommendation in scheduling["recommendations"][:3]:
                                findings.append(f"Scheduling analysis recommends: {recommendation}")
                    
                    # Add service analysis findings
                    if "service_analysis" in root_cause_analysis:
                        service = root_cause_analysis["service_analysis"]
                        
                        if service.get("has_endpoints") is False:
                            findings.append(f"Service {service.get('service_name', 'unknown')} has no endpoints")
                        
                        if "selector_issues" in service and service["selector_issues"]:
                            for issue in service["selector_issues"][:3]:
                                findings.append(f"Service selector issue: {issue}")
                                
                        if "recommendations" in service and service["recommendations"]:
                            for recommendation in service["recommendations"][:3]:
                                findings.append(f"Service analysis recommends: {recommendation}")

                    # Add event-related findings
                    if (
                        "recent_events" in diagnostic_data
                        and "events" in diagnostic_data["recent_events"]
                    ):
                        warning_events = []
                        for event in diagnostic_data["recent_events"]["events"]:
                            if event.get("type") == "Warning":
                                warning_events.append(
                                    f"{event.get('reason')}: {event.get('message')}"
                                )

                        if warning_events:
                            findings.append(
                                "Warning events: " + "; ".join(warning_events[:5])
                            )

                    # Add StatefulSet-specific findings
                    if "statefulset_details" in diagnostic_data:
                        sts_data = diagnostic_data["statefulset_details"].get(
                            "statefulset", {}
                        )

                        # Check replicas status
                        spec_replicas = sts_data.get("spec", {}).get("replicas", 0)
                        status_replicas = sts_data.get("status", {}).get("replicas", 0)
                        ready_replicas = sts_data.get("status", {}).get(
                            "readyReplicas", 0
                        )

                        if ready_replicas < spec_replicas:
                            findings.append(
                                f"StatefulSet has {ready_replicas}/{spec_replicas} ready replicas"
                            )

                    # Add resource usage findings if available
                    if (
                        "resource_usage" in diagnostic_data
                        and "pod_metrics" in diagnostic_data["resource_usage"]
                    ):
                        pod_metrics = diagnostic_data["resource_usage"]["pod_metrics"]
                        if "items" in pod_metrics:
                            high_usage_pods = []
                            for pod_metric in pod_metrics["items"]:
                                pod_name = pod_metric.get("metadata", {}).get(
                                    "name", ""
                                )
                                containers = pod_metric.get("containers", [])

                                for container in containers:
                                    container_name = container.get("name", "")
                                    usage = container.get("usage", {})

                                    # Check CPU usage (in nanocores)
                                    cpu_usage = usage.get("cpu", "")
                                    if "m" in cpu_usage:  # millicores
                                        cpu_value = int(cpu_usage.replace("m", ""))
                                        if cpu_value > 800:  # More than 800m is high
                                            high_usage_pods.append(
                                                f"{pod_name}/{container_name} high CPU: {cpu_usage}"
                                            )

                                    # Check memory usage (in bytes)
                                    mem_usage = usage.get("memory", "")
                                    if "Mi" in mem_usage:  # Mebibytes
                                        mem_value = int(mem_usage.replace("Mi", ""))
                                        if mem_value > 1024:  # More than 1GiB is high
                                            high_usage_pods.append(
                                                f"{pod_name}/{container_name} high memory: {mem_usage}"
                                            )

                            if high_usage_pods:
                                findings.append(
                                    "High resource usage detected: "
                                    + "; ".join(high_usage_pods[:3])
                                )
                    
                    # Add node condition findings
                    if "node_condition_analysis" in root_cause_analysis:
                        node_conditions = root_cause_analysis["node_condition_analysis"].get("conditions", {})
                        
                        for condition_type, condition in node_conditions.items():
                            if condition_type == "Ready" and condition.get("status") != "True":
                                findings.append(f"Node not Ready: {condition.get('reason')} - {condition.get('message', 'No message')}")
                            elif condition_type != "Ready" and condition.get("status") == "True":
                                findings.append(f"Node condition {condition_type}: {condition.get('reason')} - {condition.get('message', 'No message')}")
                    
                    # Add cluster status findings
                    if "cluster_status" in root_cause_analysis:
                        cluster = root_cause_analysis["cluster_status"]
                        
                        # Check component health
                        unhealthy_components = []
                        for component in cluster.get("components", []):
                            if not component.get("healthy", True):
                                unhealthy_components.append(component.get("name", "unknown"))
                        
                        if unhealthy_components:
                            findings.append(f"Unhealthy cluster components: {', '.join(unhealthy_components)}")
                        
                        # Check node health
                        unhealthy_nodes = []
                        for node in cluster.get("nodes", []):
                            if not node.get("ready", True):
                                unhealthy_nodes.append(node.get("name", "unknown"))
                        
                        if unhealthy_nodes:
                            findings.append(f"Unhealthy nodes in cluster: {', '.join(unhealthy_nodes)}")

                    # Add the findings to the prompt
                    if findings:
                        prompt_text += (
                            "\n"
                            + "\n".join(f"- {finding}" for finding in findings[:15])
                            + "\n"
                        )
                    else:
                        prompt_text += (
                            "\nNo significant issues found in the diagnostic data.\n"
                        )

                # Add instructions to the prompt
                prompt_text += """
Instructions:
You are a Kubernetes remediation planning assistant with advanced diagnostic capabilities. Your goal is to analyze the provided anomaly context and diagnostic information to generate a concise, actionable RemediationPlan that addresses the root cause of the issue.

Task:
1. Analyze the Anomaly Details, Recent Events, Resource Details, Diagnostic Information, and Root Cause Analysis.
2. Identify the most likely root cause based on the comprehensive diagnostic data provided.
3. Determine the best remediation strategy using ONLY the available Remediation Actions. Adhere strictly to the JSON output format.
4. Construct a RemediationPlan containing:
    - `reasoning`: A brief explanation of the identified root cause and justification for the chosen actions.
    - `actions`: A list of one or more actions from the available types with parameters filled using data from the context.
    - `requires_dry_run`: Boolean indicating if this plan should undergo a dry run validation
    - `risk_assessment`: Your assessment of potential risks associated with this plan
5. If multiple actions are needed, order them from least to most disruptive and indicate dependencies where applicable.
6. If no clear action is suitable based on the context, return a plan with an empty `actions` list and reasoning explaining why no action is recommended.

Consider the findings from the diagnostic data to inform your remediation strategy and make it as specific as possible to address the root cause, not just the symptoms.
"""
                
                # Add guidance examples for specific Kubernetes issues to the prompt
                prompt_text += """
Root Cause Analysis Guidance:
When dealing with common Kubernetes issues, here are examples of targeted remediation actions:

1. For pods stuck in CrashLoopBackOff with OOM errors:
```json
{
  "action_type": "update_resource_limits",
  "parameters": {
    "name": "deployment-name",
    "namespace": "namespace",
    "kind": "Deployment",
    "container": "container-name",
    "limits": {"memory": "512Mi"},
    "requests": {"memory": "256Mi"}
  }
}
```

2. For DNS resolution failures:
```json
{
  "action_type": "fix_dns_config",
  "parameters": {
    "name": "deployment-name",
    "namespace": "namespace",
    "kind": "Deployment",
    "dns_policy": "ClusterFirst",
    "nameservers": ["10.96.0.10"]
  }
}
```

3. For service with no endpoints:
```json
{
  "action_type": "fix_service_selectors",
  "parameters": {
    "name": "service-name",
    "namespace": "namespace",
    "selectors": {"app": "correct-label-value"}
  }
}
```

4. For failing deployment after update:
```json
{
  "action_type": "rollback_deployment",
  "parameters": {
    "name": "deployment-name",
    "namespace": "namespace",
    "revision": 2
  }
}
```

5. For pod stuck in Terminating state:
```json
{
  "action_type": "force_delete_pod",
  "parameters": {
    "name": "pod-name",
    "namespace": "namespace"
  }
}
```

6. For containers with high memory utilization:
```json
{
  "reasoning": "The container is experiencing high memory utilization (>150% of requested), which may lead to OOMKilled events or pod eviction. Increasing memory limits will provide more headroom for the application.",
  "actions": [
    {
      "action_type": "update_resource_limits",
      "parameters": {
        "name": "pod-name",
        "namespace": "namespace",
        "kind": "Pod",
        "container": "container-name",
        "limits": {"memory": "512Mi"},
        "requests": {"memory": "256Mi"}
      }
    }
  ],
  "requires_dry_run": true,
  "risk_assessment": "Updating resource limits has minimal risk but may temporarily affect pod scheduling. Verify the node has sufficient capacity for the increased limits."
}
```

7. For pods with config issues in ConfigMaps:
```json
{
  "reasoning": "Analysis of the pod logs shows configuration errors related to the ConfigMap settings. Updating the ConfigMap with correct values should resolve the issue.",
  "actions": [
    {
      "action_type": "update_configmap",
      "parameters": {
        "name": "config-name",
        "namespace": "namespace",
        "data": {
          "key1": "new-value1",
          "key2": "new-value2"
        }
      }
    }
  ],
  "requires_dry_run": true,
  "risk_assessment": "Updating ConfigMap is generally safe, but the application may need to restart to pick up the new configuration."
}
```

8. For pods with authentication issues in Secrets:
```json
{
  "reasoning": "Pod logs indicate authentication failures due to incorrect credentials in the Secret.",
  "actions": [
    {
      "action_type": "update_secret",
      "parameters": {
        "name": "secret-name",
        "namespace": "namespace",
        "data": {
          "username": "dXNlcm5hbWU=", 
          "password": "cGFzc3dvcmQ="
        }
      }
    }
  ],
  "requires_dry_run": true,
  "risk_assessment": "Updating a Secret requires careful validation of the base64-encoded values. Incorrect values may cause authentication failures."
}
```

9. For containers using outdated or buggy images:
```json
{
  "reasoning": "The current container image has known issues that are causing the observed crashes.",
  "actions": [
    {
      "action_type": "update_container_image",
      "parameters": {
        "name": "deployment-name",
        "namespace": "namespace",
        "kind": "Deployment",
        "container": "container-name",
        "image": "repository/image:latest-stable"
      }
    }
  ],
  "requires_dry_run": true,
  "risk_assessment": "Updating the container image will cause the pods to restart. Ensure the new image version is compatible with your application."
}
```

10. For repetitive CronJob failures:
```json
{
  "reasoning": "The CronJob has been failing consistently and needs to be restarted to clear its failure state.",
  "actions": [
    {
      "action_type": "restart_cronjob",
      "parameters": {
        "name": "cronjob-name",
        "namespace": "namespace"
      }
    }
  ],
  "requires_dry_run": false,
  "risk_assessment": "Restarting a CronJob is low risk as it only affects the scheduling of future jobs and doesn't impact running instances."
}
```

11. For stuck or failing Jobs:
```json
{
  "reasoning": "The Job is stuck in a failed state and needs to be restarted.",
  "actions": [
    {
      "action_type": "restart_job",
      "parameters": {
        "name": "job-name",
        "namespace": "namespace"
      }
    }
  ],
  "requires_dry_run": false,
  "risk_assessment": "Restarting a Job will delete and recreate it, which will terminate any running pods and start fresh."
}
```

Always use the action type that most directly addresses the root cause, not just the symptoms.
"""

                # Add final instructions about expected output format
                prompt_text += """
IMPORTANT: Your response MUST be a single, valid JSON object matching the structure below.
Ensure all strings are double-quoted, keys are double-quoted, and commas correctly separate elements in objects and arrays.
```json
{
  "reasoning": "Your detailed reasoning including root cause identification here...",
  "actions": [
    {
      "action_type": "action_type_here",
      "parameters": {
        "param1": "value1",
        "param2": "value2"
      }
    }
  ],
  "requires_dry_run": true,
  "risk_assessment": "Your risk assessment here..."
}
```

If no actions are needed, return the same structure with an empty actions array.
"""

                # Use agent.run with a simpler approach
                result = await agent.run(prompt_text, deps=dependencies)

                duration = time.monotonic() - start_time

                # The output is a string, not a RemediationPlan object
                # Parse the textual response into a structured RemediationPlan
                text_response = result.output
                logger.info(f"Generated AI response in {duration:.2f}s")

                try:
                    # Extract the JSON response from the AI output
                    json_match = re.search(
                        r"```json\s*(.*?)\s*```", text_response, re.DOTALL
                    )
                    if not json_match:
                        json_match = re.search(r"{.*}", text_response, re.DOTALL)

                    if json_match:
                        json_str = (
                            json_match.group(1)
                            if "```json" in text_response
                            else json_match.group(0)
                        )
                        
                        # Clean the JSON before parsing
                        json_str = clean_json_response(json_str)
                        
                        # Try to parse the cleaned JSON
                        try:
                            ai_response = json.loads(json_str)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse cleaned JSON: {e}. Falling back to static remediation plan.")
                            return await load_static_plan(
                                anomaly_record,
                                dependencies.db,
                                dependencies.k8s_client,
                                context=context
                            )

                        # Extract the actions from the AI response
                        ai_actions = ai_response.get("actions", [])
                        actions = []

                        # Create RemediationAction objects from the AI-recommended actions
                        for ai_action in ai_actions:
                            action_type = ai_action.get("action_type")
                            parameters = ai_action.get("parameters", {})

                            # Skip if action_type is missing
                            if not action_type:
                                continue

                            # Create a valid RemediationAction and convert to dictionary
                            action = RemediationAction(
                                action_type=action_type,
                                parameters=parameters,
                                description=f"Execute {action_type} to address the detected issue",
                                justification="AI-generated remediation based on anomaly data and diagnostic information",
                                entity_type=entity_type,
                                entity_id=entity_id,
                                priority=1,
                            )
                            actions.append(action.model_dump())

                        # Create a RemediationPlan with the parsed information
                        plan = RemediationPlan(
                            anomaly_id=anomaly_id,
                            plan_name=f"AI Plan for {anomaly_record.failure_reason or 'anomaly'}",
                            description=f"AI-generated plan for {anomaly_record.entity_id}",
                            reasoning=text_response[
                                :2000
                            ],  # Store enough reasoning but limit length
                            actions=actions,
                            ordered=True,
                            source_type="ai_generated",
                            created_at=datetime.datetime.now(datetime.timezone.utc),
                            trigger_source="automatic",
                            target_entity_type=entity_type,
                            target_entity_id=entity_id,
                            risk_assessment=ai_response.get(
                                "risk_assessment", "Automatically generated plan"
                            ),
                            context=context
                            or {},  # Include the provided context if available
                        )

                        logger.info(
                            f"Successfully parsed AI response into remediation plan: {plan.plan_name}"
                        )

                        # Store the plan in the database
                        try:
                            # Convert RemediationPlan to dict format
                            plan_dict = plan.model_dump(mode="json")

                            # Insert the plan, which gives us the ID to associate with the anomaly
                            result = await dependencies.db.remediation_plans.insert_one(
                                plan_dict
                            )
                            plan_id = result.inserted_id

                            # Update the anomaly record with the reference to this plan
                            await dependencies.db.anomalies.update_one(
                                {"_id": anomaly_id},
                                {
                                    "$set": {
                                        "remediation_plan_id": plan_id,
                                        "remediation_status": "planned",
                                    }
                                },
                            )

                            # Update the plan with its ID
                            plan.id = str(plan_id)

                            logger.info(
                                f"Stored remediation plan {plan_id} for anomaly {anomaly_id}"
                            )

                        except Exception as db_err:
                            logger.error(f"Error storing remediation plan: {db_err}")
                            # Continue anyway since we have the plan in memory

                        return plan
                    else:
                        logger.error("Failed to extract JSON from AI response")
                        raise ValueError("No JSON found in AI response")

                except Exception as parse_err:
                    logger.error(
                        f"Failed to parse AI response into a plan: {parse_err}"
                    )
                    raise

            except UnexpectedModelBehavior as e:
                logger.error(f"Gemini API error: {e}")
                logger.warning("Falling back to static remediation plan")

            except ValidationError as e:
                logger.error(f"Schema validation error for generated plan: {e}")
                logger.warning("Falling back to static remediation plan")

            except Exception as e:
                logger.exception(f"Error generating plan with Gemini: {e}")
                logger.warning("Falling back to static remediation plan")

        except Exception as context_err:
            logger.exception(
                f"Error setting up context for AI plan generation: {context_err}"
            )
            logger.warning("Falling back to static remediation plan")
    else:
        logger.info("No Gemini agent provided, using static remediation plan")

    # 2. Fall back to static plan if AI-generated plan failed or agent not available
    static_plan = await load_static_plan(
        anomaly_record,
        dependencies.db,
        dependencies.k8s_client,
        context=context,  # Pass the context to static plan too
    )

    if static_plan:
        logger.info(f"Using static remediation plan: {static_plan.plan_name}")
        return static_plan
    else:
        logger.warning(
            f"No remediation plan could be generated for anomaly {anomaly_id}"
        )
        return None


async def load_static_plan(
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    k8s_client: Optional[client.ApiClient] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Optional[RemediationPlan]:
    """
    Load a static remediation plan based on the anomaly type and subtype.
    Args:
        anomaly_record: The anomaly record
        db: MongoDB database client
        k8s_client: Kubernetes API client
        context: Optional additional context about the anomaly and diagnosis
    Returns:
        RemediationPlan if successful, None otherwise
    """
    if not anomaly_record.id:
        logger.error("Cannot load static plan: AnomalyRecord is missing database ID")
        return None

    metric_name = anomaly_record.metric_name or ""
    event_reason = anomaly_record.event_reason or ""
    entity_type = anomaly_record.entity_type or "Pod"  # Default to Pod if unknown
    entity_id = anomaly_record.entity_id or ""
    namespace, name = entity_id.split("/", 1) if "/" in entity_id else (None, None)

    # Handle DaemonSet pod name patterns
    # If this is a pod with format like 'ama-metrics-node-fvh6v', 
    # it's likely a DaemonSet pod and we need to extract the base DaemonSet name
    controller_kind = None
    controller_name = None
    if entity_type.lower() == "pod" and name and k8s_client:
        # If name looks like a DaemonSet pod, try to get the actual controller
        if "-node-" in name or "ama-" in name:  # Check for common DaemonSet pod patterns
            original_pod_name = name
            try:
                controller_kind, controller_name = await get_pod_controller(k8s_client, name, namespace or "default")
                if controller_kind:
                    logger.info(f"Detected pod '{name}' belongs to {controller_kind}/{controller_name}")
                    
                    # Update entity_type for correct plan selection if we found a specific controller
                    if controller_kind.lower() in ["daemonset", "statefulset", "deployment"]:
                        entity_type = controller_kind
                        # We don't modify name here, as the remediation action needs the original pod name
                        # But we'll pass the controller_name for context
            except Exception as e:
                logger.warning(f"Error determining controller for pod {name}: {e}")
                # If we found an error, continue with the original name
                
                # If we couldn't find the controller via owner refs, try pattern-based discovery for known types
                if "ama-metrics" in name:
                    logger.info(f"Identified Azure Monitor agent pod: {name}, trying to find matching DaemonSet")
                    try:
                        matching_daemonset = await find_matching_daemonset(k8s_client, namespace or "default", name)
                        if matching_daemonset:
                            logger.info(f"Found matching DaemonSet {matching_daemonset} for pod {name}")
                            controller_kind = "DaemonSet"
                            controller_name = matching_daemonset
                            entity_type = "DaemonSet"
                    except Exception as e:
                        logger.warning(f"Error finding matching DaemonSet for pod {name}: {e}")

    # Don't create pod-based actions for non-pod entities that have specific handling
    if entity_type.lower() in ["namespace", "node", "persistentvolume"]:
        logger.info(
            f"Not creating pod-based remediation actions for entity type {entity_type}"
        )
        # For namespace metrics, create a monitoring plan
        if entity_type.lower() == "namespace":
            plan = RemediationPlan(
                anomaly_id=str(anomaly_record.id),
                plan_name="Namespace Monitoring Plan",
                description=f"Monitor resources in namespace {name}",
                reasoning=f"Static plan: Creating observability plan for namespace {name}",
                actions=[],  # No direct actions, just monitoring
                ordered=True,
                source_type="static",
                created_at=datetime.datetime.now(datetime.timezone.utc),
                trigger_source="automatic",
                target_entity_type="Namespace",
                target_entity_id=entity_id,
                safety_score=1.0,
                risk_assessment="Informational only: No actions will be taken automatically. Consider reviewing resource quotas and limits in this namespace.",
                context=context or {},  # Include provided context
            )
            return plan

        # Create an empty plan with appropriate reasoning for others (node, pv)
        plan = RemediationPlan(
            anomaly_id=str(anomaly_record.id),
            plan_name=f"No Action Plan for {entity_type}",
            description=f"No remediation actions for {entity_type}",
            reasoning=f"Static plan: No appropriate actions available for entity type {entity_type}",
            actions=[],  # Empty actions list
            ordered=True,
            source_type="static",
            created_at=datetime.datetime.now(datetime.timezone.utc),
            trigger_source="automatic",
            target_entity_type=entity_type,
            target_entity_id=entity_id,
            context=context or {},  # Include provided context
        )
        return plan

    plan: Optional[RemediationPlan] = None # Initialize plan
    owner_meta: Optional[Dict[str, Any]] = None

    # Parse entity_id from anomaly_record to get original_namespace and original_name
    # This parsing is still needed for _format_static_plan and other logic
    parsed_namespace, parsed_name = anomaly_record.entity_id.split("/", 1) if "/" in anomaly_record.entity_id else (None, anomaly_record.entity_id)
    
    # Determine the kind and name for get_resource_metadata
    # entity_type from AnomalyRecord can be like "Pod", "Deployment", "Node", etc.
    kind_for_metadata = entity_type.lower()
    name_for_metadata = parsed_name
    
    # If we detected a controller, and we want to look up a controller resource directly,
    # use the controller name instead of the original entity name
    if controller_kind and controller_name and kind_for_metadata.lower() in ["daemonset", "deployment", "statefulset"]:
        name_for_metadata = controller_name
        logger.info(f"Using discovered controller name '{controller_name}' for resource metadata lookup")

    # Get resource metadata if k8s_client is available
    if k8s_client:
        # Correctly determine namespace for get_resource_metadata
        # If entity_type is 'node', namespace is None. Otherwise, use parsed_namespace or 'default'.
        namespace_for_metadata = None
        if kind_for_metadata != "node":
            namespace_for_metadata = parsed_namespace or "default"  # Fallback to 'default' if not in entity_id

        # For Pods, owner_meta is crucial. For direct controller anomalies, resource_meta is important.
        resource_meta, temp_owner_meta = await get_resource_metadata(
            k8s_client, 
            name=name_for_metadata,  # Use the controller name if available
            kind=kind_for_metadata, 
            namespace=namespace_for_metadata
        )
        
        if entity_type.lower() == "pod":
            owner_meta = temp_owner_meta  # If it was a pod, the second return is its owner
            
            # If we didn't get owner_meta from get_resource_metadata but we got it from get_pod_controller above,
            # use that information
            if not owner_meta and controller_kind and controller_name:
                # Construct a simplified owner_meta from our controller discovery
                owner_meta = {
                    "kind": controller_kind,
                    "metadata": {"name": controller_name},
                }
        else:
            # If entity is not a pod, owner_meta remains None
            pass

    # Handle StatefulSet or DaemonSet anomalies directly
    if entity_type.lower() in ["statefulset", "sts", "daemonset", "ds"]:
        logger.info(f"Processing {entity_type} anomaly for {entity_id}")
        
        if entity_type.lower() in ["daemonset", "ds"]:
            # Use controller_name if we have it, otherwise use original name
            ds_name_to_use = controller_name if controller_name else name
            
            # For DaemonSets, we need to create a DaemonSet restart plan
            plan = RemediationPlan(
                anomaly_id=str(anomaly_record.id),
                plan_name="DaemonSet Restart Plan",
                description=f"Restart DaemonSet {ds_name_to_use} to address issues",
                reasoning=f"Static plan: Restarting DaemonSet {ds_name_to_use} to address potential issues.",
                actions=[
                    RemediationAction(
                        action_type="restart_daemonset",
                        parameters={
                            "name": ds_name_to_use,
                            "namespace": namespace or "default",
                            # Store original pod name for reference if we started with a pod
                            "pod_name": name if entity_type.lower() == "pod" else None
                        },
                        description="Restart DaemonSet to refresh pods",
                        justification="DaemonSet pods may be experiencing issues",
                        entity_type="daemonset",
                        entity_id=entity_id,
                    ).model_dump()
                ],
                ordered=True,
                source_type="static",
                created_at=datetime.datetime.now(datetime.timezone.utc),
                trigger_source="automatic",
                target_entity_type=entity_type,
                target_entity_id=entity_id,
                context=context or {},
            )
            return plan
        else:
            # For StatefulSets, use the existing high load template
            plan = get_static_plan_template("statefulset_issue", "high_load")
            if plan:
                plan.anomaly_id = str(anomaly_record.id)
                plan_context = context or {}
                plan_context.update(
                    {"anomaly_type": "statefulset_issue", "entity_type": entity_type}
                )
                plan.context = plan_context
                # Formatting will use entity_id which is statefulset name directly
                return _format_static_plan(plan, entity_id)

    # Check for direct failure flag from detector
    if anomaly_record.is_direct_failure and anomaly_record.failure_reason:
        logger.info(
            f"Processing direct failure: {anomaly_record.failure_reason} for {entity_id}"
        )
        # ... (existing OOMKilled, CrashLoopBackOff, ImagePullBackOff logic - these are pod-specific actions) ...
        # For OOMKilled, CrashLoop, ImagePull - these typically result in delete_pod, which is fine.
        # The issue is when a generic "restart controller" plan is chosen.
        if anomaly_record.failure_reason in ("OOMKilled", "EVENT_OOMKILLED_COUNT"):
            plan = get_static_plan_template("oomkilled_event", "detected")
        elif anomaly_record.failure_reason in ("CrashLoopBackOff", "EVENT_CRASHLOOPBACKOFF_COUNT"):
            plan = get_static_plan_template("container_restart_count", "high")
        elif anomaly_record.failure_reason in ("ImagePullBackOff", "ErrImagePull", "EVENT_IMAGEPULLBACKOFF_COUNT"):
            plan = get_static_plan_template("default", "unknown") # Uses delete_pod
            if plan:
                plan.plan_name = "ImagePullBackOff Pod Restart"
        # ... (rest of direct failure logic for NodeNotReady etc.)

    # Match based on metric name pattern - THIS IS WHERE CPU_SPIKE_PLAN IS CHOSEN
    if not plan: # If plan not set by direct failure logic
        for pattern_key in STATIC_PLAN_TEMPLATES.keys():
            if pattern_key in metric_name.lower():
                variant = "high" # Default variant
                if "utilization" in metric_name.lower() or "usage" in metric_name.lower():
                    metric_value = anomaly_record.metric_value
                    if metric_value is not None and metric_value <= 90: # Assuming spike is not > 90
                        variant = "spike"
                
                plan = get_static_plan_template(pattern_key, variant)
                break # Found a plan based on metric name

            # Fall back to a default plan if no specific pattern matches
    if not plan and entity_type.lower() == "pod":
        plan = get_static_plan_template("default", "unknown")
    
    # If a plan is selected (especially one that might try to restart a controller)
    if plan:
        plan.anomaly_id = str(anomaly_record.id)
        # Add context to the plan
        plan_context = context or {}
        plan_context.update({"anomaly_record": anomaly_record.model_dump(mode='json')}) # Add full anomaly for reference
        
        # If we have discovered controller info, add it to the context
        if controller_kind and controller_name:
            plan_context.update({
                "discovered_controller": {
                    "kind": controller_kind,
                    "name": controller_name
                }
            })
            
        plan.context = plan_context

        # Primary entity type for the action
        action_entity_kind = entity_type.lower()
        if entity_type.lower() == "pod" and owner_meta:
            action_entity_kind = owner_meta.get("kind", "").lower()

        # Adjust restart action based on actual owner type (for pod anomalies)
        # OR based on the direct entity type if not a pod.
        if entity_type.lower() == "pod" and owner_meta:
            owner_kind_from_meta = owner_meta.get("kind", "").lower()
            owner_name_from_meta = owner_meta.get("metadata", {}).get("name") # Get owner's name

            for action in plan.actions:
                if action.action_type == ActionType.RESTART_DEPLOYMENT:
                    if owner_kind_from_meta == "daemonset":
                        action.action_type = ActionType.RESTART_DAEMONSET
                        if owner_name_from_meta:
                            action.parameters["name"] = owner_name_from_meta # Use owner's name
                        logger.info(f"Plan {plan.plan_name}: Switched to restart_daemonset for pod {entity_id} owned by DaemonSet {owner_name_from_meta}")
                    elif owner_kind_from_meta == "statefulset":
                        action.action_type = ActionType.RESTART_STATEFULSET
                        if owner_name_from_meta:
                            action.parameters["name"] = owner_name_from_meta # Use owner's name
                        logger.info(f"Plan {plan.plan_name}: Switched to restart_statefulset for pod {entity_id} owned by StatefulSet {owner_name_from_meta}")
                    elif owner_kind_from_meta == "deployment":
                        if owner_name_from_meta:
                            action.parameters["name"] = owner_name_from_meta # Use owner's name (Deployment name)
                        logger.info(f"Plan {plan.plan_name}: Ensured restart_deployment targets Deployment {owner_name_from_meta} for pod {entity_id}")
                    else:
                        # If owner is not D, DS, STS (e.g. ReplicaSet not owned by Deployment, or other controller)
                        # and the action is RESTART_DEPLOYMENT, it will likely fail.
                        # Keep the original parameters (which _format_static_plan would have set based on pod name)
                        # and log a warning.
                        if owner_kind_from_meta not in ["replicaset"]: # ReplicaSets are usually intermediary
                            logger.warning(f"Plan {plan.plan_name}: Action restart_deployment for pod {entity_id} with owner kind '{owner_kind_from_meta}' ({owner_name_from_meta}). Action may fail if '{action.parameters.get('name')}' is not a Deployment.")
                elif action.action_type == ActionType.RESTART_DAEMONSET:
                    # For existing restart_daemonset actions, ensure we have the correct DaemonSet name
                    pod_name = action.parameters.get("name", "")
                    if "-node-" in pod_name:
                        # Store the original pod name for better discovery
                        action.parameters["pod_name"] = pod_name
                        
                        # If we discovered the controller through API calls, use that name
                        if owner_kind_from_meta == "daemonset" and owner_name_from_meta:
                            logger.info(f"Plan {plan.plan_name}: Using discovered DaemonSet name '{owner_name_from_meta}' for pod '{pod_name}'")
                            action.parameters["name"] = owner_name_from_meta
                        # Otherwise try to extract it from the pod name pattern
                        else:
                            # Likely trying to restart a DaemonSet using a pod name with node suffix
                            parts = pod_name.split("-node-")
                            if len(parts) >= 1:
                                daemonset_name = parts[0]
                                logger.info(f"Plan {plan.plan_name}: Extracted DaemonSet name '{daemonset_name}' from pod name '{pod_name}'")
                                action.parameters["name"] = daemonset_name
                        
        elif entity_type.lower() == "daemonset":
            for action in plan.actions:
                if action.action_type == ActionType.RESTART_DEPLOYMENT: # If a generic plan chose this
                    action.action_type = ActionType.RESTART_DAEMONSET
                    logger.info(f"Plan {plan.plan_name}: Switched to restart_daemonset for DaemonSet {entity_id}")
        elif entity_type.lower() in ["statefulset", "sts"]:
            for action in plan.actions:
                if action.action_type == ActionType.RESTART_DEPLOYMENT: # If a generic plan chose this
                    action.action_type = ActionType.RESTART_STATEFULSET
                    logger.info(f"Plan {plan.plan_name}: Switched to restart_statefulset for StatefulSet {entity_id}")

        return _format_static_plan(plan, entity_id)

    # Fallback for non-pod entities if no plan was found earlier
    if entity_type.lower() not in ["pod", "statefulset", "sts"]:
        logger.info(f"No suitable static plan template for entity type {entity_type}")
        # ... (existing code to create empty plan for non-pod/non-sts)
        plan = RemediationPlan(
            anomaly_id=str(anomaly_record.id),
            plan_name=f"No Action Plan for {entity_type}",
            description=f"No suitable remediation actions for {entity_type}/{name}",
            reasoning=f"Static plan: No suitable remediation actions found for entity type {entity_type}",
            actions=[],
            ordered=True,
            source_type="static",
            created_at=datetime.datetime.now(datetime.timezone.utc),
            trigger_source="automatic",
            target_entity_type=entity_type,
            target_entity_id=entity_id,
            context=context or {},
        )
        return plan

    return None # Default return if no plan is formulated


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
        anomaly_id=str(
            plan.anomaly_id
        ),  # Convert to string to ensure it's properly handled
        plan_name=plan.plan_name,
        description=plan.description,
        reasoning=plan.reasoning,
        actions=[action.model_copy(deep=True).model_dump() for action in plan.actions],
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
        approval_time=plan.approval_time,
    )

    # Extract entity type and name from entity_id
    entity_type = "pod"  # Default to pod if no type is specified
    if "/" in entity_id:
        parts = entity_id.split("/", 1)
        if parts[0].lower() in [
            "namespace",
            "node",
            "deployment",
            "statefulset",
            "daemonset",
            "job",
        ]:
            entity_type = parts[0].lower()
            namespace, name = "default", parts[1]
        else:
            # Standard case: namespace/name format
            namespace, name = parts
    else:
        namespace, name = "default", entity_id

    # Filter out actions incompatible with the entity type
    valid_actions = []
    for action in formatted_plan.actions:
        # Skip pod operations on non-pod entities
        if action.action_type == "delete_pod" and entity_type != "pod":
            logger.info(f"Skipping delete_pod action for {entity_type}/{name}")
            continue

        # Skip deployment operations on non-deployment entities
        if action.action_type in [
            "scale_deployment",
            "restart_deployment",
        ] and entity_type not in ["deployment", "deploy"]:
            logger.info(
                f"Skipping {action.action_type} action for {entity_type}/{name}"
            )
            continue

        # Skip node operations on non-node entities
        if (
            action.action_type in ["cordon_node", "uncordon_node", "drain_node"]
            and entity_type != "node"
        ):
            logger.info(
                f"Skipping {action.action_type} action for {entity_type}/{name}"
            )
            continue

        # Update action parameters based on entity type
        for key, value in list(action.parameters.items()):
            if isinstance(value, str):
                # Replace {resource_name} with the entity name
                if "{resource_name}" in value:
                    action.parameters[key] = value.replace(
                        "{resource_name}", name or ""
                    )

                # Replace {namespace} with the extracted namespace
                if "{namespace}" in value:
                    action.parameters[key] = value.replace(
                        "{namespace}", namespace or "default"
                    )

                # Replace {pod_name} with the entity name for pod actions
                if "{pod_name}" in value:
                    action.parameters[key] = value.replace("{pod_name}", name or "")

        # For delete_pod actions, ensure namespace and name are set correctly
        if action.action_type == "delete_pod":
            action.parameters["namespace"] = namespace or "default"
            action.parameters["name"] = name or ""

        # For deployment actions, ensure namespace and name are set correctly
        if action.action_type in ["scale_deployment", "restart_deployment"]:
            action.parameters["namespace"] = namespace or "default"
            action.parameters["name"] = name or ""

        # For daemonset actions, handle node-suffixed pod names
        if action.action_type == "restart_daemonset":
            action.parameters["namespace"] = namespace or "default"
            # Handle DaemonSet pod names with node suffixes (example: ama-metrics-node-fvh6v)
            pod_name = name or ""
            if "-node-" in pod_name:
                parts = pod_name.split("-node-")
                if len(parts) >= 1:
                    daemonset_name = parts[0]
                    logger.info(f"_format_static_plan: Extracted DaemonSet name '{daemonset_name}' from pod name '{pod_name}'")
                    action.parameters["name"] = daemonset_name
                    # Also store the original pod name for better discovery
                    action.parameters["pod_name"] = pod_name
                    
                    # Special handling for Azure Monitor pods
                    if "ama-metrics" in pod_name:
                        logger.info(f"_format_static_plan: Applied special handling for Azure Monitor pod '{pod_name}'")
            else:
                action.parameters["name"] = pod_name

        # For node actions, ensure name is set correctly
        if action.action_type in ["cordon_node", "uncordon_node", "drain_node"]:
            action.parameters["name"] = name or ""

        valid_actions.append(action)

    # Update the plan with filtered actions
    formatted_plan.actions = valid_actions

    # If no valid actions remain, update the plan description
    if not valid_actions:
        formatted_plan.plan_name = f"No Action Plan for {entity_type}/{name}"
        formatted_plan.description = (
            f"No suitable remediation actions for {entity_type}/{name}"
        )
        formatted_plan.reasoning = f"Static plan: No suitable remediation actions found for entity type {entity_type}"

    return formatted_plan
