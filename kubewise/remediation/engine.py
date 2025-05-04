import asyncio
import datetime
import json
import random
import time
from functools import wraps
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, TypeVar, Union
import traceback

import motor.motor_asyncio
from bson import ObjectId
from kubernetes_asyncio import client
from loguru import logger
from pydantic import ValidationError
from pydantic_ai import Agent

from kubewise.collector.k8s_events import load_k8s_config
from kubewise.config import settings
from kubewise.models import (
    ActionType,
    AnomalyRecord,
    PyObjectId,
    ExecutedActionRecord,
    RemediationAction,
    RemediationPlan,
)
from kubewise.remediation.planner import PlannerDependencies, generate_remediation_plan, load_static_plan
from kubewise.utils.retry import with_exponential_backoff

# Type definitions
T = TypeVar('T')
ActionCoroutine = Callable[[client.ApiClient, Dict[str, Any]], Coroutine[Any, Any, Tuple[bool, str]]]

# Constants
ACTION_TIMEOUT = 60.0  # seconds before action times out

# Registry for remediation actions
ACTION_REGISTRY: Dict[ActionType, ActionCoroutine] = {}

def register_action(action_type: ActionType) -> Callable[[ActionCoroutine], ActionCoroutine]:
    """
    Decorator to register a function as a handler for a specific remediation action.
    
    Args:
        action_type: The action type identifier (e.g., 'scale_deployment').
        
    Returns:
        The decorator function.
    """
    def decorator(func: ActionCoroutine) -> ActionCoroutine:
        if action_type in ACTION_REGISTRY:
            logger.warning(f"Action type '{action_type}' is already registered. Overwriting.")
        logger.debug(f"Registering action handler for '{action_type}'")
        ACTION_REGISTRY[action_type] = func
        return func
    return decorator


# Registered DSL Actions

@register_action("scale_deployment")
async def scale_deployment_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Scales a Kubernetes Deployment to the specified number of replicas.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name', 'replicas', and optionally 'namespace'.

    Returns:
        Tuple (success: bool, message: str).
    """
    apps_v1_api = client.AppsV1Api(api_client)
    name = params.get("name")
    replicas = params.get("replicas")
    namespace = params.get("namespace", "default")

    if not name or replicas is None:
        return False, "Missing 'name' or 'replicas' parameter for scale_deployment"

    logger.debug(f"scale_deployment_action received replicas: type={type(replicas)}, value='{replicas}'")

    target_replicas: int
    expected_placeholder = "{current_replicas + 1}"
    is_placeholder = isinstance(replicas, str) and replicas.strip() == expected_placeholder
    logger.debug(f"Checking for placeholder: received='{replicas}', expected='{expected_placeholder}', is_match={is_placeholder}")

    # Handle placeholder string for replicas
    if is_placeholder:
        try:
            logger.debug(f"Attempting to fetch current replicas for {namespace}/{name}")
            current_scale = await apps_v1_api.read_namespaced_deployment_scale(name=name, namespace=namespace)
            current_replicas = current_scale.spec.replicas if current_scale.spec and current_scale.spec.replicas is not None else 0
            logger.debug(f"Fetched current replicas: {current_replicas}")
            target_replicas = current_replicas + 1
            logger.info(f"Placeholder detected. Current replicas: {current_replicas}. Target replicas: {target_replicas}")
        except client.ApiException as e:
            msg = f"Failed to get current replica count for deployment '{namespace}/{name}': {e.status} - {e.reason}"
            logger.error(msg)
            return False, msg
        except Exception as e:
            msg = f"Unexpected error getting current replica count for deployment '{namespace}/{name}': {e}"
            logger.exception(msg)
            return False, msg
    elif isinstance(replicas, int) and replicas >= 0:
        target_replicas = replicas
    else:
        return False, f"Invalid 'replicas' value: {replicas}. Must be a non-negative integer or '{{current_replicas + 1}}'."

    patch_body = {"spec": {"replicas": target_replicas}}
    try:
        logger.info(f"Scaling deployment '{namespace}/{name}' to {target_replicas} replicas...")
        await apps_v1_api.patch_namespaced_deployment_scale(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully scaled deployment '{namespace}/{name}' to {target_replicas} replicas."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = (f"Failed to scale deployment '{namespace}/{name}': "
               f"{e.status} - {e.reason} - {e.body}")
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error scaling deployment '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("delete_pod")
async def delete_pod_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Deletes a specific Kubernetes Pod.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name' and 'namespace'.

    Returns:
        Tuple (success: bool, message: str).
    """
    core_v1_api = client.CoreV1Api(api_client)
    name = params.get("name")
    namespace = params.get("namespace")

    if not name or not namespace:
        return False, "Missing 'name' or 'namespace' parameter for delete_pod"

    try:
        # Pre-check: Verify the pod exists before attempting deletion
        logger.info(f"Verifying pod '{namespace}/{name}' exists before deletion...")
        try:
            await core_v1_api.read_namespaced_pod(name=name, namespace=namespace)
        except client.ApiException as pre_check_error:
            if pre_check_error.status == 404:
                msg = f"Pre-check: Pod '{namespace}/{name}' not found. Marking as already deleted."
                logger.warning(msg)
                return True, msg  # Pod doesn't exist, mark action as successful
            else:
                # Other API errors during pre-check should be reported
                raise pre_check_error

        # Pod exists, proceed with deletion
        logger.info(f"Deleting pod '{namespace}/{name}'...")
        await core_v1_api.delete_namespaced_pod(name=name, namespace=namespace)
        msg = f"Successfully initiated deletion for pod '{namespace}/{name}'."
        logger.info(msg)
        # Deletion is asynchronous in K8s. We report success on initiating.
        return True, msg
    except client.ApiException as e:
        # Handle 'Not Found' gracefully - maybe the pod was already deleted
        if e.status == 404:
            msg = f"Pod '{namespace}/{name}' not found. Assuming already deleted."
            logger.warning(msg)
            return True, msg # Treat as success if already gone
        else:
            msg = (f"Failed to delete pod '{namespace}/{name}': "
                   f"{e.status} - {e.reason} - {e.body}")
            logger.error(msg)
            return False, msg
    except Exception as e:
        msg = f"Unexpected error deleting pod '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("restart_deployment")
async def restart_deployment_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Restart a Kubernetes Deployment by adding a restart annotation.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name' and optionally 'namespace'.

    Returns:
        Tuple (success: bool, message: str).
    """
    apps_v1_api = client.AppsV1Api(api_client)
    name = params.get("name")
    namespace = params.get("namespace", "default")

    if not name:
        return False, "Missing 'name' parameter for restart_deployment"

    try:
        # Add a restart annotation with the current timestamp
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        patch_body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": timestamp
                        }
                    }
                }
            }
        }
        
        logger.info(f"Restarting deployment '{namespace}/{name}'...")
        await apps_v1_api.patch_namespaced_deployment(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully initiated restart for deployment '{namespace}/{name}' at {timestamp}."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = (f"Failed to restart deployment '{namespace}/{name}': "
               f"{e.status} - {e.reason} - {e.body}")
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error restarting deployment '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("drain_node")
async def drain_node_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Drain a Kubernetes node by cordoning it and evicting pods.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name', and optionally 'grace_period_seconds' and 'force'.

    Returns:
        Tuple (success: bool, message: str).
    """
    core_v1_api = client.CoreV1Api(api_client)
    name = params.get("name")
    force = params.get("force", False)
    grace_period = params.get("grace_period_seconds", 300)  # Default 5 min grace period

    if not name:
        return False, "Missing 'name' parameter for drain_node"

    try:
        # Step 1: Cordon the node (mark as unschedulable)
        logger.info(f"Cordoning node '{name}'...")
        body = {"spec": {"unschedulable": True}}
        await core_v1_api.patch_node(name=name, body=body)
        
        # Step 2: Get all pods on the node
        field_selector = f"spec.nodeName={name},status.phase!=Failed,status.phase!=Succeeded"
        pods = await core_v1_api.list_pod_for_all_namespaces(field_selector=field_selector)
        
        pods_count = len(pods.items)
        logger.info(f"Node '{name}' has {pods_count} pods to evict")
        
        if pods_count == 0:
            return True, f"Node '{name}' successfully cordoned with no pods to evict"
        
        # Step 3: Evict each pod with grace period
        evicted_count = 0
        for pod in pods.items:
            # Skip DaemonSet pods if not force
            if not force and any(owner.kind == "DaemonSet" for owner in pod.metadata.owner_references or []):
                logger.info(f"Skipping DaemonSet pod '{pod.metadata.namespace}/{pod.metadata.name}'")
                continue
                
            logger.info(f"Evicting pod '{pod.metadata.namespace}/{pod.metadata.name}' from node '{name}'")
            # Create eviction object
            eviction_body = {
                "apiVersion": "policy/v1",
                "kind": "Eviction",
                "metadata": {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace
                },
                "deleteOptions": {
                    "gracePeriodSeconds": grace_period
                }
            }
            
            try:
                # We use the generic API since evictions are special
                await api_client.post(
                    f"/api/v1/namespaces/{pod.metadata.namespace}/pods/{pod.metadata.name}/eviction",
                    body=eviction_body
                )
                evicted_count += 1
            except client.ApiException as pod_e:
                if pod_e.status == 429:  # Too Many Requests
                    logger.warning(f"Pod eviction throttled, waiting 10s: '{pod.metadata.namespace}/{pod.metadata.name}'")
                    await asyncio.sleep(10)
                    # Continue with next pod, we'll let caller retry the drain if needed
                else:
                    logger.error(f"Failed to evict pod '{pod.metadata.namespace}/{pod.metadata.name}': {pod_e.reason}")
        
        msg = f"Node '{name}' drained: cordoned successfully and evicted {evicted_count}/{pods_count} pods"
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = f"Failed to drain node '{name}': {e.status} - {e.reason}"
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error draining node '{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("scale_statefulset")
async def scale_statefulset_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Scales a Kubernetes StatefulSet to the specified number of replicas.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name', 'replicas', and optionally 'namespace'.

    Returns:
        Tuple (success: bool, message: str).
    """
    apps_v1_api = client.AppsV1Api(api_client)
    name = params.get("name")
    replicas = params.get("replicas")
    namespace = params.get("namespace", "default")  # Default to 'default' if not provided

    if not name or replicas is None:
        return False, "Missing 'name' or 'replicas' parameter for scale_statefulset"

    # --- Debugging Replica Value ---
    logger.debug(f"scale_statefulset_action received replicas: type={type(replicas)}, value='{replicas}'")
    # --- End Debugging ---

    target_replicas: int
    expected_placeholder = "{current_replicas + 1}"
    # More robust check: strip whitespace from input 'replicas' before comparison
    is_placeholder = isinstance(replicas, str) and replicas.strip() == expected_placeholder
    logger.debug(f"Checking for placeholder: received='{replicas}', expected='{expected_placeholder}', is_match={is_placeholder}")

    # Handle placeholder string for replicas
    if is_placeholder:
        try:
            logger.debug(f"Attempting to fetch current replicas for {namespace}/{name}")
            current_scale = await apps_v1_api.read_namespaced_stateful_set_scale(name=name, namespace=namespace)
            current_replicas = current_scale.spec.replicas if current_scale.spec and current_scale.spec.replicas is not None else 0
            logger.debug(f"Fetched current replicas: {current_replicas}")
            target_replicas = current_replicas + 1
            logger.info(f"Placeholder detected. Current replicas: {current_replicas}. Target replicas: {target_replicas}")
        except client.ApiException as e:
            msg = f"Failed to get current replica count for statefulset '{namespace}/{name}': {e.status} - {e.reason}"
            logger.error(msg)
            return False, msg
        except Exception as e:
            msg = f"Unexpected error getting current replica count for statefulset '{namespace}/{name}': {e}"
            logger.exception(msg)
            return False, msg
    elif isinstance(replicas, int) and replicas >= 0:
        target_replicas = replicas
    else:
        return False, f"Invalid 'replicas' value: {replicas}. Must be a non-negative integer or '{{current_replicas + 1}}'."

    patch_body = {"spec": {"replicas": target_replicas}}
    try:
        logger.info(f"Scaling statefulset '{namespace}/{name}' to {target_replicas} replicas...")
        await apps_v1_api.patch_namespaced_stateful_set_scale(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully scaled statefulset '{namespace}/{name}' to {target_replicas} replicas."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = (f"Failed to scale statefulset '{namespace}/{name}': "
               f"{e.status} - {e.reason} - {e.body}")
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error scaling statefulset '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("taint_node")
async def taint_node_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Apply a taint to a node.
    
    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing:
            - 'name': Node name
            - 'key': Taint key
            - 'value': Taint value
            - 'effect': One of: NoSchedule, PreferNoSchedule, NoExecute
            
    Returns:
        Tuple (success: bool, message: str).
    """
    core_v1_api = client.CoreV1Api(api_client)
    name = params.get("name")
    key = params.get("key")
    value = params.get("value")
    effect = params.get("effect")
    
    if not name or not key or not value or not effect:
        return False, "Missing required parameters for taint_node"
    
    if effect not in ["NoSchedule", "PreferNoSchedule", "NoExecute"]:
        return False, f"Invalid effect '{effect}'. Must be one of: NoSchedule, PreferNoSchedule, NoExecute"
    
    try:
        # Get current node
        node = await core_v1_api.read_node(name=name)
        
        # Create the taint to apply
        new_taint = {"key": key, "value": value, "effect": effect}
        
        # Check if the taint already exists
        current_taints = node.spec.taints or []
        
        for taint in current_taints:
            if taint.key == key and taint.effect == effect:
                msg = f"Taint with key '{key}' and effect '{effect}' already exists on node '{name}'"
                logger.info(msg)
                return True, msg
        
        # Add the new taint
        updated_taints = current_taints + [client.V1Taint(**new_taint)]
        
        # Prepare the patch
        patch_body = {"spec": {"taints": updated_taints}}
        
        # Apply the patch
        logger.info(f"Adding taint {key}={value}:{effect} to node '{name}'")
        await core_v1_api.patch_node(name=name, body=patch_body)
        
        msg = f"Successfully added taint {key}={value}:{effect} to node '{name}'"
        logger.info(msg)
        return True, msg
        
    except client.ApiException as e:
        msg = f"Failed to taint node '{name}': {e.status} - {e.reason} - {e.body}"
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error tainting node '{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("evict_pod")
async def evict_pod_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Evicts a pod using the Pod Eviction API.
    
    Unlike delete_pod, this respects PodDisruptionBudgets.
    
    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing:
            - 'name': Pod name
            - 'namespace': Pod namespace
            - Optional 'grace_period_seconds': Grace period for pod termination
            
    Returns:
        Tuple (success: bool, message: str).
    """
    core_v1_api = client.CoreV1Api(api_client)
    policy_v1_api = client.PolicyV1Api(api_client)
    
    name = params.get("name")
    namespace = params.get("namespace")
    grace_period_seconds = params.get("grace_period_seconds")
    
    if not name or not namespace:
        return False, "Missing 'name' or 'namespace' parameter for evict_pod"
    
    # Create eviction body
    eviction_body = {
        "apiVersion": "policy/v1",
        "kind": "Eviction",
        "metadata": {
            "name": name,
            "namespace": namespace
        }
    }
    
    # Add grace period if specified
    if grace_period_seconds is not None:
        if not isinstance(grace_period_seconds, int) or grace_period_seconds < 0:
            return False, f"Invalid grace_period_seconds: {grace_period_seconds}. Must be a non-negative integer."
        eviction_body["deleteOptions"] = {"gracePeriodSeconds": grace_period_seconds}
    
    try:
        logger.info(f"Evicting pod '{namespace}/{name}'...")
        # Use the custom API call for evictions
        await policy_v1_api.create_namespaced_pod_eviction(
            name=name, 
            namespace=namespace,
            body=client.V1Eviction(**eviction_body)
        )
        msg = f"Successfully initiated eviction for pod '{namespace}/{name}'."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        # Special handling for eviction blocked by PDB
        if e.status == 429:  # Too Many Requests
            msg = f"Cannot evict pod '{namespace}/{name}': blocked by PodDisruptionBudget"
            logger.warning(msg)
            return False, msg
        elif e.status == 404:
            msg = f"Pod '{namespace}/{name}' not found. Assuming already gone."
            logger.warning(msg)
            return True, msg  # Treat as success if already gone
        else:
            msg = f"Failed to evict pod '{namespace}/{name}': {e.status} - {e.reason} - {e.body}"
            logger.error(msg)
            return False, msg
    except Exception as e:
        msg = f"Unexpected error evicting pod '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("vertical_scale_deployment")
async def vertical_scale_deployment_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Vertically scale a deployment by updating resource requests/limits for a specific container.
    
    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing:
            - 'name': Deployment name
            - 'namespace': Deployment namespace
            - 'container': Container name to scale
            - 'resource': Resource to scale ('cpu' or 'memory')
            - 'value': New resource value (e.g. '200m' for CPU, '512Mi' for memory)
            
    Returns:
        Tuple (success: bool, message: str).
    """
    apps_v1_api = client.AppsV1Api(api_client)
    name = params.get("name")
    namespace = params.get("namespace", "default")
    container_name = params.get("container")
    resource_type = params.get("resource")
    resource_value = params.get("value")
    
    # Validate params
    if not name or not container_name or not resource_type or not resource_value:
        return False, "Missing required parameters for vertical_scale_deployment"
    
    if resource_type not in ["cpu", "memory"]:
        return False, f"Invalid resource type: {resource_type}. Must be 'cpu' or 'memory'."
    
    try:
        # Get current deployment
        deployment = await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
        
        # Find the container spec
        containers = deployment.spec.template.spec.containers
        target_container = None
        container_index = -1
        
        for i, container in enumerate(containers):
            if container.name == container_name:
                target_container = container
                container_index = i
                break
        
        if target_container is None:
            return False, f"Container '{container_name}' not found in deployment '{namespace}/{name}'"
        
        # Ensure resources exist
        if not target_container.resources:
            target_container.resources = client.V1ResourceRequirements()
        
        # Ensure requests and limits exist
        if not target_container.resources.requests:
            target_container.resources.requests = {}
        if not target_container.resources.limits:
            target_container.resources.limits = {}
        
        # Update the resources
        target_container.resources.requests[resource_type] = resource_value
        target_container.resources.limits[resource_type] = resource_value
        
        # Update the container in the list
        containers[container_index] = target_container
        
        # Create the patch
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "resources": {
                                    "requests": {
                                        resource_type: resource_value
                                    },
                                    "limits": {
                                        resource_type: resource_value
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
        
        # Apply the patch with strategic merge patch type
        logger.info(f"Vertically scaling {resource_type} resources for container '{container_name}' in deployment '{namespace}/{name}' to {resource_value}")
        await apps_v1_api.patch_namespaced_deployment(
            name=name, 
            namespace=namespace, 
            body=patch_body
        )
        
        msg = f"Successfully updated {resource_type} resources for container '{container_name}' in deployment '{namespace}/{name}' to {resource_value}"
        logger.info(msg)
        return True, msg
        
    except client.ApiException as e:
        msg = f"Failed to update resources for container '{container_name}' in deployment '{namespace}/{name}': {e.status} - {e.reason} - {e.body}"
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error updating resources for container '{container_name}' in deployment '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("vertical_scale_statefulset")
async def vertical_scale_statefulset_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Vertically scale a statefulset by updating resource requests/limits for a specific container.
    
    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing:
            - 'name': StatefulSet name
            - 'namespace': StatefulSet namespace
            - 'container': Container name to scale
            - 'resource': Resource to scale ('cpu' or 'memory')
            - 'value': New resource value (e.g. '200m' for CPU, '512Mi' for memory)
            
    Returns:
        Tuple (success: bool, message: str).
    """
    apps_v1_api = client.AppsV1Api(api_client)
    name = params.get("name")
    namespace = params.get("namespace", "default")
    container_name = params.get("container")
    resource_type = params.get("resource")
    resource_value = params.get("value")
    
    # Validate params
    if not name or not container_name or not resource_type or not resource_value:
        return False, "Missing required parameters for vertical_scale_statefulset"
    
    if resource_type not in ["cpu", "memory"]:
        return False, f"Invalid resource type: {resource_type}. Must be 'cpu' or 'memory'."
    
    try:
        # Get current statefulset
        statefulset = await apps_v1_api.read_namespaced_stateful_set(name=name, namespace=namespace)
        
        # Find the container spec
        containers = statefulset.spec.template.spec.containers
        target_container = None
        container_index = -1
        
        for i, container in enumerate(containers):
            if container.name == container_name:
                target_container = container
                container_index = i
                break
        
        if target_container is None:
            return False, f"Container '{container_name}' not found in statefulset '{namespace}/{name}'"
        
        # Create the patch
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "resources": {
                                    "requests": {
                                        resource_type: resource_value
                                    },
                                    "limits": {
                                        resource_type: resource_value
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
        
        # Apply the patch with strategic merge patch type
        logger.info(f"Vertically scaling {resource_type} resources for container '{container_name}' in statefulset '{namespace}/{name}' to {resource_value}")
        await apps_v1_api.patch_namespaced_stateful_set(
            name=name, 
            namespace=namespace, 
            body=patch_body
        )
        
        msg = f"Successfully updated {resource_type} resources for container '{container_name}' in statefulset '{namespace}/{name}' to {resource_value}"
        logger.info(msg)
        return True, msg
        
    except client.ApiException as e:
        msg = f"Failed to update resources for container '{container_name}' in statefulset '{namespace}/{name}': {e.status} - {e.reason} - {e.body}"
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error updating resources for container '{container_name}' in statefulset '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("cordon_node")
async def cordon_node_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Cordon a node (mark it as unschedulable).
    
    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing:
            - 'name': Node name
            
    Returns:
        Tuple (success: bool, message: str).
    """
    core_v1_api = client.CoreV1Api(api_client)
    name = params.get("name")
    
    if not name:
        return False, "Missing 'name' parameter for cordon_node"
    
    try:
        # Get current node
        node = await core_v1_api.read_node(name=name)
        
        # Check if already cordoned
        if node.spec.unschedulable:
            msg = f"Node '{name}' is already cordoned"
            logger.info(msg)
            return True, msg
        
        # Prepare the patch
        patch_body = {"spec": {"unschedulable": True}}
        
        # Apply the patch
        logger.info(f"Cordoning node '{name}'")
        await core_v1_api.patch_node(name=name, body=patch_body)
        
        msg = f"Successfully cordoned node '{name}'"
        logger.info(msg)
        return True, msg
        
    except client.ApiException as e:
        msg = f"Failed to cordon node '{name}': {e.status} - {e.reason} - {e.body}"
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error cordoning node '{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("uncordon_node")
async def uncordon_node_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Uncordon a node (mark it as schedulable).
    
    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing:
            - 'name': Node name
            
    Returns:
        Tuple (success: bool, message: str).
    """
    core_v1_api = client.CoreV1Api(api_client)
    name = params.get("name")
    
    if not name:
        return False, "Missing 'name' parameter for uncordon_node"
    
    try:
        # Get current node
        node = await core_v1_api.read_node(name=name)
        
        # Check if already uncordoned
        if not node.spec.unschedulable:
            msg = f"Node '{name}' is already schedulable"
            logger.info(msg)
            return True, msg
        
        # Prepare the patch
        patch_body = {"spec": {"unschedulable": False}}
        
        # Apply the patch
        logger.info(f"Uncordoning node '{name}'")
        await core_v1_api.patch_node(name=name, body=patch_body)
        
        msg = f"Successfully uncordoned node '{name}'"
        logger.info(msg)
        return True, msg
        
    except client.ApiException as e:
        msg = f"Failed to uncordon node '{name}': {e.status} - {e.reason} - {e.body}"
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error uncordoning node '{name}': {e}"
        logger.exception(msg)
        return False, msg


async def execute_action_with_timeout(
    action: RemediationAction,
    api_client: client.ApiClient,
    action_idx: int,
    total_actions: int,
    anomaly_id: PyObjectId,
    action_log_collection: motor.motor_asyncio.AsyncIOMotorCollection,
    entity_id: str,
    anomaly_score: float,
    source_type: str,
    app_context=None
) -> Tuple[bool, str]:
    """
    Execute a remediation action with proper timeout, retry logic, and logging.
    
    Args:
        action: The RemediationAction to execute
        api_client: Kubernetes API client
        action_idx: Index of this action in the plan (1-based)
        total_actions: Total number of actions in the plan
        anomaly_id: ID of the anomaly that triggered remediation
        action_log_collection: Collection to store execution records
        entity_id: ID of the entity being remediated
        anomaly_score: Score of the anomaly
        source_type: Source of the anomaly (e.g., 'metric', 'event')
        app_context: Optional application context for metrics
        
    Returns:
        Tuple of (success, message)
    """
    action_type = action.action_type
    
    # Skip if action doesn't exist
    if action_type not in ACTION_REGISTRY:
        error_msg = f"Unknown action type: {action_type}"
        logger.error(error_msg)
        return False, error_msg
    
    # Prepare to track metrics if app_context is available
    if app_context:
        # Use remediation_stats instead of trying to access metrics
        if hasattr(app_context, 'record_remediation'):
            # We'll record this after execution completes successfully or fails
            # This is just preparing for execution
            logger.debug(f"Execution context ready for action {action_type}")
            
    # Parse entity info from entity_id
    namespace = "default"
    name = ""
    if "/" in entity_id:
        try:
            namespace, name = entity_id.split("/", 1)
        except ValueError:
            name = entity_id
    
    # Extract metadata from the action
    params = action.parameters
    entity_type = action.entity_type or "Pod"  # Default to Pod if not specified
    
    # Update action with entity info if not already set
    if not action.entity_type:
        action.entity_type = entity_type
    if not action.entity_id:
        action.entity_id = entity_id
        
    # Ensure namespace is in parameters if not present
    if "namespace" not in params and namespace:
        params["namespace"] = namespace
        
    # Ensure name is in parameters if not present and action requires it
    if "name" not in params and name and action_type not in ["cordon_node", "uncordon_node", "drain_node"]:
        params["name"] = name

    # Set start time for duration tracking
    start_time = time.monotonic()
    
    # Get the action function
    action_func = ACTION_REGISTRY[action_type]
    
    # Define retry wrapper
    retry_count = 0
    max_retries = 2  # Maximum number of retry attempts
    
    async def retry_action() -> Tuple[bool, str]:
        nonlocal retry_count
        retry_count += 1
        
        if retry_count > 1:
            logger.info(f"Retry {retry_count-1}/{max_retries} for action {action_type} on {entity_id}")
        
        try:
            # Use the registry to call the appropriate action function
            return await action_func(api_client, params)
        except Exception as e:
            logger.exception(f"Error executing action {action_type}: {str(e)}")
            return False, f"Exception during execution: {str(e)}"
    
    # Execute with timeout
    try:
        logger.info(f"Executing action {action_idx}/{total_actions}: {action_type} with parameters {params}")
        
        # Use exponential backoff for retries
        success = False
        message = ""
        last_error = None
        
        for attempt in range(1, max_retries + 2):  # +2 because range starts at 1 and we need to include max_retries
            try:
                # Set timeout based on action type (node operations need more time)
                timeout_secs = 60 if "node" in action_type else 30
                
                # Execute with timeout
                success, message = await asyncio.wait_for(
                    retry_action(), 
                    timeout=timeout_secs
                )
                
                # Break if successful
                if success:
                    break
                    
                # Only retry on specific failures
                if "not found" in message.lower() or "connection" in message.lower():
                    # These are failures worth retrying (transient issues)
                    if attempt <= max_retries:
                        retry_delay = 2 ** attempt  # Exponential backoff
                        logger.warning(f"Action failed, will retry in {retry_delay}s: {message}")
                        await asyncio.sleep(retry_delay)
                        continue
                        
                # Non-retryable failure or max retries reached
                break
                
            except asyncio.TimeoutError:
                last_error = "Operation timed out"
                if attempt <= max_retries:
                    retry_delay = 2 ** attempt
                    logger.warning(f"Action timed out, will retry in {retry_delay}s")
                    await asyncio.sleep(retry_delay)
                else:
                    message = f"Action timed out after {timeout_secs}s and {max_retries} retries"
                    success = False
                    break
                    
            except Exception as e:
                last_error = str(e)
                if attempt <= max_retries:
                    retry_delay = 2 ** attempt
                    logger.warning(f"Action failed with exception, will retry in {retry_delay}s: {str(e)}")
                    await asyncio.sleep(retry_delay)
                else:
                    message = f"Action failed after {max_retries} retries: {str(e)}"
                    success = False
                    break
        
        # Use last error as message if we didn't get a specific message
        if not message and last_error:
            message = last_error
            
        # Calculate duration
        duration = time.monotonic() - start_time
        
        # Update action with execution results
        action.executed = True
        action.execution_timestamp = datetime.datetime.now(datetime.timezone.utc)
        action.execution_status = "succeeded" if success else "failed"
        action.execution_message = message
        action.execution_duration = duration
        action.retry_count = retry_count - 1  # Adjust for the initial execution
        
        # Log outcome
        if success:
            logger.info(f"Action {action_type} executed successfully in {duration:.2f}s: {message}")
        else:
            logger.error(f"Action {action_type} failed after {duration:.2f}s: {message}")
        
        # Record execution in database
        try:
            plan_id = None
            try:
                # Try to get the plan ID from the database
                result = await action_log_collection.database["remediation_plans"].find_one(
                    {"anomaly_id": anomaly_id, "completed": False},
                    {"_id": 1}
                )
                if result:
                    plan_id = result["_id"]
            except Exception as e:
                logger.warning(f"Failed to retrieve plan ID: {e}")
            
            # Create execution record
            execution_record = ExecutedActionRecord(
                anomaly_id=str(anomaly_id),
                plan_id=str(plan_id) if plan_id else str(ObjectId()),  # Use a generic ID if no plan found
                action_id=str(action.id) if action.id else str(ObjectId()),  # Use action ID if available
                action_type=action_type,
                parameters=params,
                entity_type=entity_type,
                entity_id=entity_id,
                namespace=namespace,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
                success=success,
                message=message[:500],  # Limit message length to avoid oversized documents
                duration_seconds=duration,
                score=anomaly_score,
                source_type=source_type,
                retry_attempt=retry_count - 1,
                error_details={
                    "retry_count": retry_count - 1,
                    "last_error": last_error,
                    "timeout_seconds": 60 if "node" in action_type else 30
                } if not success else None
            )
            
            # Store in database
            await action_log_collection.insert_one(execution_record.model_dump())
            
        except Exception as db_err:
            logger.error(f"Failed to record action execution failure: {db_err}")
        
        # After execution is complete, record the metrics
        try:
            if app_context and hasattr(app_context, 'record_remediation'):
                app_context.record_remediation(
                    action_type=action_type,
                    entity_type=entity_type,
                    namespace=namespace,
                    success=success,
                    duration=duration
                )
        except Exception as metrics_err:
            logger.warning(f"Failed to record remediation metrics: {metrics_err}")
        
        return success, message
        
    except Exception as e:
        error_msg = f"Unexpected error executing action {action_type}: {str(e)}"
        logger.exception(error_msg)
        
        # Try to record failure in database
        try:
            execution_record = ExecutedActionRecord(
                anomaly_id=str(anomaly_id),
                action_type=action_type,
                parameters=params,
                entity_type=entity_type,
                entity_id=entity_id,
                namespace=namespace,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
                success=False,
                message=f"Unexpected error: {str(e)}",
                duration_seconds=time.monotonic() - start_time,
                score=anomaly_score,
                source_type=source_type,
                error_details={"exception": str(e), "traceback": traceback.format_exc()}
            )
            
            await action_log_collection.insert_one(execution_record.model_dump())
            
            # Record metrics for the failed remediation action
            if app_context and hasattr(app_context, 'record_remediation'):
                try:
                    app_context.record_remediation(
                        action_type=action_type,
                        entity_type=entity_type,
                        namespace=namespace,
                        success=False,
                        duration=time.monotonic() - start_time
                    )
                except Exception as metrics_err:
                    logger.warning(f"Failed to record failure metrics: {metrics_err}")
                
        except Exception as db_err:
            logger.error(f"Failed to record action execution failure: {db_err}")
        
        return False, error_msg


async def execute_remediation_plan(
    plan: RemediationPlan,
    anomaly_record: AnomalyRecord,
    dependencies: PlannerDependencies,
    app_context=None,
    dry_run: bool = False,
) -> bool:
    """
    Executes the actions defined in a RemediationPlan with robust retry logic.
    
    Implements:
    - Thorough validation of plans
    - Exponential backoff retries for failing actions
    - Comprehensive logging of all actions with context fields
    - Parallel execution of independent actions when possible
    - Metrics tracking for operations monitoring
    - Dry run mode to simulate execution without making changes
    
    Args:
        plan: The RemediationPlan to execute
        anomaly_record: The anomaly that triggered remediation
        dependencies: Database and Kubernetes API client bundled in PlannerDependencies
        app_context: Optional AppContext for recording metrics
        dry_run: If True, simulate execution without making actual changes
        
    Returns:
        True if all actions executed successfully or simulated successfully, False otherwise
    """
    # Extract dependencies
    db = dependencies.db
    k8s_client = dependencies.k8s_client
    
    action_log_collection = db["executed_actions"]
    anomaly_collection = db["anomalies"]

    if not anomaly_record.id:
        logger.error("Cannot execute plan: AnomalyRecord is missing database ID")
        return False

    anomaly_id_obj: PyObjectId = anomaly_record.id
    anomaly_id_str = str(anomaly_id_obj)

    # --- Plan Validation ---
    try:
        validated_plan = RemediationPlan.model_validate(plan.model_dump())
        logger.debug(f"Remediation plan for anomaly {anomaly_id_str} passed validation")
    except ValidationError as e:
        error_msg = f"Generated remediation plan failed validation: {e}"
        logger.error(error_msg)
        await anomaly_collection.update_one(
            {"_id": anomaly_record.id},
            {"$set": {"remediation_status": "failed", "remediation_error": error_msg}}
        )
        return False
    
    # If no actions in plan, mark as completed
    if not validated_plan.actions:
        logger.info(f"Remediation plan has no actions (reasoning: {validated_plan.reasoning})")
        if not dry_run:
            await anomaly_collection.update_one(
                {"_id": anomaly_record.id}, 
                {"$set": {"remediation_status": "completed", "remediation_message": validated_plan.reasoning}}
            )
        return True

    # Get entity ID for structured logging
    entity_id = "unknown"
    if anomaly_record.entity_id:
        entity_id = anomaly_record.entity_id

    run_mode = "dry run" if dry_run else "execution"
    logger.info(
        f"Starting {run_mode} of remediation plan for anomaly {anomaly_id_str}. "
        f"Entity: {entity_id}. "
        f"Actions: {len(validated_plan.actions)}. Reasoning: \n{validated_plan.reasoning}"
    )
    
    if not dry_run:
        await anomaly_collection.update_one(
            {"_id": anomaly_record.id}, {"$set": {"remediation_status": "executing"}}
        )

    # Store dry run results
    dry_run_results = []

    # Determine if actions can be executed in parallel
    # For simplicity, we'll consider each action independent for now
    # In a more advanced version, you might analyze dependencies between actions
    # Use the configurable setting for max parallel actions
    can_parallelize = len(validated_plan.actions) > 1 and len(validated_plan.actions) <= settings.max_parallel_remediation_actions

    overall_success = True
    final_message = ""

    # Create a custom Kubernetes API client for dry runs if needed
    if dry_run:
        # For dry run, we'll need to create mock clients and simulate actions
        logger.info(f"Performing DRY RUN for remediation plan on anomaly {anomaly_id_str}")
        
        # Execute actions sequentially in dry run mode (parallelization doesn't make sense for simulation)
        for i, action in enumerate(validated_plan.actions):
            action_type = action.action_type
            params = action.parameters
            
            # Check if action exists in registry
            if action_type not in ACTION_REGISTRY:
                msg = f"Unknown action type '{action_type}' (Action {i+1}/{len(validated_plan.actions)})"
                logger.error(msg)
                dry_run_results.append({
                    "action_index": i,
                    "action_type": action_type,
                    "parameters": params,
                    "success": False,
                    "message": msg,
                    "simulated_impact": "Unknown impact - action type not registered"
                })
                overall_success = False
                continue
            
            # Simulate the action's impact
            try:
                # Prepare impact assessment based on action type
                impact_assessment = await simulate_action_impact(k8s_client, action)
                
                logger.info(f"DRY RUN - Simulated action {i+1}/{len(validated_plan.actions)}: {action_type} with parameters {params}")
                logger.info(f"DRY RUN - Impact assessment: {impact_assessment}")
                
                # Record dry run result
                dry_run_results.append({
                    "action_index": i,
                    "action_type": action_type,
                    "parameters": params,
                    "success": True,
                    "message": f"Simulated execution of {action_type}",
                    "simulated_impact": impact_assessment
                })
                
            except Exception as e:
                msg = f"DRY RUN - Failed to simulate action {action_type}: {str(e)}"
                logger.error(msg)
                dry_run_results.append({
                    "action_index": i,
                    "action_type": action_type,
                    "parameters": params,
                    "success": False,
                    "message": msg,
                    "simulated_impact": "Failed to simulate - potential error during execution"
                })
                overall_success = False
        
        # Update plan with dry run results if executing as part of a validation sequence
        if plan.dry_run_results is None:
            plan.dry_run_results = []
        plan.dry_run_results.extend(dry_run_results)
        
        # Calculate safety score based on simulation results
        successful_simulations = sum(1 for result in dry_run_results if result["success"])
        total_simulations = len(dry_run_results)
        safety_score = successful_simulations / total_simulations if total_simulations > 0 else 0.0
        plan.safety_score = safety_score
        
        # Generate risk assessment
        risk_level = "Low" if safety_score > 0.8 else "Medium" if safety_score > 0.5 else "High"
        plan.risk_assessment = f"{risk_level} risk. {successful_simulations} of {total_simulations} actions simulated successfully."
        
        if overall_success:
            final_message = f"Dry run completed successfully. All {len(validated_plan.actions)} action(s) can be executed safely."
            logger.info(final_message)
        else:
            final_message = f"Dry run revealed potential issues with {len(validated_plan.actions) - successful_simulations} of {len(validated_plan.actions)} action(s)."
            logger.warning(final_message)
        
        return overall_success
    
    # Execute the plan for real (not dry run)
    if can_parallelize:
        # Execute actions in parallel with asyncio.gather
        logger.info(f"Executing {len(validated_plan.actions)} actions in parallel for anomaly {anomaly_id_str}")
        
        # Prepare coroutines for each action
        action_coroutines = []
        for i, action in enumerate(validated_plan.actions):
            action_coro = execute_action_with_timeout(
                action=action,
                api_client=k8s_client,
                action_idx=i+1,
                total_actions=len(validated_plan.actions),
                anomaly_id=anomaly_id_obj,
                action_log_collection=action_log_collection,
                entity_id=entity_id,
                anomaly_score=anomaly_record.anomaly_score,
                source_type=anomaly_record.data_source or "unknown",
                app_context=app_context
            )
            action_coroutines.append(action_coro)
        
        # Execute all actions in parallel and collect results
        try:
            results = await asyncio.gather(*action_coroutines, return_exceptions=True)
            
            # Process results, checking for exceptions
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Action {i+1} failed with exception: {result}")
                    overall_success = False
                    final_message = f"Action {i+1} failed: {str(result)}"
                elif isinstance(result, tuple) and len(result) == 2:
                    success, msg = result
                    if not success:
                        overall_success = False
                        logger.error(f"Action {i+1} reported failure: {msg}")
                        final_message = msg
                else:
                    logger.error(f"Action {i+1} returned unexpected result: {result}")
                    overall_success = False
                    final_message = f"Action {i+1} failed with unexpected result"
            
            if overall_success:
                logger.info(f"All parallel actions completed successfully for anomaly {anomaly_id_str}")
                final_message = "All actions executed successfully"
                
        except Exception as e:
            logger.exception(f"Parallel action execution failed for anomaly {anomaly_id_str}: {e}")
            overall_success = False
            final_message = f"Parallel execution failed: {str(e)}"
    else:
        # Execute actions sequentially
        for i, action in enumerate(validated_plan.actions):
            success, msg = await execute_action_with_timeout(
                action=action,
                api_client=k8s_client,
                action_idx=i+1,
                total_actions=len(validated_plan.actions),
                anomaly_id=anomaly_id_obj,
                action_log_collection=action_log_collection,
                entity_id=entity_id,
                anomaly_score=anomaly_record.anomaly_score,
                source_type=anomaly_record.data_source or "unknown",
                app_context=app_context
            )
            
            final_message = msg
            if not success:
                overall_success = False
                logger.error(
                    f"Remediation plan execution failed at action {i+1}/{len(validated_plan.actions)}. "
                    f"Stopping. Anomaly ID: {anomaly_id_str}"
                )
                break

    # Update final status on the AnomalyRecord
    final_status = "completed" if overall_success else "failed"
    update_doc = {"$set": {"remediation_status": final_status}}
    if not overall_success:
        update_doc["$set"]["remediation_error"] = final_message
    else:
        update_doc["$set"]["remediation_message"] = "Successfully executed all remediation actions"

    try:
        await anomaly_collection.update_one({"_id": anomaly_record.id}, update_doc)
        logger.info(f"Remediation plan execution for anomaly {anomaly_id_str} finished with status: {final_status}")
    except Exception as e:
        logger.exception(f"Failed to update final anomaly status for {anomaly_id_str}: {e}")

    return overall_success


async def simulate_action_impact(
    api_client: client.ApiClient, 
    action: RemediationAction
) -> str:
    """
    Simulates the impact of executing an action without making actual changes.
    
    Args:
        api_client: An initialized Kubernetes ApiClient
        action: The RemediationAction to simulate
        
    Returns:
        A string describing the predicted impact
    """
    action_type = action.action_type
    params = action.parameters
    
    # Simulate based on action type
    if action_type == "scale_deployment":
        # Get current deployment details
        apps_v1_api = client.AppsV1Api(api_client)
        name = params.get("name")
        namespace = params.get("namespace", "default")
        target_replicas = params.get("replicas")
        
        try:
            # Get current deployment state
            deployment = await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
            current_replicas = deployment.spec.replicas
            
            # Handle placeholder
            if isinstance(target_replicas, str) and "{current_replicas + 1}" in target_replicas:
                target_replicas = current_replicas + 1
                
            # Determine impact
            if current_replicas == target_replicas:
                return f"No change in replica count (already at {current_replicas})"
            elif current_replicas < target_replicas:
                return f"Scale up from {current_replicas} to {target_replicas} replicas. Increased pod count may affect resource usage."
            else:
                return f"Scale down from {current_replicas} to {target_replicas} replicas. Decreased capacity may impact service availability."
                
        except client.ApiException as e:
            if e.status == 404:
                return f"Deployment '{namespace}/{name}' not found - action will fail"
            return f"Error accessing deployment: {e.reason}"
            
    elif action_type == "delete_pod":
        # Simulate pod deletion
        core_v1_api = client.CoreV1Api(api_client)
        name = params.get("name")
        namespace = params.get("namespace")
        
        try:
            # Get pod details
            pod = await core_v1_api.read_namespaced_pod(name=name, namespace=namespace)
            
            # Check if pod is managed by a controller
            owner_references = pod.metadata.owner_references or []
            
            if owner_references:
                controller_kind = owner_references[0].kind
                controller_name = owner_references[0].name
                return f"Pod will be deleted and replaced by controller {controller_kind}/{controller_name}. Brief service disruption may occur."
            else:
                return "Pod will be permanently deleted. No controller will replace it, which may cause service disruption."
                
        except client.ApiException as e:
            if e.status == 404:
                return f"Pod '{namespace}/{name}' not found - action will effectively do nothing"
            return f"Error accessing pod: {e.reason}"
            
    elif action_type == "restart_deployment":
        # Simulate deployment restart
        apps_v1_api = client.AppsV1Api(api_client)
        name = params.get("name")
        namespace = params.get("namespace", "default")
        
        try:
            # Get deployment details
            deployment = await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
            replicas = deployment.spec.replicas
            
            return f"All {replicas} pod(s) of deployment will be restarted. Rolling restart will be performed to minimize disruption."
                
        except client.ApiException as e:
            if e.status == 404:
                return f"Deployment '{namespace}/{name}' not found - action will fail"
            return f"Error accessing deployment: {e.reason}"
    
    elif action_type == "drain_node":
        # Simulate node draining
        core_v1_api = client.CoreV1Api(api_client)
        name = params.get("name")
        force = params.get("force", False)
        
        try:
            # Get node details
            node = await core_v1_api.read_node(name=name)
            
            # Get pods on the node
            field_selector = f"spec.nodeName={name}"
            pods = await core_v1_api.list_pod_for_all_namespaces(field_selector=field_selector)
            pod_count = len(pods.items)
            
            # Check for daemonsets
            daemon_pods = [p for p in pods.items if any(
                owner.kind == "DaemonSet" for owner in (p.metadata.owner_references or [])
            )]
            daemon_count = len(daemon_pods)
            
            if node.spec.unschedulable:
                base_message = f"Node is already cordoned. {pod_count} pod(s) will be evicted"
            else:
                base_message = f"Node will be cordoned and {pod_count} pod(s) will be evicted"
                
            if daemon_count > 0 and not force:
                return f"{base_message}. {daemon_count} DaemonSet pod(s) will be preserved due to force=False."
            elif daemon_count > 0:
                return f"{base_message}, including {daemon_count} DaemonSet pod(s) due to force=True."
            else:
                return base_message
                
        except client.ApiException as e:
            if e.status == 404:
                return f"Node '{name}' not found - action will fail"
            return f"Error accessing node: {e.reason}"
    
    elif action_type == "taint_node":
        # Simulate node tainting
        core_v1_api = client.CoreV1Api(api_client)
        name = params.get("name")
        key = params.get("key")
        value = params.get("value")
        effect = params.get("effect")
        
        try:
            # Get node details
            node = await core_v1_api.read_node(name=name)
            
            # Check current taints
            current_taints = node.spec.taints or []
            for taint in current_taints:
                if taint.key == key and taint.effect == effect:
                    return f"Node already has taint {key}={value}:{effect} - no change will occur"
            
            # Determine impact based on effect
            if effect == "NoSchedule":
                return f"Adding taint {key}={value}:{effect} - new pods without matching toleration will not be scheduled on this node"
            elif effect == "PreferNoSchedule":
                return f"Adding taint {key}={value}:{effect} - scheduler will try to avoid placing new pods without matching toleration on this node"
            elif effect == "NoExecute":
                # Get pods on the node
                field_selector = f"spec.nodeName={name}"
                pods = await core_v1_api.list_pod_for_all_namespaces(field_selector=field_selector)
                
                # Count pods that would be affected
                affected_pods = 0
                for pod in pods.items:
                    # Check if pod has tolerations for this taint
                    tolerations = pod.spec.tolerations or []
                    has_matching_toleration = any(
                        t.key == key and (t.effect == effect or t.effect == "") for t in tolerations
                    )
                    if not has_matching_toleration:
                        affected_pods += 1
                
                return f"Adding taint {key}={value}:{effect} - {affected_pods} existing pod(s) without matching toleration will be evicted"
        
        except client.ApiException as e:
            if e.status == 404:
                return f"Node '{name}' not found - action will fail"
            return f"Error accessing node: {e.reason}"
    
    elif action_type == "evict_pod":
        # Similar to delete_pod, but check PodDisruptionBudgets
        core_v1_api = client.CoreV1Api(api_client)
        policy_v1_api = client.PolicyV1Api(api_client)
        name = params.get("name")
        namespace = params.get("namespace")
        
        try:
            # Get pod details
            pod = await core_v1_api.read_namespaced_pod(name=name, namespace=namespace)
            
            # Check if pod is part of a controller
            owner_references = pod.metadata.owner_references or []
            
            # Try to find matching PDBs
            pdbs = await policy_v1_api.list_namespaced_pod_disruption_budget(namespace=namespace)
            matching_pdbs = []
            
            for pdb in pdbs.items:
                selector = pdb.spec.selector
                # Simplified check - in reality need to match labels
                if selector and pod.metadata.labels:
                    # This is a simplification - would need to properly match selectors
                    matching_pdbs.append(pdb.metadata.name)
            
            if matching_pdbs:
                return f"Pod will be evicted if PDBs allow: {', '.join(matching_pdbs)}. May be blocked if disruption budget is at limit."
            elif owner_references:
                controller_kind = owner_references[0].kind
                controller_name = owner_references[0].name
                return f"Pod will be evicted and replaced by controller {controller_kind}/{controller_name}. No PDBs found that would block eviction."
            else:
                return "Pod will be permanently evicted. No controller will replace it, which may cause service disruption."
                
        except client.ApiException as e:
            if e.status == 404:
                return f"Pod '{namespace}/{name}' not found - action will effectively do nothing"
            return f"Error accessing pod: {e.reason}"
    
    # Add simulations for other action types
    else:
        # Generic simulation for actions without specific handling
        return f"Will execute {action_type} with parameters {params}. Specific impact cannot be predicted without a custom simulator."


async def process_anomaly(
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    ai_agent: Agent,
) -> bool:
    """
    Processes an anomaly by generating and executing a remediation plan.
    
    Implements the following flow:
    1. Check if this anomaly has already been remediated recently
    2. Generate a remediation plan using AI or static template
    3. Execute the plan and record results
    
    Args:
        anomaly_record: The anomaly record to process
        db: MongoDB database instance
        ai_agent: The Generative AI agent for planning
        
    Returns:
        True if remediation was successful, False otherwise
    """
    entity_id = anomaly_record.entity_id
    if not entity_id:
        logger.error(f"Anomaly record {anomaly_record.id} has no entity_id")
        return False
    
    # Initialize API client for Kubernetes operations
    try:
        # Load Kubernetes configuration
        await load_k8s_config()
        # Create API client
        k8s_client = client.ApiClient()
    except Exception as e:
        logger.exception(f"Failed to initialize Kubernetes client: {e}")
        return False
    
    try:
        # Check remediation history for this entity
        action_collection = db["executed_actions"]
        recent_timestamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=settings.remediation_cooldown_seconds
        )
        
        # Look for recent successful actions for this entity
        recent_actions = await action_collection.find_one({
            "entity_id": entity_id,
            "executed_at": {"$gt": recent_timestamp},
            "success": True
        })
        
        if recent_actions:
            logger.info(
                f"Skipping remediation for {entity_id} - "
                f"recently remediated within cooldown period "
                f"({settings.remediation_cooldown_seconds}s)"
            )
            return False  # Skip remediation due to cooldown
        
        # Generate remediation plan
        # First try AI-powered plan if agent is available
        if ai_agent is not None:
            logger.info(f"Generating AI-powered remediation plan for {entity_id}")
            remediation_plan = await generate_remediation_plan(
                anomaly_record=anomaly_record,
                db=db,
                k8s_api_client=k8s_client,
                agent=ai_agent
            )
        else:
            # Fall back to static plan if AI agent unavailable
            logger.info(f"Generating static remediation plan for {entity_id}")
            remediation_plan = await load_static_plan(
                anomaly_record=anomaly_record,
                db=db,
                k8s_client=k8s_client
            )
        
        if not remediation_plan:
            logger.warning(f"No remediation plan was generated for {entity_id}")
            return False
        
        # Set the anomaly ID on the plan
        remediation_plan.anomaly_id = str(anomaly_record.id)
        
        # Execute the remediation plan
        success = await execute_remediation_plan(
            plan=remediation_plan,
            anomaly_record=anomaly_record,
            dependencies=PlannerDependencies(db=db, k8s_client=k8s_client),
            app_context=None
        )
        
        # Close K8s client when done
        await k8s_client.close()
        
        return success
        
    except Exception as e:
        logger.exception(f"Error processing anomaly {anomaly_record.id} for {entity_id}: {e}")
        # Make sure to clean up API client even on error
        try:
            if 'k8s_client' in locals():
                await k8s_client.close()
        except Exception as close_err:
            logger.error(f"Error closing Kubernetes client: {close_err}")
        return False


async def main():
    """Production-ready main function for running the engine separately."""
    from motor.motor_asyncio import AsyncIOMotorClient
    from kubewise.config import settings
    from kubewise.models import AnomalyRecord
    from pydantic_ai import Agent
    from pydantic_ai.models.gemini import GeminiModel
    from pydantic_ai.providers.google_gla import GoogleGLAProvider
    
    try:
        # Connect to MongoDB
        client = AsyncIOMotorClient(str(settings.mongo_uri))
        db = client[settings.mongo_db_name]
        
        # Initialize API client
        await load_k8s_config()
        k8s_client = client.ApiClient()
        
        # Initialize Gemini Agent if API key is available
        ai_agent = None
        if settings.gemini_api_key and settings.gemini_api_key.get_secret_value() != "changeme":
            try:
                # Initialize provider with API key
                provider = GoogleGLAProvider(api_key=settings.gemini_api_key.get_secret_value())
                # Initialize model with the provider and model ID from settings
                model = GeminiModel(settings.gemini_model_id, provider=provider)
                # Initialize Agent with the configured model
                ai_agent = Agent(model)
                logger.info(f"Gemini agent initialized with model '{settings.gemini_model_id}'.")
            except Exception as agent_err:
                logger.exception(f"Failed to initialize Gemini agent: {agent_err}")
                ai_agent = None
                logger.error("Gemini agent failed to initialize. Using static remediation planning.")
        
        # Process an anomaly from the database
        anomaly_id = input("Enter the ID of the anomaly to process: ")
        anomaly_record = await db["anomalies"].find_one({"_id": ObjectId(str(anomaly_id))})
        
        if not anomaly_record:
            logger.error(f"Anomaly with ID {anomaly_id} not found")
            return
        
        # Convert to Pydantic model
        anomaly = AnomalyRecord.model_validate(anomaly_record)
        
        # Process the anomaly
        success = await process_anomaly(anomaly, db, ai_agent)
        
        if success:
            logger.info(f"Successfully remediated anomaly {anomaly_id}")
        else:
            logger.error(f"Failed to remediate anomaly {anomaly_id}")
        
        # Clean up
        await k8s_client.close()
        client.close()
        
    except Exception as e:
        logger.exception(f"Error in main: {e}")


if __name__ == "__main__":
    from kubewise.logging import setup_logging
    import asyncio
    
    setup_logging()
    asyncio.run(main())
