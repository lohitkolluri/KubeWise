import asyncio
import datetime
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger

from kubewise.models import ActionType, RemediationAction, RemediationPlan
from kubewise.utils.retry import with_exponential_backoff

# Add implementation for new action types

async def execute_update_container_image(
    action: RemediationAction,
    k8s_client: client.ApiClient,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Update a container's image in a Deployment, StatefulSet, or DaemonSet.
    
    Args:
        action: The RemediationAction
        k8s_client: Kubernetes API client
        context: Optional execution context
        
    Returns:
        Tuple of (success boolean, message string)
    """
    params = action.parameters
    name = params.get("name")
    namespace = params.get("namespace", "default")
    kind = params.get("kind", "Deployment").lower()
    container = params.get("container")
    image = params.get("image")
    
    if not name or not container or not image:
        return False, "Missing required parameters: name, container, or image"
    
    apps_v1 = client.AppsV1Api(k8s_client)
    
    try:
        # Get the current resource
        if kind == "deployment":
            resource = await apps_v1.read_namespaced_deployment(
                name=name, namespace=namespace
            )
            
            # Find the specified container and update its image
            for container_spec in resource.spec.template.spec.containers:
                if container_spec.name == container:
                    container_spec.image = image
                    break
            else:
                return False, f"Container '{container}' not found in deployment"
            
            # Update the deployment
            await apps_v1.patch_namespaced_deployment(
                name=name,
                namespace=namespace,
                body=resource
            )
            
            return True, f"Successfully updated image for container '{container}' in deployment '{name}' to '{image}'"
            
        elif kind == "statefulset":
            resource = await apps_v1.read_namespaced_stateful_set(
                name=name, namespace=namespace
            )
            
            # Find the specified container and update its image
            for container_spec in resource.spec.template.spec.containers:
                if container_spec.name == container:
                    container_spec.image = image
                    break
            else:
                return False, f"Container '{container}' not found in statefulset"
            
            # Update the statefulset
            await apps_v1.patch_namespaced_stateful_set(
                name=name,
                namespace=namespace,
                body=resource
            )
            
            return True, f"Successfully updated image for container '{container}' in statefulset '{name}' to '{image}'"
            
        elif kind == "daemonset":
            resource = await apps_v1.read_namespaced_daemon_set(
                name=name, namespace=namespace
            )
            
            # Find the specified container and update its image
            for container_spec in resource.spec.template.spec.containers:
                if container_spec.name == container:
                    container_spec.image = image
                    break
            else:
                return False, f"Container '{container}' not found in daemonset"
            
            # Update the daemonset
            await apps_v1.patch_namespaced_daemon_set(
                name=name,
                namespace=namespace,
                body=resource
            )
            
            return True, f"Successfully updated image for container '{container}' in daemonset '{name}' to '{image}'"
            
        else:
            return False, f"Unsupported resource kind: {kind}"
    
    except client.ApiException as e:
        return False, f"API error updating image: {e.status} - {e.reason}"
    except Exception as e:
        return False, f"Error updating image: {str(e)}"

async def execute_update_resource_limits(
    action: RemediationAction,
    k8s_client: client.ApiClient,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Update resource limits and requests for a container in a Kubernetes resource.
    
    Args:
        action: The RemediationAction
        k8s_client: Kubernetes API client
        context: Optional execution context
        
    Returns:
        Tuple of (success boolean, message string)
    """
    params = action.parameters
    name = params.get("name")
    namespace = params.get("namespace", "default")
    kind = params.get("kind", "Deployment").lower()
    container = params.get("container")
    limits = params.get("limits", {})
    requests = params.get("requests", {})
    
    if not name or not container:
        return False, "Missing required parameters: name or container"
    
    if not limits and not requests:
        return False, "At least one of limits or requests must be provided"
    
    try:
        # Get the appropriate API based on resource kind
        if kind in ["deployment", "statefulset", "daemonset"]:
            apps_v1 = client.AppsV1Api(k8s_client)
            
            # Get the current resource
            if kind == "deployment":
                resource = await apps_v1.read_namespaced_deployment(
                    name=name, namespace=namespace
                )
            elif kind == "statefulset":
                resource = await apps_v1.read_namespaced_stateful_set(
                    name=name, namespace=namespace
                )
            elif kind == "daemonset":
                resource = await apps_v1.read_namespaced_daemon_set(
                    name=name, namespace=namespace
                )
            
            # Find the specified container and update its resource requirements
            for container_spec in resource.spec.template.spec.containers:
                if container_spec.name == container:
                    # Create or update resource requirements
                    if not container_spec.resources:
                        container_spec.resources = client.V1ResourceRequirements()
                    
                    # Update limits if provided
                    if limits:
                        container_spec.resources.limits = limits
                    
                    # Update requests if provided
                    if requests:
                        container_spec.resources.requests = requests
                    
                    break
            else:
                return False, f"Container '{container}' not found in {kind}"
            
            # Update the resource
            if kind == "deployment":
                await apps_v1.patch_namespaced_deployment(
                    name=name, namespace=namespace, body=resource
                )
            elif kind == "statefulset":
                await apps_v1.patch_namespaced_stateful_set(
                    name=name, namespace=namespace, body=resource
                )
            elif kind == "daemonset":
                await apps_v1.patch_namespaced_daemon_set(
                    name=name, namespace=namespace, body=resource
                )
            
            return True, f"Successfully updated resource requirements for container '{container}' in {kind} '{name}'"
            
        elif kind == "pod":
            core_v1 = client.CoreV1Api(k8s_client)
            
            # Pods are immutable - we can't update them directly
            # We'll need to delete and recreate, which is risky
            return False, "Cannot update resource limits for a running pod. Consider targeting the pod's controller (Deployment, StatefulSet, etc.) instead."
        
        else:
            return False, f"Unsupported resource kind: {kind}"
    
    except client.ApiException as e:
        return False, f"API error updating resource limits: {e.status} - {e.reason}"
    except Exception as e:
        return False, f"Error updating resource limits: {str(e)}"

async def execute_fix_service_selectors(
    action: RemediationAction,
    k8s_client: client.ApiClient,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Fix service selectors to match pod labels.
    
    Args:
        action: The RemediationAction
        k8s_client: Kubernetes API client
        context: Optional execution context
        
    Returns:
        Tuple of (success boolean, message string)
    """
    params = action.parameters
    name = params.get("name")
    namespace = params.get("namespace", "default")
    selectors = params.get("selectors", {})
    
    if not name:
        return False, "Missing required parameter: name"
    
    if not selectors:
        return False, "Missing required parameter: selectors"
    
    core_v1 = client.CoreV1Api(k8s_client)
    
    try:
        # Get the current service
        service = await core_v1.read_namespaced_service(
            name=name, namespace=namespace
        )
        
        # Update the service selectors
        service.spec.selector = selectors
        
        # Update the service
        await core_v1.patch_namespaced_service(
            name=name, namespace=namespace, body=service
        )
        
        return True, f"Successfully updated selectors for service '{name}'"
    
    except client.ApiException as e:
        return False, f"API error updating service selectors: {e.status} - {e.reason}"
    except Exception as e:
        return False, f"Error updating service selectors: {str(e)}"

async def execute_rollback_deployment(
    action: RemediationAction,
    k8s_client: client.ApiClient,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Rollback a deployment to a previous revision.
    
    Args:
        action: The RemediationAction
        k8s_client: Kubernetes API client
        context: Optional execution context
        
    Returns:
        Tuple of (success boolean, message string)
    """
    params = action.parameters
    name = params.get("name")
    namespace = params.get("namespace", "default")
    revision = params.get("revision")
    
    if not name:
        return False, "Missing required parameter: name"
    
    apps_v1 = client.AppsV1Api(k8s_client)
    
    try:
        # First, check if a specific revision was requested
        if revision:
            # Use rollback API with specific revision
            rollback_body = client.V1beta1DeploymentRollback(
                name=name,
                rollback_to=client.AppsV1beta1RollbackConfig(
                    revision=revision
                )
            )
            
            # Note: This works in earlier K8s versions, but in newer versions
            # we need a different approach using ReplicaSets
            try:
                await apps_v1.create_namespaced_deployment_rollback(
                    name=name, namespace=namespace, body=rollback_body
                )
                return True, f"Successfully rolled back deployment '{name}' to revision {revision}"
            except client.ApiException as rollback_err:
                if rollback_err.status == 404 or rollback_err.status == 405:
                    # Rollback API not available or deprecated, use ReplicaSet approach
                    logger.warning("Deployment rollback API not available, using ReplicaSet approach")
                    pass
                else:
                    raise rollback_err
        
        # Get all ReplicaSets for this deployment to find the desired revision
        selector = ""
        deployment = await apps_v1.read_namespaced_deployment(
            name=name, namespace=namespace
        )
        
        if deployment.spec.selector and deployment.spec.selector.match_labels:
            selector_items = [f"{k}={v}" for k, v in deployment.spec.selector.match_labels.items()]
            selector = ",".join(selector_items)
        else:
            return False, "Deployment has no selector labels to identify its ReplicaSets"
        
        rs_list = await apps_v1.list_namespaced_replica_set(
            namespace=namespace, label_selector=selector
        )
        
        # Find the ReplicaSet with the desired revision or the previous revision
        target_rs = None
        
        # Sort by creation timestamp, newest first
        sorted_rs = sorted(
            rs_list.items,
            key=lambda x: x.metadata.creation_timestamp or datetime.datetime.min,
            reverse=True
        )
        
        if revision:
            # Look for a specific revision
            for rs in sorted_rs:
                if rs.metadata.annotations and "deployment.kubernetes.io/revision" in rs.metadata.annotations:
                    rs_revision = rs.metadata.annotations["deployment.kubernetes.io/revision"]
                    if rs_revision == str(revision):
                        target_rs = rs
                        break
            
            if not target_rs:
                return False, f"Revision {revision} not found for deployment '{name}'"
        else:
            # If no specific revision, use the second most recent (previous version)
            if len(sorted_rs) >= 2:
                target_rs = sorted_rs[1]  # Second newest
            else:
                return False, "No previous revision available for rollback"
        
        # Get the template from the target ReplicaSet
        template = target_rs.spec.template
        
        # Update the deployment's template
        deployment.spec.template = template
        
        # Patch the deployment
        await apps_v1.patch_namespaced_deployment(
            name=name, namespace=namespace, body=deployment
        )
        
        return True, f"Successfully rolled back deployment '{name}' to previous revision"
    
    except client.ApiException as e:
        return False, f"API error rolling back deployment: {e.status} - {e.reason}"
    except Exception as e:
        return False, f"Error rolling back deployment: {str(e)}"

async def execute_fix_dns_config(
    action: RemediationAction,
    k8s_client: client.ApiClient,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Fix DNS configuration for a Pod, Deployment, StatefulSet, or DaemonSet.
    
    Args:
        action: The RemediationAction
        k8s_client: Kubernetes API client
        context: Optional execution context
        
    Returns:
        Tuple of (success boolean, message string)
    """
    params = action.parameters
    name = params.get("name")
    namespace = params.get("namespace", "default")
    kind = params.get("kind", "Deployment").lower()
    dns_policy = params.get("dns_policy", "ClusterFirst")
    nameservers = params.get("nameservers", [])
    searches = params.get("searches", [])
    options = params.get("options", [])
    
    if not name:
        return False, "Missing required parameter: name"
    
    try:
        # Create DNS config object
        dns_config = client.V1PodDNSConfig(
            nameservers=nameservers,
            searches=searches,
            options=[client.V1PodDNSConfigOption(name=opt.get("name"), value=opt.get("value")) for opt in options] if options else None
        )
        
        # Update the appropriate resource based on kind
        if kind in ["deployment", "statefulset", "daemonset"]:
            apps_v1 = client.AppsV1Api(k8s_client)
            
            if kind == "deployment":
                resource = await apps_v1.read_namespaced_deployment(
                    name=name, namespace=namespace
                )
                
                # Update DNS policy and config
                resource.spec.template.spec.dns_policy = dns_policy
                resource.spec.template.spec.dns_config = dns_config
                
                await apps_v1.patch_namespaced_deployment(
                    name=name, namespace=namespace, body=resource
                )
                
            elif kind == "statefulset":
                resource = await apps_v1.read_namespaced_stateful_set(
                    name=name, namespace=namespace
                )
                
                # Update DNS policy and config
                resource.spec.template.spec.dns_policy = dns_policy
                resource.spec.template.spec.dns_config = dns_config
                
                await apps_v1.patch_namespaced_stateful_set(
                    name=name, namespace=namespace, body=resource
                )
                
            elif kind == "daemonset":
                resource = await apps_v1.read_namespaced_daemon_set(
                    name=name, namespace=namespace
                )
                
                # Update DNS policy and config
                resource.spec.template.spec.dns_policy = dns_policy
                resource.spec.template.spec.dns_config = dns_config
                
                await apps_v1.patch_namespaced_daemon_set(
                    name=name, namespace=namespace, body=resource
                )
            
            return True, f"Successfully updated DNS configuration for {kind} '{name}'"
            
        elif kind == "pod":
            core_v1 = client.CoreV1Api(k8s_client)
            
            # Pods are immutable - need to delete and recreate
            return False, "Cannot update DNS configuration for a running pod. Consider targeting the pod's controller (Deployment, StatefulSet, etc.) instead."
        
        else:
            return False, f"Unsupported resource kind: {kind}"
    
    except client.ApiException as e:
        return False, f"API error updating DNS configuration: {e.status} - {e.reason}"
    except Exception as e:
        return False, f"Error updating DNS configuration: {str(e)}"

async def execute_force_delete_pod(
    action: RemediationAction,
    k8s_client: client.ApiClient,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Force delete a pod that might be stuck in Terminating state.
    
    Args:
        action: The RemediationAction
        k8s_client: Kubernetes API client
        context: Optional execution context
        
    Returns:
        Tuple of (success boolean, message string)
    """
    params = action.parameters
    name = params.get("name")
    namespace = params.get("namespace", "default")
    
    if not name:
        return False, "Missing required parameter: name"
    
    core_v1 = client.CoreV1Api(k8s_client)
    
    try:
        # First try normal deletion with grace period of 0
        try:
            await core_v1.delete_namespaced_pod(
                name=name,
                namespace=namespace,
                grace_period_seconds=0,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0,
                    propagation_policy="Background"
                )
            )
            
            # Wait a brief period to see if the pod is actually deleted
            await asyncio.sleep(5)
            
            # Check if pod still exists
            try:
                pod = await core_v1.read_namespaced_pod(name=name, namespace=namespace)
                # Pod still exists, need to use force deletion
                if pod.metadata.deletion_timestamp:
                    logger.info(f"Pod {name} still exists and is in Terminating state, force deleting...")
                else:
                    return True, f"Successfully deleted pod '{name}'"
            except client.ApiException as e:
                if e.status == 404:
                    # Pod is gone, deletion was successful
                    return True, f"Successfully deleted pod '{name}'"
                raise e
        
        except client.ApiException as delete_err:
            if delete_err.status != 404:  # Ignore if pod was already deleted
                logger.warning(f"Error during normal deletion: {delete_err}")
                # Continue to force deletion
        
        # For force deletion, we need to clear finalizers
        logger.info(f"Force deleting pod {name} in namespace {namespace}")
        
        # Use JSON patch to remove finalizers
        patch = [
            {
                "op": "remove",
                "path": "/metadata/finalizers"
            }
        ]
        
        # Apply the patch
        try:
            await core_v1.patch_namespaced_pod(
                name=name,
                namespace=namespace,
                body=patch,
                content_type="application/json-patch+json"
            )
            logger.info(f"Successfully removed finalizers from pod {name}")
        except client.ApiException as patch_err:
            if patch_err.status != 404:  # Ignore if pod was already deleted
                logger.warning(f"Error removing finalizers: {patch_err}")
                # Continue anyway since finalizers might not exist
        
        # Try deletion again
        try:
            await core_v1.delete_namespaced_pod(
                name=name,
                namespace=namespace,
                grace_period_seconds=0,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0,
                    propagation_policy="Background"
                )
            )
        except client.ApiException as final_delete_err:
            if final_delete_err.status != 404:  # Ignore if pod was already deleted
                return False, f"Failed to force delete pod: {final_delete_err.status} - {final_delete_err.reason}"
        
        return True, f"Successfully force deleted pod '{name}'"
    
    except Exception as e:
        return False, f"Error force deleting pod: {str(e)}"

# Map of action types to their executor functions
ACTION_EXECUTORS = {
    # Add your new action executor functions to this map
    ActionType.UPDATE_CONTAINER_IMAGE: execute_update_container_image,
    ActionType.UPDATE_RESOURCE_LIMITS: execute_update_resource_limits,
    ActionType.FIX_SERVICE_SELECTORS: execute_fix_service_selectors,
    ActionType.ROLLBACK_DEPLOYMENT: execute_rollback_deployment,
    ActionType.FIX_DNS_CONFIG: execute_fix_dns_config,
    ActionType.FORCE_DELETE_POD: execute_force_delete_pod,
} 