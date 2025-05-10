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
from loguru import logger
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior

from kubewise.models import (
    AnomalyRecord,
    RemediationAction,
    RemediationPlan,
)


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
        )
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
        )
    ],
)

MEMORY_HIGH_UTILIZATION_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="Memory High Utilization Scale Up",
    description="Scale up deployment to handle high memory utilization",
    reasoning="Static plan: High memory utilization detected. Scaling up the deployment to distribute memory load.",
    actions=[
        RemediationAction(
            action_type="scale_deployment",
            parameters={
                "name": "{resource_name}",
                "replicas": "{current_replicas + 1}",
                "namespace": "{namespace}",
            },
        )
    ],
)

MEMORY_LEAK_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="Memory Leak Pod Restart",
    description="Restart pods to address potential memory leak",
    reasoning="Static plan: Potential memory leak detected. Restarting the affected pods to reclaim memory.",
    actions=[
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"},
        )
    ],
)

OOMKILLED_POD_PLAN = RemediationPlan(
    anomaly_id=str(ObjectId()),
    plan_name="OOMKilled Pod Restart",
    description="Restart pod that experienced OOMKilled event",
    reasoning="Static plan: OOMKilled event detected for pod. Deleting the pod to allow rescheduling.",
    actions=[
        RemediationAction(
            action_type="delete_pod",
            parameters={"name": "{pod_name}", "namespace": "{namespace}"},
        )
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
        )
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
        )
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
        )
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
        )
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


class DiagnosticTools:
    """A collection of tools for gathering diagnostic information from Kubernetes."""

    def __init__(self, k8s_client: client.ApiClient):
        self.k8s_client = k8s_client
        self.core_v1_api = client.CoreV1Api(k8s_client)
        self.apps_v1_api = client.AppsV1Api(k8s_client)

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
                dependencies.k8s_client, anomaly_record
            )

            # Collect diagnostic information for analysis
            diagnostic_data = {}
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
            }

            try:
                # Make the actual API call to Gemini
                start_time = time.monotonic()

                # Format the input for Gemini API properly
                prompt_text = (
                    """Generate a remediation plan for Kubernetes anomaly:
Entity ID: """
                    + str(anomaly_record.entity_id)
                    + """
Entity Type: """
                    + str(entity_type)
                    + """
Failure Reason: """
                    + str(anomaly_record.failure_reason)
                    + """
Metric Name: """
                    + str(anomaly_record.metric_name)
                    + """
Metric Value: """
                    + str(anomaly_record.metric_value)
                    + """

Available Remediation Actions:
- action_type: "scale_deployment", parameters: { "name": "<deployment_name>", "replicas": <integer>, "namespace": "<namespace>" }
- action_type: "delete_pod", parameters: { "name": "<pod_name>", "namespace": "<namespace>" }
- action_type: "restart_deployment", parameters: { "name": "<deployment_name>", "namespace": "<namespace>" }
- action_type: "drain_node", parameters: { "name": "<node_name>", "grace_period_seconds": <integer>, "force": <boolean> }
- action_type: "scale_statefulset", parameters: { "name": "<statefulset_name>", "replicas": <integer>, "namespace": "<namespace>" }
- action_type: "taint_node", parameters: { "name": "<node_name>", "key": "<taint_key>", "value": "<taint_value>", "effect": "<NoSchedule|PreferNoSchedule|NoExecute>" }
- action_type: "evict_pod", parameters: { "name": "<pod_name>", "namespace": "<namespace>", "grace_period_seconds": <integer> }
- action_type: "vertical_scale_deployment", parameters: { "name": "<deployment_name>", "namespace": "<namespace>", "container": "<container_name>", "resource": "<cpu|memory>", "value": "<resource_value>" }
- action_type: "vertical_scale_statefulset", parameters: { "name": "<statefulset_name>", "namespace": "<namespace>", "container": "<container_name>", "resource": "<cpu|memory>", "value": "<resource_value>" }
- action_type: "cordon_node", parameters: { "name": "<node_name>" }
- action_type: "uncordon_node", parameters: { "name": "<node_name>" }
- action_type: "manual_intervention", parameters: { "reason": "<reason_string>", "instructions": "<instructions_string>" }

IMPORTANT: Use only action_type values from the list above. Do not use any other action types not listed here.

Diagnostic Information:
The system has collected detailed diagnostic information about the affected resource including logs, events, configuration, and status. This data has been analyzed to provide context about the anomaly.

Key findings from diagnostic data:
"""
                )
                # Add key findings from diagnostic data if available
                if diagnostic_data:
                    findings = []

                    # Add pod-related findings
                    if "pod_logs" in diagnostic_data and isinstance(
                        diagnostic_data["pod_logs"], dict
                    ):
                        for pod_name, logs in diagnostic_data["pod_logs"].items():
                            if logs and isinstance(logs, str):
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

                    # Add the findings to the prompt
                    if findings:
                        prompt_text += (
                            "\n"
                            + "\n".join(f"- {finding}" for finding in findings[:10])
                            + "\n"
                        )
                    else:
                        prompt_text += (
                            "\nNo significant issues found in the diagnostic data.\n"
                        )

                # Continue with the rest of the prompt
                prompt_text += """
Instructions:
You are a Kubernetes remediation planning assistant. Your goal is to analyze the provided anomaly context and diagnostic information to generate a concise, actionable RemediationPlan.

Task:
1. Analyze the Anomaly Details, Recent Events, Resource Details, and Diagnostic Information.
2. Determine the most likely cause and the best course of action using ONLY the available Remediation Actions. Adhere strictly to the JSON output format.
3. Construct a RemediationPlan containing:
    - `reasoning`: A brief explanation for the chosen actions based on the provided context and diagnostic data.
    - `actions`: A list of one or more actions from the available types with parameters filled using data from the context.
    - `requires_dry_run`: Boolean indicating if this plan should undergo a dry run validation
    - `risk_assessment`: Your assessment of potential risks associated with this plan
4. If no clear action is suitable based on the context, return a plan with an empty `actions` list and reasoning explaining why no action is recommended.

Consider the findings from the diagnostic data to inform your remediation strategy and make it as specific as possible.

IMPORTANT: Your response MUST be a single, valid JSON object matching the structure below.
Ensure all strings are double-quoted, keys are double-quoted, and commas correctly separate elements in objects and arrays.
```json
{
  "reasoning": "Your detailed reasoning here...",
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

                            # Create a valid RemediationAction
                            action = RemediationAction(
                                action_type=action_type,
                                parameters=parameters,
                                description=f"Execute {action_type} to address the detected issue",
                                justification="AI-generated remediation based on anomaly data and diagnostic information",
                                entity_type=entity_type,
                                entity_id=entity_id,
                                priority=1,
                            )
                            actions.append(action)

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

    # Extract relevant information from the anomaly
    metric_name = anomaly_record.metric_name or ""
    event_reason = anomaly_record.event_reason or ""
    entity_type = anomaly_record.entity_type or "Pod"  # Default to Pod if unknown
    entity_id = anomaly_record.entity_id or ""
    namespace, name = entity_id.split("/", 1) if "/" in entity_id else (None, None)

    # Don't create pod-based actions for non-pod entities
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

        # Create an empty plan with appropriate reasoning for others
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

    # Handle StatefulSet anomalies
    if entity_type.lower() in ["statefulset", "sts"]:
        logger.info(f"Processing StatefulSet anomaly for {entity_id}")
        plan = get_static_plan_template("statefulset_issue", "high_load")
        if plan:
            plan.anomaly_id = str(anomaly_record.id)

            # Add context to the plan
            plan_context = context or {}
            plan_context.update(
                {"anomaly_type": "statefulset_issue", "entity_type": entity_type}
            )
            plan.context = plan_context

            return _format_static_plan(plan, entity_id)

    # Check for direct failure flag from detector
    if anomaly_record.is_direct_failure and anomaly_record.failure_reason:
        logger.info(
            f"Processing direct failure: {anomaly_record.failure_reason} for {entity_id}"
        )

        # Select plan based on failure reason
        if anomaly_record.failure_reason in ("OOMKilled", "EVENT_OOMKILLED_COUNT"):
            # Handle OOM failures - get the template and add the anomaly ID
            plan = get_static_plan_template("oomkilled_event", "detected")
            if plan:
                plan.anomaly_id = str(anomaly_record.id)

                # Merge provided context with plan-specific context
                plan_context = context or {}
                plan_context.update(
                    {
                        "anomaly_type": "direct_failure",
                        "failure_reason": anomaly_record.failure_reason,
                        "failure_message": anomaly_record.failure_message,
                    }
                )
                plan.context = plan_context

                return _format_static_plan(plan, entity_id)

        elif anomaly_record.failure_reason in (
            "CrashLoopBackOff",
            "EVENT_CRASHLOOPBACKOFF_COUNT",
        ):
            # Handle crash loops - get high restart count plan
            plan = get_static_plan_template("container_restart_count", "high")
            if plan:
                plan.anomaly_id = str(anomaly_record.id)

                # Merge provided context with plan-specific context
                plan_context = context or {}
                plan_context.update(
                    {
                        "anomaly_type": "direct_failure",
                        "failure_reason": anomaly_record.failure_reason,
                        "failure_message": anomaly_record.failure_message,
                    }
                )
                plan.context = plan_context

                return _format_static_plan(plan, entity_id)

        elif anomaly_record.failure_reason in (
            "ImagePullBackOff",
            "ErrImagePull",
            "EVENT_IMAGEPULLBACKOFF_COUNT",
        ):
            # Image pull issues - use default pod restart plan
            plan = get_static_plan_template("default", "unknown")
            if plan:
                plan.anomaly_id = str(anomaly_record.id)
                plan.plan_name = "ImagePullBackOff Pod Restart"
                plan.description = "Restart pod with image pull issues"
                plan.reasoning = (
                    f"Static plan for ImagePullBackOff failure detected on {entity_id}"
                )

                # Merge provided context with plan-specific context
                plan_context = context or {}
                plan_context.update(
                    {
                        "anomaly_type": "direct_failure",
                        "failure_reason": anomaly_record.failure_reason,
                        "failure_message": anomaly_record.failure_message,
                    }
                )
                plan.context = plan_context

                return _format_static_plan(plan, entity_id)

        elif anomaly_record.failure_reason in ("NodeNotReady", "EVENT_UNHEALTHY_COUNT"):
            # Node issues - if we're handling a pod, try to delete to reschedule
            if entity_type.lower() == "pod":
                plan = get_static_plan_template("default", "unknown")
                if plan:
                    plan.anomaly_id = str(anomaly_record.id)
                    plan.plan_name = "Pod on Unhealthy Node"
                    plan.description = "Restart pod on unhealthy node"
                    plan.reasoning = (
                        f"Static plan for pod on unhealthy node: {entity_id}"
                    )

                    # Merge provided context with plan-specific context
                    plan_context = context or {}
                    plan_context.update(
                        {
                            "anomaly_type": "direct_failure",
                            "failure_reason": anomaly_record.failure_reason,
                            "failure_message": anomaly_record.failure_message,
                        }
                    )
                    plan.context = plan_context

                    return _format_static_plan(plan, entity_id)
            # For node issues with node entity_type, use the node pressure plan
            elif entity_type.lower() == "node":
                plan = get_static_plan_template("node_issue", "resource_pressure")
                if plan:
                    plan.anomaly_id = str(anomaly_record.id)

                    # Merge provided context with plan-specific context
                    plan_context = context or {}
                    plan_context.update(
                        {
                            "anomaly_type": "direct_failure",
                            "failure_reason": anomaly_record.failure_reason,
                            "failure_message": anomaly_record.failure_message,
                        }
                    )
                    plan.context = plan_context

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
                        plan.anomaly_id = str(anomaly_record.id)
                        plan.plan_name = "Deployment Scale Up"
                        plan.description = (
                            "Scale up deployment with insufficient replicas"
                        )
                        plan.reasoning = f"Static plan for deployment with insufficient replicas: {entity_id}"

                        # Merge provided context with plan-specific context
                        plan_context = context or {}
                        plan_context.update(
                            {
                                "anomaly_type": "direct_failure",
                                "failure_reason": anomaly_record.failure_reason,
                                "failure_message": anomaly_record.failure_message,
                            }
                        )
                        plan.context = plan_context

                        return _format_static_plan(plan, entity_id)

            elif "statefulset_replicas_available_ratio" in pattern:
                # This is a statefulset with insufficient available replicas
                if entity_type.lower() in ("statefulset", "sts"):
                    # Scale up the statefulset
                    plan = get_static_plan_template("statefulset_issue", "high_load")
                    if plan:
                        plan.anomaly_id = str(anomaly_record.id)
                        plan.plan_name = "StatefulSet Scale Up"
                        plan.description = (
                            "Scale up statefulset with insufficient replicas"
                        )
                        plan.reasoning = f"Static plan for statefulset with insufficient replicas: {entity_id}"

                        # Merge provided context with plan-specific context
                        plan_context = context or {}
                        plan_context.update(
                            {
                                "anomaly_type": "direct_failure",
                                "failure_reason": anomaly_record.failure_reason,
                                "failure_message": anomaly_record.failure_message,
                            }
                        )
                        plan.context = plan_context

                        return _format_static_plan(plan, entity_id)

            elif "pod_crashloopbackoff" in pattern or "pod_imagepullbackoff" in pattern:
                # Pod in crash loop or image pull backoff
                plan = get_static_plan_template("default", "unknown")
                if plan:
                    plan.anomaly_id = str(anomaly_record.id)
                    plan.plan_name = f"Pod {pattern} Restart"
                    plan.description = f"Restart pod with {pattern} issue"
                    plan.reasoning = f"Static plan for pod in {pattern}: {entity_id}"
                    plan.context = {
                        "anomaly_type": "direct_failure",
                        "failure_reason": anomaly_record.failure_reason,
                        "failure_message": anomaly_record.failure_message,
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
                        variant = "spike"  # Default if no value
                else:
                    variant = "high"  # Default if no value

                plan = get_static_plan_template(pattern, variant)
                if plan:
                    plan.anomaly_id = str(anomaly_record.id)
                    return _format_static_plan(plan, entity_id)
            else:
                # For non-utilization metrics, use default variant if available
                plan = get_static_plan_template(pattern)
                if plan:
                    plan.anomaly_id = str(anomaly_record.id)
                    return _format_static_plan(plan, entity_id)

    # Fall back to a default plan when no specific pattern matches
    # But only for pod entities
    if entity_type.lower() == "pod":
        default_plan = get_static_plan_template("default", "unknown")
        if default_plan:
            default_plan.anomaly_id = str(anomaly_record.id)
            return _format_static_plan(default_plan, entity_id)
    elif entity_type.lower() in ["statefulset", "sts"]:
        # Another fallback for StatefulSets if not matched above
        default_plan = get_static_plan_template("statefulset_issue", "high_load")
        if default_plan:
            default_plan.anomaly_id = str(anomaly_record.id)
            return _format_static_plan(default_plan, entity_id)
    else:
        # Create an empty plan for non-pod entities with no specific action
        logger.info(f"No suitable static plan template for entity type {entity_type}")
        plan = RemediationPlan(
            anomaly_id=str(anomaly_record.id),
            plan_name=f"No Action Plan for {entity_type}",
            description=f"No suitable remediation actions for {entity_type}/{name}",
            reasoning=f"Static plan: No suitable remediation actions found for entity type {entity_type}",
            actions=[],  # Empty actions list
            ordered=True,
            source_type="static",
            created_at=datetime.datetime.now(datetime.timezone.utc),
            trigger_source="automatic",
            target_entity_type=entity_type,
            target_entity_id=entity_id,
        )
        return plan

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
        anomaly_id=str(
            plan.anomaly_id
        ),  # Convert to string to ensure it's properly handled
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
