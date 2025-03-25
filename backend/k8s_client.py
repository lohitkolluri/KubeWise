from kubernetes import client, config
import os
from kubernetes.client.rest import ApiException
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("k8s_client")

# Load kubeconfig or in-cluster config
try:
    # Try to load in-cluster config (when running in a pod)
    config.load_incluster_config()
    logger.info("Loaded in-cluster configuration")
except config.ConfigException:
    try:
        # Try to load kubeconfig from default location or from KUBECONFIG env var
        kube_config_path = os.environ.get('KUBE_CONFIG_PATH')
        if kube_config_path:
            config.load_kube_config(kube_config_path)
            logger.info(f"Loaded kubeconfig from {kube_config_path}")
        else:
            config.load_kube_config()
            logger.info("Loaded kubeconfig from default location")
    except Exception as e:
        logger.error(f"Failed to load Kubernetes configuration: {e}")
        raise

# Initialize API clients
api_client = client.ApiClient()
core_v1 = client.CoreV1Api(api_client)
apps_v1 = client.AppsV1Api(api_client)
batch_v1 = client.BatchV1Api(api_client)

def get_cluster_info():
    """
    Get basic information about the Kubernetes cluster

    Returns:
        Dictionary with node and pod counts
    """
    try:
        # Get node count
        nodes = core_v1.list_node().items
        node_count = len(nodes)

        # Get pod count
        pods = core_v1.list_pod_for_all_namespaces().items
        pod_count = len(pods)

        # Get namespace count
        namespaces = core_v1.list_namespace().items
        namespace_count = len(namespaces)

        return {
            "nodes": node_count,
            "pods": pod_count,
            "namespaces": namespace_count
        }
    except ApiException as e:
        logger.error(f"Error getting cluster info: {e}")
        return {
            "nodes": 0,
            "pods": 0,
            "namespaces": 0
        }

def get_node_info():
    """Get detailed information about cluster nodes"""
    try:
        nodes = core_v1.list_node().items
        node_info = []

        for node in nodes:
            # Extract basic node info
            node_name = node.metadata.name

            # Get node status
            status = "Unknown"
            for condition in node.status.conditions:
                if condition.type == "Ready":
                    status = "Ready" if condition.status == "True" else "NotReady"
                    break

            # Get capacity and allocatable resources
            capacity = node.status.capacity
            allocatable = node.status.allocatable

            # Get node labels and taints
            labels = node.metadata.labels if node.metadata.labels else {}
            taints = node.spec.taints if node.spec.taints else []

            # Build node info object
            node_info.append({
                "name": node_name,
                "status": status,
                "capacity": {
                    "cpu": capacity.get("cpu", "N/A"),
                    "memory": capacity.get("memory", "N/A"),
                    "pods": capacity.get("pods", "N/A")
                },
                "allocatable": {
                    "cpu": allocatable.get("cpu", "N/A"),
                    "memory": allocatable.get("memory", "N/A"),
                    "pods": allocatable.get("pods", "N/A")
                },
                "labels": labels,
                "taints": [{"key": t.key, "effect": t.effect} for t in taints]
            })

        return node_info
    except ApiException as e:
        logger.error(f"Error getting node info: {e}")
        return []

def get_namespaced_pods(namespace="default"):
    """Get pods in a specific namespace"""
    try:
        pods = core_v1.list_namespaced_pod(namespace).items
        pod_list = []

        for pod in pods:
            # Get container statuses
            container_statuses = pod.status.container_statuses if pod.status.container_statuses else []
            containers = []

            for cs in container_statuses:
                # Determine container state
                state = "unknown"
                if cs.state.running:
                    state = "running"
                elif cs.state.waiting:
                    state = f"waiting ({cs.state.waiting.reason})"
                elif cs.state.terminated:
                    state = f"terminated ({cs.state.terminated.reason})"

                container = {
                    "name": cs.name,
                    "ready": cs.ready,
                    "restartCount": cs.restart_count,
                    "state": state
                }
                containers.append(container)

            # Build pod info
            pod_info = {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "phase": pod.status.phase,
                "hostIP": pod.status.host_ip,
                "podIP": pod.status.pod_ip,
                "containers": containers
            }

            pod_list.append(pod_info)

        return pod_list
    except ApiException as e:
        logger.error(f"Error getting pods in namespace {namespace}: {e}")
        return []

def get_all_deployments():
    """Get all deployments across all namespaces"""
    try:
        deployments = apps_v1.list_deployment_for_all_namespaces().items
        deployment_list = []

        for deployment in deployments:
            deployment_info = {
                "name": deployment.metadata.name,
                "namespace": deployment.metadata.namespace,
                "replicas": deployment.spec.replicas,
                "availableReplicas": deployment.status.available_replicas or 0,
                "unavailableReplicas": deployment.status.unavailable_replicas or 0,
                "labels": deployment.metadata.labels or {}
            }
            deployment_list.append(deployment_info)

        return deployment_list
    except ApiException as e:
        logger.error(f"Error getting deployments: {e}")
        return []

def get_pod_logs(pod_name, namespace="default", container=None, tail_lines=100):
    """Get logs from a specific pod"""
    try:
        if container:
            logs = core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines
            )
        else:
            logs = core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=tail_lines
            )

        return logs
    except ApiException as e:
        logger.error(f"Error getting logs for pod {pod_name}: {e}")
        return f"Error fetching logs: {e}"

def perform_remediation_action(action_type, target, parameters=None):
    """
    Execute a remediation action on the Kubernetes cluster

    Args:
        action_type: Type of action (restartPod, scaleDeployment, etc.)
        target: The resource to act on
        parameters: Additional parameters for the action

    Returns:
        Dictionary with action result
    """
    if parameters is None:
        parameters = {}

    try:
        # Handle different action types
        if action_type == "restartPod":
            return restart_pod(target, parameters.get("namespace", "default"))

        elif action_type == "scaleDeployment":
            namespace = parameters.get("namespace", "default")
            replicas = parameters.get("replicas")
            if not replicas:
                raise ValueError("Replicas must be specified for scaling")

            return scale_deployment(target, namespace, replicas)

        elif action_type == "cordonNode":
            return cordon_node(target, True)  # True for cordon, False for uncordon

        elif action_type == "uncordonNode":
            return cordon_node(target, False)

        elif action_type == "drainNode":
            force = parameters.get("force", False)
            return drain_node(target, force)

        else:
            raise ValueError(f"Unknown action type: {action_type}")

    except Exception as e:
        logger.error(f"Error performing {action_type} on {target}: {e}")
        raise

def restart_pod(pod_name, namespace="default"):
    """
    Restart a pod by deleting it (it will be recreated by its controller)

    Args:
        pod_name: Name of the pod to restart
        namespace: Namespace of the pod

    Returns:
        Dictionary with description and status
    """
    try:
        # Delete the pod (which forces a restart if managed by controller)
        core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        logger.info(f"Successfully restarted pod {pod_name} in namespace {namespace}")

        return {
            "description": f"Restarted pod {pod_name}",
            "status": "success"
        }
    except ApiException as e:
        logger.error(f"Error restarting pod {pod_name} in namespace {namespace}: {e}")
        raise

def scale_deployment(deployment_name, namespace="default", replicas=None):
    """
    Scale a deployment to a specified number of replicas

    Args:
        deployment_name: Name of the deployment to scale
        namespace: Namespace of the deployment
        replicas: Target number of replicas (if None, increase by 1)

    Returns:
        Dictionary with description and status
    """
    try:
        # Get the current deployment
        deployment = apps_v1.read_namespaced_deployment(
            name=deployment_name,
            namespace=namespace
        )

        current_replicas = deployment.spec.replicas

        # If replicas not specified, increase by 1
        if replicas is None:
            new_replicas = current_replicas + 1
        else:
            new_replicas = replicas

        # Only update if changing
        if new_replicas != current_replicas:
            # Update the deployment
            deployment.spec.replicas = new_replicas
            apps_v1.patch_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                body=deployment
            )

            action = "increased" if new_replicas > current_replicas else "decreased"
            logger.info(f"Successfully scaled deployment {deployment_name} from {current_replicas} to {new_replicas} replicas")

            return {
                "description": f"Scaled deployment {deployment_name} from {current_replicas} to {new_replicas} replicas",
                "status": "success",
                "previousReplicas": current_replicas,
                "newReplicas": new_replicas
            }
        else:
            logger.info(f"Deployment {deployment_name} already has {current_replicas} replicas, no scaling performed")
            return {
                "description": f"No change to deployment {deployment_name} (already at {current_replicas} replicas)",
                "status": "unchanged",
                "replicas": current_replicas
            }

    except ApiException as e:
        logger.error(f"Error scaling deployment {deployment_name} in namespace {namespace}: {e}")
        raise

def cordon_node(node_name, cordon=True):
    """
    Cordon or uncordon a node

    Args:
        node_name: Name of the node
        cordon: True to cordon, False to uncordon

    Returns:
        Dictionary with description and status
    """
    try:
        # Get the node
        node = core_v1.read_node(name=node_name)

        # Check if node is already in desired state
        is_cordoned = "unschedulable" in node.spec.taints or node.spec.unschedulable

        if cordon == is_cordoned:
            action = "cordon" if cordon else "uncordon"
            logger.info(f"Node {node_name} already {action}ed, no action needed")
            return {
                "description": f"Node {node_name} already {action}ed",
                "status": "unchanged"
            }

        # Prepare the patch to apply
        body = {
            "spec": {
                "unschedulable": cordon
            }
        }

        # Apply the patch
        core_v1.patch_node(name=node_name, body=body)

        action = "cordoned" if cordon else "uncordoned"
        logger.info(f"Successfully {action} node {node_name}")

        return {
            "description": f"{action.capitalize()} node {node_name}",
            "status": "success"
        }

    except ApiException as e:
        action = "cordon" if cordon else "uncordon"
        logger.error(f"Error {action}ing node {node_name}: {e}")
        raise

def drain_node(node_name, force=False):
    """
    Drain a node (cordon + evict pods)

    NOTE: This is a simplified implementation. A complete drain would require:
    1. Cordoning the node
    2. Getting all pods on the node
    3. Evicting each pod (respecting PDBs unless force=True)
    4. Handling DaemonSets appropriately

    Args:
        node_name: Name of the node to drain
        force: Whether to force eviction (ignore PDBs)

    Returns:
        Dictionary with description and status
    """
    try:
        # First cordon the node
        cordon_result = cordon_node(node_name, True)

        # Now evict all pods from the node
        field_selector = f'spec.nodeName={node_name}'
        pods = core_v1.list_pod_for_all_namespaces(field_selector=field_selector).items

        evicted_pods = 0
        for pod in pods:
            # Skip DaemonSet pods
            owner_references = pod.metadata.owner_references or []
            if any(ref.kind == "DaemonSet" for ref in owner_references):
                continue

            try:
                # Create eviction object
                eviction = client.V1Eviction(
                    metadata=client.V1ObjectMeta(
                        name=pod.metadata.name,
                        namespace=pod.metadata.namespace
                    )
                )

                # Evict the pod
                core_v1.create_namespaced_pod_eviction(
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                    body=eviction
                )
                evicted_pods += 1
            except ApiException as e:
                if force:
                    # If force=True, delete the pod instead of evicting
                    core_v1.delete_namespaced_pod(
                        name=pod.metadata.name,
                        namespace=pod.metadata.namespace
                    )
                    evicted_pods += 1
                else:
                    logger.warning(f"Failed to evict pod {pod.metadata.name}: {e}")

        logger.info(f"Drained node {node_name}, evicted {evicted_pods} pods")

        return {
            "description": f"Drained node {node_name}",
            "status": "success",
            "evictedPods": evicted_pods
        }

    except ApiException as e:
        logger.error(f"Error draining node {node_name}: {e}")
        raise

def update_deployment_resources(deployment_name, namespace="default", cpu_limit=None, memory_limit=None, cpu_request=None, memory_request=None):
    """
    Update resource requests and limits for a deployment

    Args:
        deployment_name: Name of the deployment
        namespace: Namespace of the deployment
        cpu_limit: New CPU limit (e.g., "500m", "1")
        memory_limit: New memory limit (e.g., "512Mi", "1Gi")
        cpu_request: New CPU request
        memory_request: New memory request

    Returns:
        Dictionary with description and status
    """
    try:
        # Get the current deployment
        deployment = apps_v1.read_namespaced_deployment(
            name=deployment_name,
            namespace=namespace
        )

        # Get the container templates
        containers = deployment.spec.template.spec.containers

        # Make sure there's at least one container
        if not containers:
            return {
                "description": f"No containers found in deployment {deployment_name}",
                "status": "error"
            }

        # For simplicity, update the first container only
        # In a real system, you'd probably want to handle multiple containers
        container = containers[0]

        # Create resources if they don't exist
        if not container.resources:
            container.resources = client.V1ResourceRequirements(
                limits={},
                requests={}
            )

        # Update limits if provided
        if container.resources.limits is None:
            container.resources.limits = {}

        if cpu_limit:
            container.resources.limits["cpu"] = cpu_limit
        if memory_limit:
            container.resources.limits["memory"] = memory_limit

        # Update requests if provided
        if container.resources.requests is None:
            container.resources.requests = {}

        if cpu_request:
            container.resources.requests["cpu"] = cpu_request
        if memory_request:
            container.resources.requests["memory"] = memory_request

        # Update the deployment
        apps_v1.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=deployment
        )

        logger.info(f"Updated resources for deployment {deployment_name}")

        return {
            "description": f"Updated resources for deployment {deployment_name}",
            "status": "success",
            "container": container.name,
            "limits": container.resources.limits,
            "requests": container.resources.requests
        }

    except ApiException as e:
        logger.error(f"Error updating resources for deployment {deployment_name}: {e}")
        raise
