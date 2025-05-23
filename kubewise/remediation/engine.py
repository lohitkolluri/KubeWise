import asyncio
import datetime
import time
import traceback
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Tuple,
    TypeVar,
)

import motor.motor_asyncio
from bson import ObjectId
from kubernetes_asyncio import client
from kubewise.logging import get_logger
from pydantic import ValidationError
from pydantic_ai import Agent

# Initialize logger with component name
logger = get_logger("remediation.engine")

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
from kubewise.utils.email import send_email
from kubewise.remediation.planner import (
    PlannerDependencies,
    generate_remediation_plan,
    load_static_plan,
    get_pod_controller,
    find_matching_daemonset,
)

# Type definitions
T = TypeVar("T")
ActionCoroutine = Callable[
    [client.ApiClient, Dict[str, Any]], Coroutine[Any, Any, Tuple[bool, str]]
]

# Constants
ACTION_TIMEOUT = 60.0  # seconds before action times out

# Registry for remediation actions
ACTION_REGISTRY: Dict[ActionType, ActionCoroutine] = {}


def register_action(
    action_type: ActionType,
) -> Callable[[ActionCoroutine], ActionCoroutine]:
    """
    Decorator to register a function as a handler for a specific remediation action.

    Args:
        action_type: The action type identifier (e.g., 'scale_deployment').

    Returns:
        The decorator function.
    """

    def decorator(func: ActionCoroutine) -> ActionCoroutine:
        if action_type in ACTION_REGISTRY:
            logger.warning(
                f"Action type '{action_type}' is already registered. Overwriting."
            )
        # Use a standardized message for consistent logging
        logger.debug(f"Registered handler: {action_type}")
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

    logger.debug(
        f"scale_deployment_action received replicas: type={type(replicas)}, value='{replicas}'"
    )

    target_replicas: int
    expected_placeholder = "{current_replicas + 1}"
    is_placeholder = (
        isinstance(replicas, str) and replicas.strip() == expected_placeholder
    )
    logger.debug(
        f"Checking for placeholder: received='{replicas}', expected='{expected_placeholder}', is_match={is_placeholder}"
    )

    # Handle placeholder string for replicas
    if is_placeholder:
        try:
            logger.debug(f"Attempting to fetch current replicas for {namespace}/{name}")
            current_scale = await apps_v1_api.read_namespaced_deployment_scale(
                name=name, namespace=namespace
            )
            current_replicas = (
                current_scale.spec.replicas
                if current_scale.spec and current_scale.spec.replicas is not None
                else 0
            )
            logger.debug(f"Fetched current replicas: {current_replicas}")
            target_replicas = current_replicas + 1
            logger.info(
                f"Placeholder detected. Current replicas: {current_replicas}. Target replicas: {target_replicas}"
            )
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
        return (
            False,
            f"Invalid 'replicas' value: {replicas}. Must be a non-negative integer or '{{current_replicas + 1}}'.",
        )

    patch_body = {"spec": {"replicas": target_replicas}}
    try:
        logger.info(
            f"Scaling deployment '{namespace}/{name}' to {target_replicas} replicas..."
        )
        await apps_v1_api.patch_namespaced_deployment_scale(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully scaled deployment '{namespace}/{name}' to {target_replicas} replicas."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = (
            f"Failed to scale deployment '{namespace}/{name}': "
            f"{e.status} - {e.reason} - {e.body}"
        )
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
            return True, msg  # Treat as success if already gone
        else:
            msg = (
                f"Failed to delete pod '{namespace}/{name}': "
                f"{e.status} - {e.reason} - {e.body}"
            )
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
                        "annotations": {"kubectl.kubernetes.io/restartedAt": timestamp}
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
        msg = (
            f"Failed to restart deployment '{namespace}/{name}': "
            f"{e.status} - {e.reason} - {e.body}"
        )
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error restarting deployment '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("restart_daemonset")
async def restart_daemonset_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Restart a Kubernetes DaemonSet by adding a restart annotation.
    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name' and optionally 'namespace'.
    Returns:
        Tuple (success: bool, message: str).
    """
    apps_v1_api = client.AppsV1Api(api_client)
    core_v1_api = client.CoreV1Api(api_client)
    name = params.get("name")
    namespace = params.get("namespace", "default")
    pod_name = params.get("pod_name")  # Optional pod name if this is called for a pod

    if not name:
        return False, "Missing 'name' parameter for restart_daemonset"
    
    # Initialize original_name to keep track of what was passed in
    original_name = name
    
    # Handle case when a DaemonSet pod name with node suffix is passed instead of the DaemonSet name
    # Example: ama-metrics-node-fvh6v should look for ama-metrics
    if "-node-" in name:
        # Extract the base name (assuming format: daemonset-name-node-suffix)
        parts = name.split("-node-")
        if len(parts) >= 1:
            name = parts[0]
            logger.info(f"Extracted potential DaemonSet name '{name}' from pod name '{original_name}'")

    # Try to restart the DaemonSet with the name we have
    try:
        # First attempt to restart the DaemonSet using the extracted name
        patch_body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubewise.io/restartedAt": datetime.datetime.utcnow().isoformat()
                        }
                    }
                }
            }
        }
        logger.info(f"Restarting DaemonSet: {namespace}/{name}")
        await apps_v1_api.patch_namespaced_daemon_set(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully restarted daemonset '{namespace}/{name}'."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        # If we get a 404 error, let's try to find the actual DaemonSet
        if e.status == 404:
            logger.warning(f"DaemonSet '{namespace}/{name}' not found. Attempting to discover the actual DaemonSet name.")
            
            # Use our advanced controller discovery function first
            pod_name_to_check = pod_name if pod_name else original_name
            
            # Option 1: Try to get the controller directly using owner references
            controller_kind, controller_name = await get_pod_controller(api_client, pod_name_to_check, namespace)
            
            if controller_kind == "DaemonSet" and controller_name:
                # Found the DaemonSet through owner references, restart it
                try:
                    patch_body = {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "annotations": {
                                        "kubewise.io/restartedAt": datetime.datetime.utcnow().isoformat()
                                    }
                                }
                            }
                        }
                    }
                    logger.info(f"Restarting DaemonSet: {namespace}/{controller_name}")
                    await apps_v1_api.patch_namespaced_daemon_set(
                        name=controller_name, namespace=namespace, body=patch_body
                    )
                    msg = f"Successfully restarted daemonset '{namespace}/{controller_name}' (discovered through owner references)."
                    logger.info(msg)
                    return True, msg
                except client.ApiException as e2:
                    msg = (
                        f"Failed to restart discovered daemonset '{namespace}/{controller_name}': "
                        f"{e2.status} - {e2.reason} - {e2.body}"
                    )
                    logger.error(msg)
                    return False, msg
            
            # Option 2: If owner reference approach didn't find a DaemonSet, try pattern matching
            matching_daemonset = await find_matching_daemonset(api_client, namespace, pod_name_to_check)
            
            if matching_daemonset:
                try:
                    patch_body = {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "annotations": {
                                        "kubewise.io/restartedAt": datetime.datetime.utcnow().isoformat()
                                    }
                                }
                            }
                        }
                    }
                    logger.info(f"Restarting DaemonSet: {namespace}/{matching_daemonset}")
                    await apps_v1_api.patch_namespaced_daemon_set(
                        name=matching_daemonset, namespace=namespace, body=patch_body
                    )
                    msg = f"Successfully restarted daemonset '{namespace}/{matching_daemonset}' (found through pattern matching)."
                    logger.info(msg)
                    return True, msg
                except client.ApiException as e3:
                    msg = (
                        f"Failed to restart matched daemonset '{namespace}/{matching_daemonset}': "
                        f"{e3.status} - {e3.reason} - {e3.body}"
                    )
                    logger.error(msg)
                    return False, msg
            
            # Option 3: Fall back to the original discovery method if both new methods fail
            try:
                # Check if the original name is a pod
                try:
                    pod = await core_v1_api.read_namespaced_pod(name=pod_name_to_check, namespace=namespace)
                    
                    # Check if the pod has owner references
                    if pod.metadata.owner_references:
                        for owner_ref in pod.metadata.owner_references:
                            # If the owner is a DaemonSet, use that name
                            if owner_ref.kind == "DaemonSet":
                                actual_ds_name = owner_ref.name
                                logger.info(f"Discovered actual DaemonSet name '{actual_ds_name}' from pod '{pod_name_to_check}'")
                                
                                # Now restart the actual DaemonSet
                                try:
                                    patch_body = {
                                        "spec": {
                                            "template": {
                                                "metadata": {
                                                    "annotations": {
                                                        "kubewise.io/restartedAt": datetime.datetime.utcnow().isoformat()
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    logger.info(f"Restarting actual daemonset '{namespace}/{actual_ds_name}'...")
                                    await apps_v1_api.patch_namespaced_daemon_set(
                                        name=actual_ds_name, namespace=namespace, body=patch_body
                                    )
                                    msg = f"Successfully restarted daemonset '{namespace}/{actual_ds_name}' (discovered from pod '{pod_name_to_check}')."
                                    logger.info(msg)
                                    return True, msg
                                except client.ApiException as e2:
                                    msg = (
                                        f"Failed to restart discovered daemonset '{namespace}/{actual_ds_name}': "
                                        f"{e2.status} - {e2.reason} - {e2.body}"
                                    )
                                    logger.error(msg)
                                    return False, msg
                
                    # If we get here, the pod exists but doesn't have a DaemonSet owner
                    logger.warning(f"Pod '{namespace}/{pod_name_to_check}' exists but is not owned by a DaemonSet")
                    
                    # Try to list all DaemonSets in the namespace as a last resort
                    try:
                        daemonsets = await apps_v1_api.list_namespaced_daemon_set(namespace=namespace)
                        if daemonsets.items:
                            # Look for a DaemonSet that might match our pod name pattern
                            for ds in daemonsets.items:
                                ds_name = ds.metadata.name
                                # Check if DS name is in pod name
                                if ds_name in pod_name_to_check:
                                    logger.info(f"Found potential matching DaemonSet '{ds_name}' for pod '{pod_name_to_check}'")
                                    # Try to restart this DaemonSet
                                    try:
                                        patch_body = {
                                            "spec": {
                                                "template": {
                                                    "metadata": {
                                                        "annotations": {
                                                            "kubewise.io/restartedAt": datetime.datetime.utcnow().isoformat()
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        logger.info(f"Restarting potential matching daemonset '{namespace}/{ds_name}'...")
                                        await apps_v1_api.patch_namespaced_daemon_set(
                                            name=ds_name, namespace=namespace, body=patch_body
                                        )
                                        msg = f"Successfully restarted daemonset '{namespace}/{ds_name}' (name matched from pod pattern)."
                                        logger.info(msg)
                                        return True, msg
                                    except client.ApiException as e3:
                                        logger.warning(f"Failed to restart potential matching daemonset '{namespace}/{ds_name}': {e3.status} - {e3.reason}")
                            
                            # If we haven't returned yet, we couldn't find a match
                            logger.warning(f"Found {len(daemonsets.items)} daemonsets in namespace '{namespace}' but none matched the pod pattern")
                            daemonset_names = [ds.metadata.name for ds in daemonsets.items]
                            return False, f"Failed to find matching DaemonSet for '{original_name}'. Available DaemonSets: {', '.join(daemonset_names)}"
                        else:
                            return False, f"No DaemonSets found in namespace '{namespace}'"
                    except client.ApiException as e4:
                        return False, f"Failed to list DaemonSets in namespace '{namespace}': {e4.status} - {e4.reason}"
                
                except client.ApiException as pod_error:
                    # The original name is not a pod either, return the original error
                    logger.warning(f"Neither DaemonSet '{name}' nor Pod '{pod_name_to_check}' exist in namespace '{namespace}'")
                    pass
            
            except Exception as discovery_error:
                logger.error(f"Error while trying to discover DaemonSet: {discovery_error}")
                pass
            
            # If all discovery attempts fail, return the original error
            msg = (
                f"Failed to restart daemonset '{namespace}/{name}': "
                f"{e.status} - {e.reason} - {e.body}"
            )
            logger.error(msg)
            return False, msg
        else:
            # For other errors, just return them
            msg = (
                f"Failed to restart daemonset '{namespace}/{name}': "
                f"{e.status} - {e.reason} - {e.body}"
            )
            logger.error(msg)
            return False, msg
    except Exception as e:
        msg = f"Unexpected error restarting daemonset '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("restart_statefulset")
async def restart_statefulset_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Restart a Kubernetes StatefulSet by adding a restart annotation.
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
        return False, "Missing 'name' parameter for restart_statefulset"

    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubewise.io/restartedAt": datetime.datetime.utcnow().isoformat()
                    }
                }
            }
        }
    }
    try:
        logger.info(f"Restarting statefulset '{namespace}/{name}'...")
        await apps_v1_api.patch_namespaced_stateful_set(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully restarted statefulset '{namespace}/{name}'."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = (
            f"Failed to restart statefulset '{namespace}/{name}': "
            f"{e.status} - {e.reason} - {e.body}"
        )
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error restarting statefulset '{namespace}/{name}': {e}"
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
        field_selector = (
            f"spec.nodeName={name},status.phase!=Failed,status.phase!=Succeeded"
        )
        pods = await core_v1_api.list_pod_for_all_namespaces(
            field_selector=field_selector
        )

        pods_count = len(pods.items)
        logger.info(f"Node '{name}' has {pods_count} pods to evict")

        if pods_count == 0:
            return True, f"Node '{name}' successfully cordoned with no pods to evict"

        # Step 3: Evict each pod with grace period
        evicted_count = 0
        for pod in pods.items:
            # Skip DaemonSet pods if not force
            if not force and any(
                owner.kind == "DaemonSet"
                for owner in pod.metadata.owner_references or []
            ):
                logger.info(
                    f"Skipping DaemonSet pod '{pod.metadata.namespace}/{pod.metadata.name}'"
                )
                continue

            logger.info(
                f"Evicting pod '{pod.metadata.namespace}/{pod.metadata.name}' from node '{name}'"
            )
            # Create eviction object
            eviction_body = {
                "apiVersion": "policy/v1",
                "kind": "Eviction",
                "metadata": {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                },
                "deleteOptions": {"gracePeriodSeconds": grace_period},
            }

            try:
                # We use the generic API since evictions are special
                await api_client.post(
                    f"/api/v1/namespaces/{pod.metadata.namespace}/pods/{pod.metadata.name}/eviction",
                    body=eviction_body,
                )
                evicted_count += 1
            except client.ApiException as pod_e:
                if pod_e.status == 429:  # Too Many Requests
                    logger.warning(
                        f"Pod eviction throttled, waiting 10s: '{pod.metadata.namespace}/{pod.metadata.name}'"
                    )
                    await asyncio.sleep(10)
                    # Continue with next pod, we'll let caller retry the drain if needed
                else:
                    logger.error(
                        f"Failed to evict pod '{pod.metadata.namespace}/{pod.metadata.name}': {pod_e.reason}"
                    )

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
    namespace = params.get(
        "namespace", "default"
    )  # Default to 'default' if not provided

    if not name or replicas is None:
        return False, "Missing 'name' or 'replicas' parameter for scale_statefulset"

    # --- Debugging Replica Value ---
    logger.debug(
        f"scale_statefulset_action received replicas: type={type(replicas)}, value='{replicas}'"
    )
    # --- End Debugging ---

    target_replicas: int
    expected_placeholder = "{current_replicas + 1}"
    # More robust check: strip whitespace from input 'replicas' before comparison
    is_placeholder = (
        isinstance(replicas, str) and replicas.strip() == expected_placeholder
    )
    logger.debug(
        f"Checking for placeholder: received='{replicas}', expected='{expected_placeholder}', is_match={is_placeholder}"
    )

    # Handle placeholder string for replicas
    if is_placeholder:
        try:
            logger.debug(f"Attempting to fetch current replicas for {namespace}/{name}")
            current_scale = await apps_v1_api.read_namespaced_stateful_set_scale(
                name=name, namespace=namespace
            )
            current_replicas = (
                current_scale.spec.replicas
                if current_scale.spec and current_scale.spec.replicas is not None
                else 0
            )
            logger.debug(f"Fetched current replicas: {current_replicas}")
            target_replicas = current_replicas + 1
            logger.info(
                f"Placeholder detected. Current replicas: {current_replicas}. Target replicas: {target_replicas}"
            )
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
        return (
            False,
            f"Invalid 'replicas' value: {replicas}. Must be a non-negative integer or '{{current_replicas + 1}}'.",
        )

    patch_body = {"spec": {"replicas": target_replicas}}
    try:
        logger.info(
            f"Scaling statefulset '{namespace}/{name}' to {target_replicas} replicas..."
        )
        await apps_v1_api.patch_namespaced_stateful_set_scale(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully scaled statefulset '{namespace}/{name}' to {target_replicas} replicas."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = (
            f"Failed to scale statefulset '{namespace}/{name}': "
            f"{e.status} - {e.reason} - {e.body}"
        )
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
    Add a taint to a Kubernetes node.

    Args:
        api_client: Kubernetes API client
        params: Dictionary with the following keys:
            - name: Name of the node
            - key: Taint key
            - value: Taint value
            - effect: Taint effect (NoSchedule, PreferNoSchedule, NoExecute)

    Returns:
        Tuple of (success, message)
    """
    name = params.get("name")
    key = params.get("key")
    value = params.get("value", "")
    effect = params.get("effect", "NoSchedule")

    if not name:
        return False, "Node name is required"

    if not key:
        return False, "Taint key is required"

    if effect not in ["NoSchedule", "PreferNoSchedule", "NoExecute"]:
        return False, f"Invalid taint effect: {effect}. Must be one of: NoSchedule, PreferNoSchedule, NoExecute"

    try:
        core_v1_api = client.CoreV1Api(api_client)
        
        # Get the current node
        try:
            node = await core_v1_api.read_node(name=name)
        except client.ApiException as e:
            return False, f"Failed to get node {name}: {e.reason}"
        
        # Create the taint
        new_taint = client.V1Taint(
            key=key,
            value=value,
            effect=effect
        )
        
        # Check if the taint already exists
        if node.spec.taints:
            for taint in node.spec.taints:
                if taint.key == key and taint.effect == effect:
                    return True, f"Taint {key}={value}:{effect} already exists on node {name}"
            
            # Add the new taint to the existing taints
            node.spec.taints.append(new_taint)
        else:
            # Initialize taints list with the new taint
            node.spec.taints = [new_taint]
        
        # Update the node
        try:
            await core_v1_api.patch_node(
                name=name,
                body={"spec": {"taints": node.spec.taints}}
            )
            return True, f"Added taint {key}={value}:{effect} to node {name}"
        except client.ApiException as e:
            return False, f"Failed to add taint to node {name}: {e.reason}"
        
    except Exception as e:
        return False, f"Error adding taint to node: {str(e)}"


@register_action("evict_pod")
async def evict_pod_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Evicts a specific Kubernetes Pod.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name', 'namespace', and optionally 'grace_period_seconds'.

    Returns:
        Tuple (success: bool, message: str).
    """
    core_v1_api = client.CoreV1Api(api_client)
    name = params.get("name")
    namespace = params.get("namespace")
    grace_period = params.get("grace_period_seconds", 30)  # Default 30s grace period

    if not name or not namespace:
        return False, "Missing 'name' or 'namespace' parameter for evict_pod"

    try:
        # Create eviction object
        eviction_body = client.V1Eviction(
            api_version="policy/v1",
            kind="Eviction",
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace
            ),
            delete_options=client.V1DeleteOptions(
                grace_period_seconds=grace_period
            )
        )

        logger.info(f"Evicting pod '{namespace}/{name}' with grace period {grace_period}s...")
        # Use the CoreV1Api create_namespaced_pod_eviction method
        await core_v1_api.create_namespaced_pod_eviction(
            name=name,
            namespace=namespace,
            body=eviction_body
        )
        msg = f"Successfully initiated eviction for pod '{namespace}/{name}'."
        logger.info(msg)
        # Eviction is asynchronous in K8s. We report success on initiating.
        return True, msg
    except client.ApiException as e:
        # Handle 'Not Found' gracefully - maybe the pod was already evicted
        if e.status == 404:
            msg = f"Pod '{namespace}/{name}' not found. Assuming already evicted."
            logger.warning(msg)
            return True, msg  # Treat as success if already gone
        elif e.status == 429:  # Too Many Requests (throttled)
            msg = f"Eviction for pod '{namespace}/{name}' throttled (Too Many Requests)."
            logger.warning(msg)
            return False, msg  # Indicate failure, caller might retry
        else:
            msg = (
                f"Failed to evict pod '{namespace}/{name}': "
                f"{e.status} - {e.reason} - {e.body}"
            )
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
    Vertically scales a Kubernetes Deployment by updating resource requests/limits.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name', optionally 'namespace', and 'resources'
                OR 'resource' (e.g., 'cpu', 'memory') and 'value' (e.g., '500m', '1Gi').
                'resources' should be a dictionary like {'cpu': '500m', 'memory': '1Gi'}.

    Returns:
        Tuple (success: bool, message: str).
    """
    apps_v1_api = client.AppsV1Api(api_client)
    name = params.get("name")
    namespace = params.get("namespace", "default")
    resources = params.get("resources")

    if not name:
        return False, "Missing 'name' parameter for vertical_scale_deployment."

    # If 'resources' is not a dict, try to construct it from 'resource' and 'value'
    if not isinstance(resources, dict):
        resource_type = params.get("resource")
        resource_value = params.get("value")
        if resource_type and resource_value:
            resources = {str(resource_type): str(resource_value)}
        else:
            # 'resources' is not a dict and cannot be constructed
            return False, "Invalid 'resources' parameter for vertical_scale_deployment. 'resources' must be a dictionary, or 'resource' and 'value' must be provided."

    if not resources: # Check if resources dict is empty after potential construction
        return False, "Missing 'resources' parameter or 'resource'/'value' for vertical_scale_deployment. 'resources' dictionary cannot be empty."
    
    # Validate that all keys and values in resources are strings, as expected by K8s API
    # This also implicitly checks that resources is a dict after the above logic.
    if not isinstance(resources, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in resources.items()
    ):
        return False, "Invalid 'resources' structure. Must be a dictionary with string keys and string values (e.g., {'cpu': '500m'})."

    try:
        # Get the current deployment
        deployment = await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)

        # Prepare the patch body to update container resources
        # This assumes all containers in the deployment should have the same resources applied.
        # A more sophisticated action might allow specifying resources per container.
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": []
                    }
                }
            }
        }

        for container in deployment.spec.template.spec.containers:
            updated_container = {
                "name": container.name,
                "resources": resources # Apply the specified resources to all containers
            }
            patch_body["spec"]["template"]["spec"]["containers"].append(updated_container)


        logger.info(f"Vertically scaling deployment '{namespace}/{name}' with resources: {resources}...")
        await apps_v1_api.patch_namespaced_deployment(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully initiated vertical scaling for deployment '{namespace}/{name}' with resources {resources}."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = (
            f"Failed to vertically scale deployment '{namespace}/{name}': "
            f"{e.status} - {e.reason} - {e.body}"
        )
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error vertically scaling deployment '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("vertical_scale_statefulset")
async def vertical_scale_statefulset_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Vertically scales a Kubernetes StatefulSet by updating resource requests/limits.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name', optionally 'namespace', and 'resources'.
                'resources' should be a dictionary like {'cpu': '500m', 'memory': '1Gi'}.

    Returns:
        Tuple (success: bool, message: str).
    """
    apps_v1_api = client.AppsV1Api(api_client)
    name = params.get("name")
    namespace = params.get("namespace", "default")
    resources = params.get("resources") # Expected format: {'cpu': '...', 'memory': '...'}

    if not name or not resources or not isinstance(resources, dict):
        return False, "Missing 'name' or invalid 'resources' parameter for vertical_scale_statefulset. 'resources' must be a dictionary."

    try:
        # Get the current statefulset
        statefulset = await apps_v1_api.read_namespaced_stateful_set(name=name, namespace=namespace)

        # Prepare the patch body to update container resources
        # This assumes all containers in the statefulset should have the same resources applied.
        # A more sophisticated action might allow specifying resources per container.
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": []
                    }
                }
            }
        }

        for container in statefulset.spec.template.spec.containers:
            updated_container = {
                "name": container.name,
                "resources": resources # Apply the specified resources to all containers
            }
            patch_body["spec"]["template"]["spec"]["containers"].append(updated_container)


        logger.info(f"Vertically scaling statefulset '{namespace}/{name}' with resources: {resources}...")
        await apps_v1_api.patch_namespaced_stateful_set(
            name=name, namespace=namespace, body=patch_body
        )
        msg = f"Successfully initiated vertical scaling for statefulset '{namespace}/{name}' with resources {resources}."
        logger.info(msg)
        return True, msg
    except client.ApiException as e:
        msg = (
            f"Failed to vertically scale statefulset '{namespace}/{name}': "
            f"{e.status} - {e.reason} - {e.body}"
        )
        logger.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error vertically scaling statefulset '{namespace}/{name}': {e}"
        logger.exception(msg)
        return False, msg


@register_action("cordon_node")
async def cordon_node_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Cordon a Kubernetes node, marking it as unschedulable.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name'.

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
            msg = f"Node '{name}' is already unschedulable"
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
    Uncordon a Kubernetes node, marking it as schedulable.

    Args:
        api_client: An initialized Kubernetes ApiClient.
        params: Dictionary containing 'name'.

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


@register_action("manual_intervention")
async def manual_intervention_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Handles manual intervention actions by sending an email with instructions.

    Args:
        api_client: An initialized Kubernetes ApiClient (not used for this action).
        params: Dictionary containing 'reason' and 'instructions'.

    Returns:
        Tuple (success: bool, message: str).
    """
    reason = params.get("reason", "No reason provided.")
    instructions = params.get("instructions", "No instructions provided.")

    # TODO: Determine the actual recipient email address.
    # For now, using a placeholder. This should ideally come from settings or anomaly context.
    recipient_email = "lk7565@srmist.edu.in" # Placeholder

    subject = f"KubeWise Manual Intervention Required: {reason}"
    body = f"Manual intervention is required for a detected anomaly.\n\nReason: {reason}\n\nInstructions:\n{instructions}"

    logger.info(f"Sending manual intervention email to {recipient_email}...")
    send_email(recipient_email, subject, body)

    msg = f"Manual intervention instructions sent via email to {recipient_email}."
    logger.info(msg)
    return True, msg


@register_action("update_resource_limits")
async def update_resource_limits_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Update resource limits and requests for a container in a Kubernetes resource.

    Args:
        api_client: Kubernetes API client
        params: Dictionary with the following keys:
            - name: Name of the resource
            - namespace: Namespace of the resource
            - kind: Kind of resource (Pod, Deployment, StatefulSet, DaemonSet, ReplicaSet)
            - container: Name of the container to update
            - limits: Dictionary with resource limits (cpu, memory)
            - requests: Dictionary with resource requests (cpu, memory)
            - update_parent: Optional boolean, if True and resource is a ReplicaSet, 
                           will update parent Deployment if it exists (default: False)

    Returns:
        Tuple of (success, message)
    """
    name = params.get("name")
    namespace = params.get("namespace", "default")
    resource_kind = params.get("kind", "Pod").lower()
    container_name = params.get("container")
    limits = params.get("limits", {})
    requests = params.get("requests", {})
    update_parent = params.get("update_parent", False)

    if not name:
        return False, "Resource name is required"

    if not container_name:
        return False, "Container name is required"

    if not limits and not requests:
        return False, "At least one of limits or requests must be specified"

    try:
        # Handle different resource kinds
        if resource_kind == "pod":
            # For Pods, we need to check owner references first
            core_v1_api = client.CoreV1Api(api_client)
            apps_v1_api = client.AppsV1Api(api_client)
            
            # Get the current Pod
            try:
                pod = await core_v1_api.read_namespaced_pod(name=name, namespace=namespace)
            except client.ApiException as e:
                return False, f"Failed to get Pod {namespace}/{name}: {e.reason}"
            
            # Log available containers for debugging
            available_containers = []
            for container in pod.spec.containers:
                available_containers.append(container.name)
            
            if not available_containers:
                return False, f"No containers found in Pod {namespace}/{name}"
            
            logger.info(f"Available containers in Pod {namespace}/{name}: {', '.join(available_containers)}")
            
            # Auto-select container for Azure Monitor pods if needed
            selected_container = container_name
            if container_name not in available_containers:
                if name and ("ama-metrics" in name.lower() or "prometheus" in name.lower()):
                    prometheus_collector = next((c for c in available_containers if "prometheus-collector" in c.lower()), None)
                    selected_container = prometheus_collector if prometheus_collector else available_containers[0]
                    logger.warning(f"Container {container_name} not found in Pod {namespace}/{name}. Automatically using '{selected_container}' instead.")
                else:
                    closest_match = min(available_containers, key=lambda x: abs(len(x) - len(container_name)))
                    return False, f"Container {container_name} not found in Pod {namespace}/{name}. Available containers: {', '.join(available_containers)}. Did you mean '{closest_match}'?"

            # Check for owner references and follow the chain to top-level controller
            current_resource = pod
            top_level_controller = None
            owner_chain = []

            while current_resource.metadata.owner_references and len(current_resource.metadata.owner_references) > 0:
                owner = current_resource.metadata.owner_references[0]
                owner_chain.append(f"{owner.kind}/{owner.name}")
                
                try:
                    if owner.kind.lower() == "replicaset":
                        current_resource = await apps_v1_api.read_namespaced_replica_set(name=owner.name, namespace=namespace)
                    elif owner.kind.lower() == "deployment":
                        top_level_controller = await apps_v1_api.read_namespaced_deployment(name=owner.name, namespace=namespace)
                        break
                    elif owner.kind.lower() == "daemonset":
                        top_level_controller = await apps_v1_api.read_namespaced_daemon_set(name=owner.name, namespace=namespace)
                        break
                    elif owner.kind.lower() == "statefulset":
                        top_level_controller = await apps_v1_api.read_namespaced_stateful_set(name=owner.name, namespace=namespace)
                        break
                    else:
                        # Unknown controller type
                        return False, f"Pod {namespace}/{name} is managed by unsupported controller type {owner.kind}"
                except client.ApiException as e:
                    return False, f"Failed to get owner {owner.kind}/{owner.name}: {e.reason}"

            if top_level_controller:
                logger.info(f"Pod {namespace}/{name} is managed by {' -> '.join(owner_chain)}. Updating top-level controller.")
                
                # Update the container in the controller template
                container_updated = False
                for container in top_level_controller.spec.template.spec.containers:
                    if container.name == selected_container:
                        if not container.resources:
                            container.resources = client.V1ResourceRequirements()
                        if limits:
                            container.resources.limits = limits
                        if requests:
                            container.resources.requests = requests
                        container_updated = True
                        break

                if not container_updated:
                    return False, f"Container {selected_container} not found in controller template"

                # Update the controller
                try:
                    if isinstance(top_level_controller, client.V1Deployment):
                        await apps_v1_api.patch_namespaced_deployment(
                            name=top_level_controller.metadata.name,
                            namespace=namespace,
                            body=top_level_controller
                        )
                    elif isinstance(top_level_controller, client.V1DaemonSet):
                        await apps_v1_api.patch_namespaced_daemon_set(
                            name=top_level_controller.metadata.name,
                            namespace=namespace,
                            body=top_level_controller
                        )
                    elif isinstance(top_level_controller, client.V1StatefulSet):
                        await apps_v1_api.patch_namespaced_stateful_set(
                            name=top_level_controller.metadata.name,
                            namespace=namespace,
                            body=top_level_controller
                        )
                    
                    return True, f"Updated resource limits for container {selected_container} in {top_level_controller.kind}/{top_level_controller.metadata.name}"
                except client.ApiException as e:
                    return False, f"Failed to update {top_level_controller.kind}/{top_level_controller.metadata.name}: {e.reason}"
            
            # For standalone pods, proceed with delete and recreate
            logger.info(f"Pod {namespace}/{name} is a standalone pod. Proceeding with delete and recreate.")
            
            # Update the container resources
            container_updated = False
            for container in pod.spec.containers:
                if container.name == selected_container:
                    if not container.resources:
                        container.resources = client.V1ResourceRequirements()
                    if limits:
                        container.resources.limits = limits
                    if requests:
                        container.resources.requests = requests
                    container_updated = True
                    break

            if not container_updated:
                return False, f"Container {selected_container} not found in pod spec"

            # Delete and recreate the pod
            try:
                await core_v1_api.delete_namespaced_pod(name=name, namespace=namespace)
            except client.ApiException as e:
                return False, f"Failed to delete Pod {namespace}/{name}: {e.reason}"

            try:
                # Remove resourceVersion to avoid conflicts
                pod.metadata.resource_version = None
                await core_v1_api.create_namespaced_pod(namespace=namespace, body=pod)
                return True, f"Updated resource limits for container {selected_container} in standalone Pod {namespace}/{name}"
            except client.ApiException as e:
                return False, f"Failed to recreate Pod {namespace}/{name}: {e.reason}"

        elif resource_kind in ["deployment", "daemonset", "statefulset", "replicaset"]:
            apps_v1_api = client.AppsV1Api(api_client)
            
            try:
                # Get the current resource
                if resource_kind == "deployment":
                    resource = await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
                elif resource_kind == "daemonset":
                    resource = await apps_v1_api.read_namespaced_daemon_set(name=name, namespace=namespace)
                elif resource_kind == "replicaset":
                    resource = await apps_v1_api.read_namespaced_replica_set(name=name, namespace=namespace)
                    
                    # Check if we should update parent Deployment instead
                    if update_parent and resource.metadata.owner_references:
                        for owner_ref in resource.metadata.owner_references:
                            if owner_ref.kind.lower() == "deployment":
                                try:
                                    deployment = await apps_v1_api.read_namespaced_deployment(
                                        name=owner_ref.name, 
                                        namespace=namespace
                                    )
                                    logger.info(f"Found parent Deployment {namespace}/{owner_ref.name} for ReplicaSet {namespace}/{name}")
                                    resource = deployment
                                    resource_kind = "deployment"
                                    break
                                except client.ApiException as e:
                                    logger.warning(f"Failed to get parent Deployment {namespace}/{owner_ref.name}: {e.reason}")
                                    # Continue with ReplicaSet update if can't get Deployment
                else:  # statefulset
                    resource = await apps_v1_api.read_namespaced_stateful_set(name=name, namespace=namespace)
            except client.ApiException as e:
                return False, f"Failed to get {resource_kind} {namespace}/{name}: {e.reason}"

            # Log available containers
            available_containers = [c.name for c in resource.spec.template.spec.containers]
            if not available_containers:
                return False, f"No containers found in {resource_kind} {namespace}/{name}"
            
            logger.info(f"Available containers in {resource_kind} {namespace}/{name}: {', '.join(available_containers)}")

            # Auto-select container for Azure Monitor resources if needed
            selected_container = container_name
            if container_name not in available_containers:
                if name and ("ama-metrics" in name.lower() or "prometheus" in name.lower()):
                    prometheus_collector = next((c for c in available_containers if "prometheus-collector" in c.lower()), None)
                    selected_container = prometheus_collector if prometheus_collector else available_containers[0]
                    logger.warning(f"Container {container_name} not found in {resource_kind} {namespace}/{name}. Automatically using '{selected_container}' instead.")
                else:
                    closest_match = min(available_containers, key=lambda x: abs(len(x) - len(container_name)))
                    return False, f"Container {container_name} not found in {resource_kind} {namespace}/{name}. Available containers: {', '.join(available_containers)}. Did you mean '{closest_match}'?"

            # Update the container resources
            container_updated = False
            for container in resource.spec.template.spec.containers:
                if container.name == selected_container:
                    if not container.resources:
                        container.resources = client.V1ResourceRequirements()
                    if limits:
                        container.resources.limits = limits
                    if requests:
                        container.resources.requests = requests
                    container_updated = True
                    break

            if not container_updated:
                return False, f"Container {selected_container} not found in {resource_kind} template"

            # Update the resource
            try:
                if resource_kind == "deployment":
                    await apps_v1_api.patch_namespaced_deployment(name=name, namespace=namespace, body=resource)
                elif resource_kind == "daemonset":
                    await apps_v1_api.patch_namespaced_daemon_set(name=name, namespace=namespace, body=resource)
                elif resource_kind == "replicaset":
                    await apps_v1_api.patch_namespaced_replica_set(name=name, namespace=namespace, body=resource)
                else:  # statefulset
                    await apps_v1_api.patch_namespaced_stateful_set(name=name, namespace=namespace, body=resource)
                
                return True, f"Updated resource limits for container {selected_container} in {resource_kind} {namespace}/{name}"
            except client.ApiException as e:
                return False, f"Failed to update {resource_kind} {namespace}/{name}: {e.reason}"

        else:
            return False, f"Unsupported resource kind: {resource_kind}"

    except Exception as e:
        return False, f"Unexpected error updating resource limits: {str(e)}"


@register_action("restart_cronjob")
async def restart_cronjob_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Restart a CronJob by suspending and then resuming it.

    Args:
        api_client: Kubernetes API client
        params: Dictionary with the following keys:
            - name: Name of the CronJob
            - namespace: Namespace of the CronJob

    Returns:
        Tuple of (success, message)
    """
    name = params.get("name")
    namespace = params.get("namespace", "default")

    if not name:
        return False, "CronJob name is required"

    try:
        batch_v1_api = client.BatchV1Api(api_client)
        
        # Get the current CronJob
        try:
            cronjob = await batch_v1_api.read_namespaced_cron_job(name=name, namespace=namespace)
        except client.ApiException as e:
            return False, f"Failed to get CronJob {namespace}/{name}: {e.reason}"
        
        # First, suspend the CronJob
        cronjob.spec.suspend = True
        try:
            await batch_v1_api.patch_namespaced_cron_job(
                name=name, 
                namespace=namespace, 
                body=cronjob
            )
        except client.ApiException as e:
            return False, f"Failed to suspend CronJob {namespace}/{name}: {e.reason}"
        
        # Wait briefly to ensure the suspension takes effect
        await asyncio.sleep(2)
        
        # Then, resume the CronJob
        cronjob.spec.suspend = False
        try:
            await batch_v1_api.patch_namespaced_cron_job(
                name=name, 
                namespace=namespace, 
                body=cronjob
            )
            return True, f"Successfully restarted CronJob {namespace}/{name}"
        except client.ApiException as e:
            return False, f"Failed to resume CronJob {namespace}/{name}: {e.reason}"
        
    except Exception as e:
        return False, f"Error restarting CronJob: {str(e)}"


@register_action("restart_job")
async def restart_job_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Restart a Job by deleting it and recreating it.

    Args:
        api_client: Kubernetes API client
        params: Dictionary with the following keys:
            - name: Name of the Job
            - namespace: Namespace of the Job

    Returns:
        Tuple of (success, message)
    """
    name = params.get("name")
    namespace = params.get("namespace", "default")

    if not name:
        return False, "Job name is required"

    try:
        batch_v1_api = client.BatchV1Api(api_client)
        
        # Get the current Job
        try:
            job = await batch_v1_api.read_namespaced_job(name=name, namespace=namespace)
        except client.ApiException as e:
            return False, f"Failed to get Job {namespace}/{name}: {e.reason}"
        
        # Save the Job spec for recreation
        job_spec = job.spec
        
        # Delete the existing Job
        try:
            await batch_v1_api.delete_namespaced_job(
                name=name, 
                namespace=namespace,
                body=client.V1DeleteOptions(
                    propagation_policy="Background"
                )
            )
        except client.ApiException as e:
            return False, f"Failed to delete Job {namespace}/{name}: {e.reason}"
        
        # Wait for the Job to be deleted
        max_wait_time = 30  # Maximum wait time in seconds
        wait_interval = 2   # Check interval in seconds
        
        for _ in range(max_wait_time // wait_interval):
            try:
                await batch_v1_api.read_namespaced_job(name=name, namespace=namespace)
                # If we get here, the Job still exists, wait and try again
                await asyncio.sleep(wait_interval)
            except client.ApiException as e:
                if e.status == 404:
                    # Job is deleted, proceed to recreation
                    break
                else:
                    return False, f"Error checking Job deletion status: {e.reason}"
        else:
            # If we exited the loop normally, the Job wasn't deleted in time
            return False, f"Timed out waiting for Job {namespace}/{name} to be deleted"
        
        # Create a new Job with the same spec
        new_job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace
            ),
            spec=job_spec
        )
        
        try:
            await batch_v1_api.create_namespaced_job(namespace=namespace, body=new_job)
            return True, f"Successfully restarted Job {namespace}/{name}"
        except client.ApiException as e:
            return False, f"Failed to recreate Job {namespace}/{name}: {e.reason}"
        
    except Exception as e:
        return False, f"Error restarting Job: {str(e)}"


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
    app_context=None,
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
        if hasattr(app_context, "record_remediation"):
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
    if (
        "name" not in params
        and name
        and action_type not in ["cordon_node", "uncordon_node", "drain_node"]
    ):
        params["name"] = name
        
        # Special handling for DaemonSet pods with node suffixes
        if action_type == "restart_daemonset":
            # First check if there's controller info already in the context
            controller_kind = None
            controller_name = None
            if hasattr(action, 'context') and action.context:
                discovered_controller = action.context.get('discovered_controller', {})
                if discovered_controller.get('kind', '').lower() == 'daemonset':
                    controller_kind = 'DaemonSet'
                    controller_name = discovered_controller.get('name')
                    if controller_name:
                        logger.info(f"Using DaemonSet controller '{controller_name}' from action context")
                        params["name"] = controller_name
                        # Also preserve the original pod name for better discovery
                        params["pod_name"] = name
            
            # If no controller info in context, store the original name as pod_name
            # and let restart_daemonset_action handle the dynamic controller discovery
            if not controller_name:
                logger.info(f"No controller found in context for '{name}', will dynamically discover at runtime")
                # Store the original entity name as pod_name for discovery
                params["pod_name"] = name
                
                # We'll still keep the original name as a fallback
                # The actual controller discovery will happen in restart_daemonset_action using get_pod_controller

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
            logger.info(
                f"Retry {retry_count-1}/{max_retries} for action {action_type} on {entity_id}"
            )

        try:
            # Use the registry to call the appropriate action function
            return await action_func(api_client, params)
        except Exception as e:
            logger.exception(f"Error executing action {action_type}: {str(e)}")
            return False, f"Exception during execution: {str(e)}"

    # Validate action and entity type compatibility
    invalid_action = False
    error_msg = ""

    # Prevent deleting nodes as pods
    if action_type == "delete_pod" and entity_type == "Node":
        invalid_action = True
        error_msg = f"Invalid action: Cannot delete Node {params.get('name', entity_id)} using delete_pod action"
    # Prevent operating on pods with node actions
    if (
        action_type in ["cordon_node", "uncordon_node", "drain_node", "taint_node"]
        and entity_type != "Node"
    ):
        invalid_action = True
        error_msg = f"Invalid action: Cannot perform {action_type} on non-Node entity {entity_id}"

    # Early return if invalid action is detected
    if invalid_action:
        logger.error(error_msg)

        # Update action with execution results
        action.executed = True
        action.execution_timestamp = datetime.datetime.now(datetime.timezone.utc)
        action.execution_status = "failed"
        action.execution_message = error_msg
        action.execution_duration = 0.0
        action.retry_count = 0

        return False, error_msg

    # Execute with timeout
    try:
        logger.info(
            f"Executing action {action_idx}/{total_actions}: {action_type} with parameters {params}"
        )

        # Use exponential backoff for retries
        success = False
        message = ""
        last_error = None

        for attempt in range(
            1, max_retries + 2
        ):  # +2 because range starts at 1 and we need to include max_retries
            try:
                # Set timeout based on action type (node operations need more time)
                timeout_secs = 60 if "node" in action_type else 30

                # Execute with timeout
                success, message = await asyncio.wait_for(
                    retry_action(), timeout=timeout_secs
                )

                # Break if successful
                if success:
                    break

                # Only retry on specific failures
                if "not found" in message.lower() or "connection" in message.lower():
                    # These are failures worth retrying (transient issues)
                    if attempt <= max_retries:
                        retry_delay = 2**attempt  # Exponential backoff
                        logger.warning(
                            f"Action failed, will retry in {retry_delay}s: {message}"
                        )
                        await asyncio.sleep(retry_delay)
                        continue

                # Non-retryable failure or max retries reached
                break

            except asyncio.TimeoutError:
                last_error = "Operation timed out"
                if attempt <= max_retries:
                    retry_delay = 2**attempt
                    logger.warning(f"Action timed out, will retry in {retry_delay}s")
                    await asyncio.sleep(retry_delay)
                else:
                    message = f"Action timed out after {timeout_secs}s and {max_retries} retries"
                    success = False
                    break

            except Exception as e:
                last_error = str(e)
                if attempt <= max_retries:
                    retry_delay = 2**attempt
                    logger.warning(
                        f"Action failed with exception, will retry in {retry_delay}s: {str(e)}"
                    )
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
            logger.info(
                f"Action {action_type} executed successfully in {duration:.2f}s: {message}"
            )
        else:
            logger.error(
                f"Action {action_type} failed after {duration:.2f}s: {message}"
            )

        # Record execution in database
        try:
            plan_id = None
            try:
                # Try to get the plan ID from the database
                result = await action_log_collection.database[
                    "remediation_plans"
                ].find_one({"anomaly_id": anomaly_id, "completed": False}, {"_id": 1})
                if result:
                    plan_id = result["_id"]
            except Exception as e:
                logger.warning(f"Failed to retrieve plan ID: {e}")

            # Create execution record
            execution_record = ExecutedActionRecord(
                anomaly_id=str(anomaly_id),
                plan_id=str(plan_id)
                if plan_id
                else str(ObjectId()),  # Use a generic ID if no plan found
                action_id=str(action.id)
                if action.id
                else str(ObjectId()),  # Use action ID if available
                action_type=action_type,
                parameters=params,
                entity_type=entity_type,
                entity_id=entity_id,
                namespace=namespace,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
                success=success,
                message=message[
                    :500
                ],  # Limit message length to avoid oversized documents
                duration_seconds=duration,
                score=anomaly_score,
                source_type=source_type,
                retry_attempt=retry_count - 1,
                error_details={
                    "retry_count": retry_count - 1,
                    "last_error": last_error,
                    "timeout_seconds": 60 if "node" in action_type else 30,
                }
                if not success
                else None,
            )

            # Store in database
            await action_log_collection.insert_one(execution_record.model_dump())

        except Exception as db_err:
            logger.error(f"Failed to record action execution failure: {db_err}")

        # After execution is complete, record the metrics
        try:
            if app_context and hasattr(app_context, "record_remediation"):
                app_context.record_remediation(
                    action_type=action_type,
                    entity_type=entity_type,
                    namespace=namespace,
                    success=success,
                    duration=duration,
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
                error_details={
                    "exception": str(e),
                    "traceback": traceback.format_exc(),
                },
            )

            await action_log_collection.insert_one(execution_record.model_dump())

            # Record metrics for the failed remediation action
            if app_context and hasattr(app_context, "record_remediation"):
                try:
                    app_context.record_remediation(
                        action_type=action_type,
                        entity_type=entity_type,
                        namespace=namespace,
                        success=False,
                        duration=time.monotonic() - start_time,
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

    # Get entity ID for structured logging
    entity_id = "unknown"
    namespace = anomaly_record.namespace or "default"
    if anomaly_record.entity_id:
        entity_id = anomaly_record.entity_id
        # Extract namespace from entity_id if in format namespace/name
        if "/" in entity_id and not namespace:
            namespace = entity_id.split("/")[0]

    # Check if namespace is in blacklist
    if namespace in settings.blacklisted_namespaces:
        error_msg = f"Cannot execute remediation in blacklisted namespace: {namespace}"
        logger.warning(error_msg)

        # Update anomaly status to indicate blacklist prevention
        if not dry_run:
            await anomaly_collection.update_one(
                {"_id": anomaly_record.id},
                {
                    "$set": {
                        "remediation_status": "blocked",
                        "remediation_error": error_msg,
                        "remediation_message": f"Remediation blocked: namespace {namespace} is blacklisted for safety",
                    }
                },
            )
        return False

    # --- Plan Validation ---
    try:
        validated_plan = RemediationPlan.model_validate(plan.model_dump())
        logger.debug(f"Remediation plan for anomaly {anomaly_id_str} passed validation")
    except ValidationError as e:
        error_msg = f"Generated remediation plan failed validation: {e}"
        logger.error(error_msg)
        await anomaly_collection.update_one(
            {"_id": anomaly_record.id},
            {"$set": {"remediation_status": "failed", "remediation_error": error_msg}},
        )
        return False

    # If no actions in plan, mark as completed
    if not validated_plan.actions:
        logger.info(
            f"Remediation plan has no actions (reasoning: {validated_plan.reasoning})"
        )
        if not dry_run:
            await anomaly_collection.update_one(
                {"_id": anomaly_record.id},
                {
                    "$set": {
                        "remediation_status": "completed",
                        "remediation_message": validated_plan.reasoning,
                    }
                },
            )
        return True

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

    can_parallelize = (
        len(validated_plan.actions) > 1
        and len(validated_plan.actions) <= settings.max_parallel_remediation_actions
    )

    overall_success = True
    final_message = ""

    # Create a custom Kubernetes API client for dry runs if needed
    if dry_run:
        # For dry run, we'll need to create mock clients and simulate actions
        logger.info(
            f"Performing DRY RUN for remediation plan on anomaly {anomaly_id_str}"
        )

        # Execute actions sequentially in dry run mode (parallelization doesn't make sense for simulation)
        for i, action in enumerate(validated_plan.actions):
            action_type = action.action_type
            params = action.parameters

            # Check if action exists in registry
            if action_type not in ACTION_REGISTRY:
                msg = f"Unknown action type '{action_type}' (Action {i+1}/{len(validated_plan.actions)})"
                logger.error(msg)
                dry_run_results.append(
                    {
                        "action_index": i,
                        "action_type": action_type,
                        "parameters": params,
                        "success": False,
                        "message": msg,
                        "simulated_impact": "Unknown impact - action type not registered",
                    }
                )
                overall_success = False
                continue

            # Simulate the action's impact
            try:
                # Prepare impact assessment based on action type
                impact_assessment = await simulate_action_impact(k8s_client, action)

                logger.info(
                    f"DRY RUN - Simulated action {i+1}/{len(validated_plan.actions)}: {action_type} with parameters {params}"
                )
                logger.info(f"DRY RUN - Impact assessment: {impact_assessment}")

                # Record dry run result
                dry_run_results.append(
                    {
                        "action_index": i,
                        "action_type": action_type,
                        "parameters": params,
                        "success": True,
                        "message": f"Simulated execution of {action_type}",
                        "simulated_impact": impact_assessment,
                    }
                )

            except Exception as e:
                msg = f"DRY RUN - Failed to simulate action {action_type}: {str(e)}"
                logger.error(msg)
                dry_run_results.append(
                    {
                        "action_index": i,
                        "action_type": action_type,
                        "parameters": params,
                        "success": False,
                        "message": msg,
                        "simulated_impact": "Failed to simulate - potential error during execution",
                    }
                )
                overall_success = False

        # Update plan with dry run results if executing as part of a validation sequence
        if plan.dry_run_results is None:
            plan.dry_run_results = []
        plan.dry_run_results.extend(dry_run_results)

        # Calculate safety score based on simulation results
        successful_simulations = sum(
            1 for result in dry_run_results if result["success"]
        )
        total_simulations = len(dry_run_results)
        safety_score = (
            successful_simulations / total_simulations if total_simulations > 0 else 0.0
        )
        plan.safety_score = safety_score

        # Generate risk assessment
        risk_level = (
            "Low" if safety_score > 0.8 else "Medium" if safety_score > 0.5 else "High"
        )
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
        logger.info(
            f"Executing {len(validated_plan.actions)} actions in parallel for anomaly {anomaly_id_str}"
        )

        # Prepare coroutines for each action
        action_coroutines = []
        for i, action in enumerate(validated_plan.actions):
            action_coro = execute_action_with_timeout(
                action=action,
                api_client=k8s_client,
                action_idx=i + 1,
                total_actions=len(validated_plan.actions),
                anomaly_id=anomaly_id_obj,
                action_log_collection=action_log_collection,
                entity_id=entity_id,
                anomaly_score=anomaly_record.anomaly_score,
                source_type=anomaly_record.data_source or "unknown",
                app_context=app_context,
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
                logger.info(
                    f"All parallel actions completed successfully for anomaly {anomaly_id_str}"
                )
                final_message = "All actions executed successfully"

        except Exception as e:
            logger.exception(
                f"Parallel action execution failed for anomaly {anomaly_id_str}: {e}"
            )
            overall_success = False
            final_message = f"Parallel execution failed: {str(e)}"
    else:
        # Execute actions sequentially
        for i, action in enumerate(validated_plan.actions):
            success, msg = await execute_action_with_timeout(
                action=action,
                api_client=k8s_client,
                action_idx=i + 1,
                total_actions=len(validated_plan.actions),
                anomaly_id=anomaly_id_obj,
                action_log_collection=action_log_collection,
                entity_id=entity_id,
                anomaly_score=anomaly_record.anomaly_score,
                source_type=anomaly_record.data_source or "unknown",
                app_context=app_context,
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
        update_doc["$set"]["remediation_message"] = (
            "Successfully executed all remediation actions"
        )

    try:
        await anomaly_collection.update_one({"_id": anomaly_record.id}, update_doc)
        logger.info(
            f"Remediation plan execution for anomaly {anomaly_id_str} finished with status: {final_status}"
        )
    except Exception as e:
        logger.exception(
            f"Failed to update final anomaly status for {anomaly_id_str}: {e}"
        )

    return overall_success


async def simulate_action_impact(
    api_client: client.ApiClient, action: RemediationAction
) -> str:
    """
    Simulates the impact of executing an action without making actual changes.

    Args:
        api_client: An initialized Kubernetes API client
        action: The RemediationAction to simulate

    Returns:
        A string describing the predicted impact
    """
    action_type = action.action_type
    params = action.parameters

    logger.info(f"Simulating action: {action_type} with params {params}")

    # This is a basic placeholder. Real simulation would involve:
    # - Checking resource existence and state
    # - Predicting outcomes based on action type and current state
    # - Assessing potential risks

    try:
        # Example simulation logic (replace with actual implementation)
        if action_type == "scale_deployment":
            name = params.get("name")
            namespace = params.get("namespace", "default")
            replicas = params.get("replicas")
            if not name or replicas is None:
                return "Missing 'name' or 'replicas' for simulation", False
            # Simulate checking if deployment exists
            try:
                apps_v1_api = client.AppsV1Api(api_client)
                await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
                return f"Simulation: Deployment '{namespace}/{name}' found. Scaling to {replicas} replicas would be attempted.", True
            except client.ApiException as e:
                if e.status == 404:
                    return f"Simulation: Deployment '{namespace}/{name}' not found.", False
                else:
                    raise # Re-raise other API errors
        elif action_type == "delete_pod":
            name = params.get("name")
            namespace = params.get("namespace")
            if not name or not namespace:
                return "Missing 'name' or 'namespace' for simulation", False
            # Simulate checking if pod exists
            try:
                core_v1_api = client.CoreV1Api(api_client)
                await core_v1_api.read_namespaced_pod(name=name, namespace=namespace)
                return f"Simulation: Pod '{namespace}/{name}' found. Deletion would be attempted.", True
            except client.ApiException as e:
                if e.status == 404:
                    return f"Simulation: Pod '{namespace}/{name}' not found.", False
                else:
                    raise # Re-raise other API errors
        elif action_type == "restart_deployment":
            name = params.get("name")
            namespace = params.get("namespace", "default")
            if not name:
                return "Missing 'name' for simulation", False
            # Simulate checking if deployment exists
            try:
                apps_v1_api = client.AppsV1Api(api_client)
                await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
                return f"Simulation: Deployment '{namespace}/{name}' found. Restart would be attempted.", True
            except client.ApiException as e:
                if e.status == 404:
                    return f"Simulation: Deployment '{namespace}/{name}' not found.", False
                else:
                    raise # Re-raise other API errors
        elif action_type == "drain_node":
            name = params.get("name")
            if not name:
                return "Missing 'name' for simulation", False
            # Simulate checking if node exists
            try:
                core_v1_api = client.CoreV1Api(api_client)
                await core_v1_api.read_node(name=name)
                # Simulate checking for pods on the node (simplified)
                # In a real simulation, you'd list pods and check if they are drainable
                field_selector = (
                    f"spec.nodeName={name},status.phase!=Failed,status.phase!=Succeeded"
                )
                pods = await core_v1_api.list_pod_for_all_namespaces(
                    field_selector=field_selector
                )
                pods_count = len(pods.items)
                return f"Simulation: Node '{name}' found with {pods_count} pods. Cordon and drain would be attempted.", True
            except client.ApiException as e:
                if e.status == 404:
                    return f"Simulation: Node '{name}' not found.", False
                else:
                    raise # Re-raise other API errors
        elif action_type == "scale_statefulset":
            name = params.get("name")
            namespace = params.get("namespace", "default")
            replicas = params.get("replicas")
            if not name or replicas is None:
                return "Missing 'name' or 'replicas' for simulation", False
            # Simulate checking if statefulset exists
            try:
                apps_v1_api = client.AppsV1Api(api_client)
                await apps_v1_api.read_namespaced_stateful_set(name=name, namespace=namespace)
                return f"Simulation: StatefulSet '{namespace}/{name}' found. Scaling to {replicas} replicas would be attempted.", True
            except client.ApiException as e:
                if e.status == 404:
                    return f"Simulation: StatefulSet '{namespace}/{name}' not found.", False
                else:
                    raise # Re-raise other API errors
        elif action_type == "evict_pod":
            name = params.get("name")
            namespace = params.get("namespace")
            if not name or not namespace:
                return "Missing 'name' or 'namespace' for simulation", False
            # Simulate checking if pod exists
            try:
                core_v1_api = client.CoreV1Api(api_client)
                await core_v1_api.read_namespaced_pod(name=name, namespace=namespace)
                return f"Simulation: Pod '{namespace}/{name}' found. Eviction would be attempted.", True
            except client.ApiException as e:
                if e.status == 404:
                    return f"Simulation: Pod '{namespace}/{name}' not found.", False
                else:
                    raise # Re-raise other API errors
        elif action_type == "vertical_scale_deployment":
             name = params.get("name")
             namespace = params.get("namespace", "default")
             if not name:
                 return "Missing 'name' for simulation", False
             # Simulate checking if deployment exists
             try:
                 apps_v1_api = client.AppsV1Api(api_client)
                 await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
                 return f"Simulation: Deployment '{namespace}/{name}' found. Vertical scaling would be attempted.", True
             except client.ApiException as e:
                 if e.status == 404:
                     return f"Simulation: Deployment '{namespace}/{name}' not found.", False
                 else:
                     raise # Re-raise other API errors
        elif action_type == "vertical_scale_statefulset":
             name = params.get("name")
             namespace = params.get("namespace", "default")
             if not name:
                 return "Missing 'name' for simulation", False
             # Simulate checking if statefulset exists
             try:
                 apps_v1_api = client.AppsV1Api(api_client)
                 await apps_v1_api.read_namespaced_stateful_set(name=name, namespace=namespace)
                 return f"Simulation: StatefulSet '{namespace}/{name}' found. Vertical scaling would be attempted.", True
             except client.ApiException as e:
                 if e.status == 404:
                     return f"Simulation: StatefulSet '{namespace}/{name}' not found.", False
                 else:
                     raise # Re-raise other API errors
        elif action_type == "cordon_node":
            name = params.get("name")
            if not name:
                return "Missing 'name' for simulation", False
            # Simulate checking if node exists
            try:
                core_v1_api = client.CoreV1Api(api_client)
                await core_v1_api.read_node(name=name)
                return f"Simulation: Node '{name}' found. Cordoning would be attempted.", True
            except client.ApiException as e:
                if e.status == 404:
                    return f"Simulation: Node '{name}' not found.", False
                else:
                    raise # Re-raise other API errors
        elif action_type == "uncordon_node":
            name = params.get("name")
            if not name:
                return "Missing 'name' for simulation", False
            # Simulate checking if node exists
            try:
                core_v1_api = client.CoreV1Api(api_client)
                await core_v1_api.read_node(name=name)
                return f"Simulation: Node '{name}' found. Uncordoning would be attempted.", True
            except client.ApiException as e:
                if e.status == 404:
                    return f"Simulation: Node '{name}' not found.", False
                else:
                    raise # Re-raise other API errors
        elif action_type == "manual_intervention":
            reason = params.get("reason", "No reason provided.")
            instructions = params.get("instructions", "No instructions provided.")
            return f"Simulation: Manual intervention required. Reason: {reason}. Instructions: {instructions}", True
        else:
            return f"Simulation not implemented for action type: {action_type}", False

    except Exception as e:
        logger.error(f"Unexpected error during simulation of action {action_type}: {e}")
        return f"Unexpected error during simulation: {e}", False


async def process_anomaly(
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    k8s_client: client.ApiClient,
    planner_agent: Agent,
) -> None:
    """
    Processes a single anomaly: generates a remediation plan and executes it.

    Args:
        anomaly_record: The anomaly record to process.
        db: MongoDB database instance.
        k8s_client: Kubernetes API client.
        planner_agent: The AI agent for generating remediation plans.
    """
    logger.info(f"Processing anomaly: {anomaly_record.id}")

    # Check if remediation is disabled
    if settings.remediation_disabled:
        logger.info(
            f"Remediation is disabled. Skipping plan generation and execution for anomaly {anomaly_record.id}."
        )
        return

    # Check if anomaly entity is in a blacklisted namespace
    if anomaly_record.entity_id and "/" in anomaly_record.entity_id:
        namespace = anomaly_record.entity_id.split("/")[0]
        if namespace in settings.blacklisted_namespaces:
            logger.info(
                f"Anomaly entity in blacklisted namespace '{namespace}'. Skipping remediation for anomaly {anomaly_record.id}."
            )
            return

    # Check if a remediation plan already exists for this anomaly
    plans_collection = db["remediation_plans"]
    existing_plan = await plans_collection.find_one({"anomaly_id": str(anomaly_record.id)})
    if existing_plan:
        logger.info(
            f"Remediation plan already exists for anomaly {anomaly_record.id}. Skipping plan generation."
        )
        # Optionally, re-execute the existing plan if needed, but for now we skip
        return

    # Generate remediation plan
    logger.info(f"Generating remediation plan for anomaly {anomaly_record.id}...")
    try:
        dependencies = PlannerDependencies(db=db, k8s_client=k8s_client)
        plan = await generate_remediation_plan(
            planner_agent, anomaly_record, dependencies
        )

        if plan and plan.actions:
            logger.info(
                f"Remediation plan generated for anomaly {anomaly_record.id} with {len(plan.actions)} actions."
            )
            # Execute the plan
            await execute_remediation_plan(k8s_client, anomaly_record, plan, db)
        elif plan:
            logger.info(
                f"Remediation plan generated for anomaly {anomaly_record.id} but contains no actions."
            )
        else:
            logger.warning(
                f"Failed to generate remediation plan for anomaly {anomaly_record.id}."
            )

    except Exception as e:
        logger.exception(
            f"Error processing anomaly {anomaly_record.id} during plan generation or execution: {e}"
        )


async def main():
    """Production-ready main function for running the engine separately."""
    logger.info("Starting KubeWise Remediation Engine...")

    # Load Kubernetes config
    try:
        await load_k8s_config()
        k8s_client = client.ApiClient()
        logger.info("Kubernetes config loaded and API client created.")
    except Exception as e:
        logger.error(f"Failed to load Kubernetes config or create API client: {e}")
        return # Exit if K8s client cannot be initialized

    # Connect to MongoDB
    try:
        mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
            settings.mongo_uri,
            maxPoolSize=100,
            minPoolSize=10,
            maxIdleTimeMS=30000,
            serverSelectionTimeoutMS=30000,  # Increased to 30 seconds
            connectTimeoutMS=20000,          # Increased to 20 seconds
            socketTimeoutMS=45000,           # Increased to 45 seconds
            waitQueueTimeoutMS=20000,        # Increased to 20 seconds
            retryWrites=True,
            w="majority",
            tls=True,                        # Use TLS for connection
            tlsAllowInvalidCertificates=True # Temporarily allow invalid certificates
        )
        db = mongo_client[settings.mongo_db_name]
        logger.info(f"Connected to MongoDB database: {settings.mongo_db_name}")
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        return # Exit if DB connection fails

    # Initialize Planner AI Agent
    try:
        planner_agent = Agent(
            model=settings.gemini_model_id,
            api_key=settings.gemini_api_key.get_secret_value(),
            temperature=settings.ai_temperature,
            max_tokens=settings.ai_max_tokens,
        )
        logger.info(f"Planner AI Agent initialized with model: {settings.gemini_model_id}")
    except Exception as e:
        logger.error(f"Failed to initialize Planner AI Agent: {e}")
        return # Exit if AI agent fails to initialize

    # Main loop for processing anomalies (simplified for example)
    # In a real application, this would likely be triggered by new anomalies
    # from a message queue or database watcher.
    logger.info("Remediation Engine is running. Waiting for anomalies...")

    # Example: Periodically check for unprocessed anomalies
    while True:
        try:
            anomalies_collection = db["anomalies"]
            # Find anomalies that have not been remediated and do not have a plan yet
            # Assuming 'remediated' is a boolean field in AnomalyRecord
            # and we can check for existence of a plan in the remediation_plans collection
            unprocessed_anomalies_cursor = anomalies_collection.aggregate([
                {"$lookup": {
                    "from": "remediation_plans",
                    "localField": "_id",
                    "foreignField": "anomaly_id",
                    "as": "plans"
                }},
                {"$match": {
                    "$or": [
                        {"remediated": {"$exists": False}},
                        {"remediated": False}
                    ],
                    "plans": {"$eq": []} # No existing plan
                }},
                {"$limit": settings.max_parallel_remediation_actions} # Process a limited number at a time
            ])

            unprocessed_anomalies: List[AnomalyRecord] = []
            async for anomaly_doc in unprocessed_anomalies_cursor:
                try:
                    # Convert anomaly_doc to AnomalyRecord Pydantic model
                    # Need to handle ObjectId conversion if necessary
                    if "_id" in anomaly_doc:
                        anomaly_doc["id"] = str(anomaly_doc["_id"])
                        del anomaly_doc["_id"]
                    unprocessed_anomalies.append(AnomalyRecord(**anomaly_doc))
                except ValidationError as e:
                    logger.error(f"Failed to validate AnomalyRecord from DB: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error processing anomaly document from DB: {e}")


            if unprocessed_anomalies:
                logger.info(f"Found {len(unprocessed_anomalies)} unprocessed anomalies.")
                # Process anomalies concurrently
                await asyncio.gather(
                    *[
                        process_anomaly(anomaly, db, k8s_client, planner_agent)
                        for anomaly in unprocessed_anomalies
                    ]
                )
            else:
                logger.debug("No unprocessed anomalies found. Waiting...")

        except Exception as e:
            logger.error(f"Error in main processing loop: {e}")
            # Continue loop even on error

        await asyncio.sleep(settings.prom_queries_poll_interval) # Wait before checking again


if __name__ == "__main__":
    # Configure loguru logger
    logger.add(
        "kubewise.log",
        rotation="1 MB",
        level=settings.log_level,
        enqueue=True, # Use a queue for thread safety
    )
    logger.info(f"Log level set to: {settings.log_level}")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Remediation Engine stopped manually.")
    except Exception as e:
        logger.exception("Remediation Engine stopped due to an unhandled exception.")
