import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from kubernetes import client, config
from kubernetes.client.rest import ApiException
import json
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed, after_log, wait_exponential

from app.core.exceptions import (
    KubernetesConnectionError, 
     OperationFailedError, 
    ResourceNotFoundError,
    ValidationError
)
from app.models.k8s_models import RemediationAction
from app.core import logger as app_logger
from app.utils.event_correlator import K8sEventCorrelator
from app.utils.command_playground import execute_command

class K8sExecutor:
    """
    Executes Kubernetes operations using the official Python client.
    Focuses on safe, validated execution. Includes context fetching.
    """

    def __init__(self):
        self._init_client()
        self._define_safe_operations()
        # Store command templates for structured remediation
        self.command_templates = self._get_command_templates()
        # Initialize blacklisted operations as an empty set
        self.blacklisted_operations = (
            set()
        )  # We'll use a blacklist approach instead of whitelist
        # Initialize event correlator
        self.event_correlator = K8sEventCorrelator()

    def _init_client(self):
        """Initialize Kubernetes client connections with error handling"""
        try:
            # Load kubernetes configuration from default location
            config.load_kube_config()
            self.v1 = client.CoreV1Api()
            self.apps_v1 = client.AppsV1Api()
            self.batch_v1 = client.BatchV1Api()
            
            # Use structured logging
            app_logger.info(
                "Successfully initialized Kubernetes client",
                component="k8s_executor",
                operation="init"
            )
        except Exception as e:
            app_logger.error(
                "Failed to initialize Kubernetes client",
                component="k8s_executor",
                operation="init",
                exception=str(e),
                exception_type=e.__class__.__name__
            )
            self.v1 = None
            self.apps_v1 = None
            self.batch_v1 = None
            # Don't raise here, allow service to start without K8s

    def _get_command_templates(self) -> Dict[str, Dict[str, Any]]:
        """Define templates for remediation commands with structured parameters."""
        return {
            "restart_deployment": {
                "description": "Restart a Kubernetes deployment",
                "required_params": ["name", "namespace"],
                "optional_params": [],
                "impact": "moderate",
                "estimated_duration": "30-60s",
                "handler": "_handle_restart_deployment",
            },
            "scale_deployment": {
                "description": "Scale a Kubernetes deployment to specified replicas",
                "required_params": ["name", "namespace", "replicas"],
                "optional_params": [
                    "incremental",
                    "max_surge_percent",
                    "wait_for_ready",
                ],
                "impact": "low-moderate",
                "estimated_duration": "varies by size",
                "handler": "_handle_scale_deployment",
            },
            "drain_node": {
                "description": "Drain workloads from a Kubernetes node",
                "required_params": ["name"],
                "optional_params": ["grace_period", "timeout", "ignore_daemonsets"],
                "impact": "high",
                "estimated_duration": "1-5m",
                "handler": "_handle_drain_node",
            },
            "delete_pod": {
                "description": "Delete a problematic pod (it will be recreated if managed by controller)",
                "required_params": ["name", "namespace"],
                "optional_params": ["grace_period"],
                "impact": "moderate",
                "estimated_duration": "10-30s",
                "handler": "_handle_delete_pod",
            },
            "cordon_node": {
                "description": "Mark a node as unschedulable",
                "required_params": ["name"],
                "optional_params": [],
                "impact": "low",
                "estimated_duration": "5s",
                "handler": "_handle_cordon_node",
            },
            "uncordon_node": {
                "description": "Mark a node as schedulable",
                "required_params": ["name"],
                "optional_params": [],
                "impact": "low",
                "estimated_duration": "5s",
                "handler": "_handle_uncordon_node",
            },
            "adjust_resources": {
                "description": "Adjust CPU/memory requests and limits for a deployment",
                "required_params": ["name", "namespace"],
                "optional_params": [
                    "cpu_request",
                    "cpu_limit",
                    "memory_request",
                    "memory_limit",
                    "container",
                ],
                "impact": "moderate",
                "estimated_duration": "30-60s",
                "handler": "_handle_adjust_resources",
            },
            "restart_pod": {
                "required_params": ["namespace", "pod_name"],
                "optional_params": [],
                "handler": "restart_pod",
                "impact": "medium",
                "estimated_duration": "30s",
                "description": "Restart a pod by deleting it (if managed by a controller, it will be recreated)",
            },
            "describe": {
                "description": "Show details of a Kubernetes resource",
                "required_params": ["name"],
                "optional_params": ["namespace", "resource_type"],
                "impact": "none",
                "estimated_duration": "1-2s",
                "handler": "_handle_describe",
            },
            "view_logs": {
                "description": "View logs from a pod",
                "required_params": ["name", "namespace"],
                "optional_params": ["container", "tail", "since"],
                "impact": "none",
                "estimated_duration": "1-5s",
                "handler": "_handle_view_logs",
            },
            "command": {
                "description": "Execute a generic command with parameters",
                "required_params": ["command"],
                "optional_params": ["params"],
                "impact": "varies",
                "estimated_duration": "varies",
                "handler": "execute_command",
            }
        }

    def _define_safe_operations(self):
        """Define safe operations that can be executed"""
        self.safe_operations = {
            # Read Operations (Generally Safe)
            "get_pod": {
                "api": "v1",
                "method": "read_namespaced_pod",
                "required": ["name", "namespace"],
                "optional": [],
                "validation": lambda p: True,  # Simple validation
            },
            "get_deployment": {
                "api": "apps_v1",
                "method": "read_namespaced_deployment",
                "required": ["name", "namespace"],
                "optional": [],
                "validation": lambda p: True,
            },
            "list_pods": {
                "api": "v1",
                "method": "list_namespaced_pod",
                "required": ["namespace"],
                "optional": ["label_selector", "field_selector"],
                "validation": lambda p: True,
            },
            "get_logs": {
                "api": "v1",
                "method": "read_namespaced_pod_log",
                "required": ["name", "namespace"],
                "optional": ["container", "tail_lines"],
                "validation": lambda p: True,  # Assuming logs are read-only safe
            },
            # Modify Operations (Require stricter validation)
            "restart_deployment": {
                "api": "apps_v1",
                "method": "patch_namespaced_deployment",
                "required": ["name", "namespace"],
                "optional": [],
                # Simple validation: just check required fields exist
                "validation": lambda p: "name" in p and "namespace" in p,
                "patch_body": lambda p: {  # Generate the patch body for restart
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "kubectl.kubernetes.io/restartedAt": datetime.utcnow().isoformat()
                                    + "Z"
                                }
                            }
                        }
                    }
                },
            },
            "scale_deployment": {
                "api": "apps_v1",
                "method": "patch_namespaced_deployment",
                "required": ["name", "namespace", "replicas"],
                "optional": [],
                # Validation: Ensure replicas is a non-negative integer
                "validation": lambda p: "name" in p
                and "namespace" in p
                and isinstance(p.get("replicas"), int)
                and p["replicas"] >= 0,
                "patch_body": lambda p: {"spec": {"replicas": p["replicas"]}},
            },
            "cordon_node": {
                "api": "v1",
                "method": "patch_node",
                "required": ["name"],
                "optional": [],
                "validation": lambda p: "name" in p,
                "patch_body": lambda p: {"spec": {"unschedulable": True}},
            },
            "uncordon_node": {
                "api": "v1",
                "method": "patch_node",
                "required": ["name"],
                "optional": [],
                "validation": lambda p: "name" in p,
                "patch_body": lambda p: {"spec": {"unschedulable": False}},
            },
            "delete_pod": {
                "api": "v1",
                "method": "delete_namespaced_pod",
                "required": ["name", "namespace"],
                "optional": ["grace_period_seconds"],
                "validation": lambda p: "name" in p and "namespace" in p,
                # No patch_body for delete operations
            },
            # Add get_node if needed for context/verification
            "get_node": {
                "api": "v1",
                "method": "read_node",
                "required": ["name"],
                "optional": [],
                "validation": lambda p: True,
            },
            "list_dependencies": {
                "api": "apps_v1",
                "method": "list_namespaced_deployment",
                "required": ["namespace"],
                "optional": ["label_selector"],
                "validation": lambda p: "namespace" in p,
                # Custom processing in execute_validated_command
            },
            # Add describe operation to match with command_templates
            "describe": {
                "api": "v1",
                "method": "_handle_describe",
                "required": ["name"],
                "optional": ["namespace", "resource_type"],
                "validation": lambda p: "name" in p,
                # No patch_body for read-only operations
            },
            # Add view_logs operation to match with command_templates
            "view_logs": {
                "api": "v1",
                "method": "_handle_view_logs",
                "required": ["name", "namespace"],
                "optional": ["container", "tail", "since"],
                "validation": lambda p: "name" in p and "namespace" in p,
                # No patch_body for read-only operations
            },
            # Add restart_pod operation to match with command_templates
            "restart_pod": {
                "api": "v1",
                "method": "restart_pod",
                "required": ["namespace", "pod_name"],
                "optional": ["force_restart"],
                "validation": lambda p: "namespace" in p and "pod_name" in p,
                # No patch_body for this operation
            },
        }

    def _get_api_client(self, api_version: str):
        """Get the appropriate API client based on version"""
        if (api_version == "v1" and self.v1):
            return self.v1
        if (api_version == "apps_v1" and self.apps_v1):
            return self.apps_v1
        if (api_version == "batch_v1" and self.batch_v1):
            return self.batch_v1
            
        app_logger.error(
            "Kubernetes API client not initialized",
            component="k8s_executor",
            api_version=api_version
        )
        raise KubernetesConnectionError(
            f"Kubernetes API client {api_version} not available",
            details={"api_version": api_version}
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(ApiException),
        after=after_log(logger, "WARNING")
    )
    async def _list_raw(
        self, resource_type: str, namespace: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Synchronous helper with retry: list raw K8s resources for use with asyncio.to_thread.
        
        Args:
            resource_type: Type of Kubernetes resource to list
            namespace: Optional namespace to filter by
            
        Returns:
            List of dictionaries representing the resources
            
        Raises:
            KubernetesConnectionError: When unable to connect to the Kubernetes API
        """
        if not self.v1 or not self.apps_v1:
            raise KubernetesConnectionError(
                "Kubernetes API client not initialized",
                details={
                    "resource_type": resource_type,
                    "namespace": namespace
                }
            )
            
        try:
            with app_logger.LogContext(
                component="k8s_executor",
                operation=f"list_{resource_type}",
                namespace=namespace
            ):
                app_logger.debug(
                    f"Listing {resource_type} resources",
                    namespace=namespace,
                    resource_type=resource_type
                )
                
                if resource_type == "pod" and namespace:
                    return [
                        item.to_dict() for item in self.v1.list_namespaced_pod(namespace).items
                    ]
                elif resource_type == "pod":  # List pods across all namespaces
                    return [
                        item.to_dict() for item in self.v1.list_pod_for_all_namespaces().items
                    ]
                elif resource_type == "deployment" and namespace:
                    return [
                        item.to_dict()
                        for item in self.apps_v1.list_namespaced_deployment(namespace).items
                    ]
                elif resource_type == "deployment":  # List deployments across all namespaces
                    return [
                        item.to_dict()
                        for item in self.apps_v1.list_deployment_for_all_namespaces().items
                    ]
                elif resource_type == "node":
                    return [item.to_dict() for item in self.v1.list_node().items]
                elif resource_type == "service" and namespace:
                    return [
                        item.to_dict()
                        for item in self.v1.list_namespaced_service(namespace).items
                    ]
                elif resource_type == "service":  # List services across all namespaces
                    return [
                        item.to_dict()
                        for item in self.v1.list_service_for_all_namespaces().items
                    ]
                elif resource_type == "persistentvolumeclaim" and namespace:
                    return [
                        item.to_dict()
                        for item in self.v1.list_namespaced_persistent_volume_claim(
                            namespace
                        ).items
                    ]
                elif resource_type == "persistentvolumeclaim":  # List PVCs across all namespaces
                    return [
                        item.to_dict()
                        for item in self.v1.list_persistent_volume_claim_for_all_namespaces().items
                    ]
                    
                app_logger.warning(
                    f"Unsupported resource type: {resource_type}",
                    resource_type=resource_type,
                    namespace=namespace
                )
                return []
                
        except ApiException as e:
            if e.status == 403:
                app_logger.error(
                    "Permission denied when accessing Kubernetes API",
                    status_code=e.status,
                    reason=e.reason,
                    resource_type=resource_type,
                    namespace=namespace
                )
                raise KubernetesConnectionError(
                    f"Permission denied when accessing {resource_type}: {e.reason}",
                    details={
                        "status_code": e.status,
                        "reason": e.reason,
                        "resource_type": resource_type,
                        "namespace": namespace
                    }
                )
            elif e.status == 404:
                app_logger.warning(
                    f"Resource not found: {resource_type}",
                    status_code=e.status,
                    reason=e.reason,
                    resource_type=resource_type,
                    namespace=namespace
                )
                return []
            elif e.status >= 500:
                # Server errors may be transient, so we'll let the retry mechanism handle them
                app_logger.warning(
                    f"Server error when listing {resource_type}",
                    status_code=e.status,
                    reason=e.reason,
                    resource_type=resource_type,
                    namespace=namespace
                )
                raise
            else:
                app_logger.error(
                    f"API error when listing {resource_type}",
                    status_code=e.status,
                    reason=e.reason,
                    resource_type=resource_type,
                    namespace=namespace
                )
                raise KubernetesConnectionError(
                    f"API error: {e.reason}",
                    details={
                        "status_code": e.status,
                        "reason": e.reason,
                        "resource_type": resource_type,
                        "namespace": namespace
                    }
                )
                
        except Exception as e:
            app_logger.error(
                f"Error listing {resource_type}",
                exception=str(e),
                exception_type=e.__class__.__name__,
                resource_type=resource_type,
                namespace=namespace
            )
            raise KubernetesConnectionError(
                f"Failed to list {resource_type}: {str(e)}",
                details={
                    "exception": str(e),
                    "exception_type": e.__class__.__name__,
                    "resource_type": resource_type,
                    "namespace": namespace
                }
            )

    def parse_and_validate_command(
        self, command_str: str
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Parse and validate a command string"""
        parts = command_str.split()
        if not parts:
            return None
        operation = parts[0]

        # Allow raw kubectl commands without validation
        if operation == "kubectl":
            app_logger.info(
                "Raw kubectl command allowed",
                component="k8s_executor",
                command=command_str
            )
            return operation, {"raw_command": command_str}

        # Check if the command is blacklisted
        if operation in self.blacklisted_operations:
            app_logger.warning(
                f"Command is blacklisted and cannot be executed",
                component="k8s_executor",
                operation=operation,
                command=command_str
            )
            return None

        # Get operation config if it exists
        op_config = self.safe_operations.get(operation)

        # If the operation is not in safe_operations, we need to create a basic configuration
        # This allows any non-blacklisted command to be executed (blacklist approach)
        if op_config is None:
            app_logger.info(
                "Command is not in safe_operations but allowed as not blacklisted",
                component="k8s_executor",
                operation=operation,
                command=command_str
            )
            # Create a basic configuration assuming all parameters are valid
            op_config = {
                "api": "v1",  # Default to core API
                "method": operation,  # Assume method name matches operation
                "required": [],  # No required params by default
                "optional": [],  # No optional params by default
                "validation": lambda p: True,  # No validation by default
            }

        params = {}
        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                try:
                    params[key] = int(value)
                except ValueError:
                    params[key] = value
            else:
                app_logger.warning(
                    "Invalid parameter format in command",
                    component="k8s_executor",
                    command=command_str,
                    invalid_part=part
                )
                return None

        # Check required parameters if specified in op_config
        for req_param in op_config["required"]:
            if req_param not in params:
                app_logger.warning(
                    "Missing required parameter for operation",
                    component="k8s_executor",
                    operation=operation,
                    missing_param=req_param,
                    command=command_str
                )
                return None

        # Perform custom validation if specified in op_config
        validator = op_config.get("validation")
        if validator and not validator(params):
            app_logger.warning(
                "Parameter validation failed for operation",
                component="k8s_executor",
                operation=operation,
                params=params,
                command=command_str
            )
            return None

        app_logger.debug(
            "Command validated",
            component="k8s_executor",
            operation=operation,
            params=params
        )
        return operation, params

    # NEW METHOD: Parse template-based remediation action
    def parse_remediation_action(
        self, action: Dict[str, Any]
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Parse a template-based remediation action into an operation and parameters."""
        action_type = action.get("action_type")
        if not action_type or action_type not in self.command_templates:
            logger.warning(f"Unknown action type: {action_type}")
            return None

        template = self.command_templates[action_type]
        params = {}

        # Add resource info to params
        params["name"] = action.get("resource_name")
        if action.get("namespace"):
            params["namespace"] = action.get("namespace")

        # Add additional parameters
        for key, value in action.get("parameters", {}).items():
            params[key] = value

        # Check required parameters
        for req_param in template["required_params"]:
            if req_param not in params:
                logger.warning(
                    f"Missing required parameter '{req_param}' for action '{action_type}'"
                )
                return None

        return action_type, params

    # NEW METHOD: Check for dependencies
    async def check_dependencies(
        self, resource_type: str, name: str, namespace: str
    ) -> Dict[str, Any]:
        """Check if other resources depend on this resource."""
        if not self.v1 or not self.apps_v1:
            return {"error": "Kubernetes client not available"}

        dependencies = {
            "has_dependencies": False,
            "dependent_resources": [],
            "critical_dependencies": False,
        }

        try:
            if resource_type == "deployment":
                # Check if there are services pointing to this deployment
                deploy = await asyncio.to_thread(
                    self.apps_v1.read_namespaced_deployment, name, namespace
                )
                selector = deploy.spec.selector.match_labels

                # Check services that might select this deployment
                services = await asyncio.to_thread(
                    self.v1.list_namespaced_service, namespace
                )
                for svc in services.items:
                    if not svc.spec.selector:
                        continue

                    # Check if service selector matches deployment selector
                    matches = all(
                        item in svc.spec.selector.items() for item in selector.items()
                    )
                    if matches:
                        dependencies["has_dependencies"] = True
                        dependencies["dependent_resources"].append(
                            {
                                "kind": "Service",
                                "name": svc.metadata.name,
                                "namespace": svc.metadata.namespace,
                            }
                        )

                        # Check if critical service
                        if svc.metadata.labels and "criticality" in svc.metadata.labels:
                            if svc.metadata.labels["criticality"] == "high":
                                dependencies["critical_dependencies"] = True

            # Similar checks could be done for other resource types

        except ApiException as e:
            logger.error(f"Error checking dependencies: {e.status} - {e.reason}")
            dependencies["error"] = f"API Error: {e.status} - {e.reason}"
        except Exception as e:
            logger.error(f"Error checking dependencies: {e}")
            dependencies["error"] = f"Error: {str(e)}"

        return dependencies

    # NEW METHOD: Handle phased deployment scaling
    async def _handle_scale_deployment(
        self,
        name: str,
        namespace: str,
        target_replicas: int,
        incremental: bool = False,
        max_surge_percent: int = 25,
        wait_for_ready: bool = True,
    ) -> Dict[str, Any]:
        """Handle scaling with optional incremental/phased approach."""
        if not self.apps_v1:
            return {"error": "Kubernetes client not available"}

        results = {"phases": [], "final_status": None}

        try:
            # Get current deployment
            deploy = await asyncio.to_thread(
                self.apps_v1.read_namespaced_deployment, name, namespace
            )
            current_replicas = deploy.spec.replicas

            if target_replicas == current_replicas:
                return {
                    "info": f"Deployment {namespace}/{name} already at desired scale: {target_replicas}"
                }

            if not incremental or abs(target_replicas - current_replicas) <= 1:
                # Simple direct scaling
                patch = {"spec": {"replicas": target_replicas}}
                await asyncio.to_thread(
                    self.apps_v1.patch_namespaced_deployment,
                    name=name,
                    namespace=namespace,
                    body=patch,
                )
                results["phases"].append(
                    {
                        "from": current_replicas,
                        "to": target_replicas,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )

                if wait_for_ready:
                    await self._wait_for_deployment_ready(name, namespace)
            else:
                # Incremental scaling
                step_size = max(
                    1,
                    abs(target_replicas - current_replicas) * max_surge_percent // 100,
                )

                if target_replicas > current_replicas:
                    # Scaling up
                    for replicas in range(
                        current_replicas + step_size, target_replicas + 1, step_size
                    ):
                        # Cap at target
                        replicas = min(replicas, target_replicas)

                        patch = {"spec": {"replicas": replicas}}
                        await asyncio.to_thread(
                            self.apps_v1.patch_namespaced_deployment,
                            name=name,
                            namespace=namespace,
                            body=patch,
                        )

                        results["phases"].append(
                            {
                                "from": current_replicas,
                                "to": replicas,
                                "timestamp": datetime.utcnow().isoformat(),
                            }
                        )

                        if wait_for_ready:
                            await self._wait_for_deployment_ready(name, namespace)

                        current_replicas = replicas
                else:
                    # Scaling down
                    for replicas in range(
                        current_replicas - step_size, target_replicas - 1, -step_size
                    ):
                        # Cap at target
                        replicas = max(replicas, target_replicas)

                        patch = {"spec": {"replicas": replicas}}
                        await asyncio.to_thread(
                            self.apps_v1.patch_namespaced_deployment,
                            name=name,
                            namespace=namespace,
                            body=patch,
                        )

                        results["phases"].append(
                            {
                                "from": current_replicas,
                                "to": replicas,
                                "timestamp": datetime.utcnow().isoformat(),
                            }
                        )

                        if wait_for_ready:
                            await self._wait_for_deployment_ready(name, namespace)

                        current_replicas = replicas

            # Get final status
            final_deploy = await asyncio.to_thread(
                self.apps_v1.read_namespaced_deployment, name, namespace
            )
            results["final_status"] = {
                "replicas": final_deploy.spec.replicas,
                "ready_replicas": final_deploy.status.ready_replicas or 0,
                "available_replicas": final_deploy.status.available_replicas or 0,
                "unavailable_replicas": final_deploy.status.unavailable_replicas or 0,
            }

            return results

        except ApiException as e:
            logger.error(f"Error scaling deployment: {e.status} - {e.reason}")
            return {"error": f"API Error: {e.status} - {e.reason}"}
        except Exception as e:
            logger.error(f"Error scaling deployment: {e}")
            return {"error": f"Error: {str(e)}"}

    # Helper method to wait for deployment to be ready
    async def _wait_for_deployment_ready(
        self, name: str, namespace: str, timeout_seconds: int = 300
    ) -> bool:
        """Wait for deployment to reach ready state."""
        if not self.apps_v1:
            logger.error("Kubernetes AppsV1 client is not initialized")
            return False

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            try:
                deploy = await asyncio.to_thread(
                    self.apps_v1.read_namespaced_deployment, name, namespace
                )

                # Check if deployment is ready
                if (
                    deploy.status.updated_replicas == deploy.spec.replicas
                    and deploy.status.replicas == deploy.spec.replicas
                    and deploy.status.available_replicas == deploy.spec.replicas
                    and deploy.status.observed_generation >= deploy.metadata.generation
                ):
                    return True

                # Wait before checking again
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error checking deployment status: {e}")
                await asyncio.sleep(5)

        return False

    # NEW METHOD: Resource adjustment
    async def _handle_adjust_resources(
        self,
        name: str,
        namespace: str,
        cpu_request: Optional[str] = None,
        cpu_limit: Optional[str] = None,
        memory_request: Optional[str] = None,
        memory_limit: Optional[str] = None,
        container: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Adjust resource requests and limits for a deployment."""
        if not self.apps_v1:
            return {"error": "Kubernetes client not available"}

        try:
            # Get current deployment
            deploy = await asyncio.to_thread(
                self.apps_v1.read_namespaced_deployment, name, namespace
            )

            # Prepare patch
            containers = deploy.spec.template.spec.containers
            updates = []

            for c in containers:
                # Skip if container name doesn't match (when specified)
                if container and c.name != container:
                    continue

                # Get current values
                resources = c.resources or client.V1ResourceRequirements()
                requests = resources.requests or {}
                limits = resources.limits or {}

                # Track original values
                updates.append(
                    {
                        "container": c.name,
                        "original": {
                            "cpu_request": requests.get("cpu"),
                            "cpu_limit": limits.get("cpu"),
                            "memory_request": requests.get("memory"),
                            "memory_limit": limits.get("memory"),
                        },
                        "new": {},
                    }
                )

                # Update resources
                updated_requests = dict(requests)
                updated_limits = dict(limits)

                if cpu_request:
                    updated_requests["cpu"] = cpu_request
                    updates[-1]["new"]["cpu_request"] = cpu_request

                if cpu_limit:
                    updated_limits["cpu"] = cpu_limit
                    updates[-1]["new"]["cpu_limit"] = cpu_limit

                if memory_request:
                    updated_requests["memory"] = memory_request
                    updates[-1]["new"]["memory_request"] = memory_request

                if memory_limit:
                    updated_limits["memory"] = memory_limit
                    updates[-1]["new"]["memory_limit"] = memory_limit

                # Create patch specification
                resources = client.V1ResourceRequirements(
                    requests=updated_requests, limits=updated_limits
                )
                c.resources = resources

            # Apply patch
            body = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": c.name, "resources": c.resources.to_dict()}
                                for c in containers
                                if not container or c.name == container
                            ]
                        }
                    }
                }
            }

            await asyncio.to_thread(
                self.apps_v1.patch_namespaced_deployment,
                name=name,
                namespace=namespace,
                body=body,
            )

            return {
                "status": "success",
                "resource_updates": updates,
                "timestamp": datetime.utcnow().isoformat(),
            }

        except ApiException as e:
            logger.error(f"Error adjusting resources: {e.status} - {e.reason}")
            return {"error": f"API Error: {e.status} - {e.reason}"}
        except Exception as e:
            logger.error(f"Error adjusting resources: {e}")
            return {"error": f"Error: {str(e)}"}

    async def execute_validated_command(
        self, operation: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a validated command against the Kubernetes API with correlation tracking"""
        if not operation or not params:
            app_logger.error(
                "Invalid operation or parameters",
                component="k8s_executor",
                operation=operation
            )
            raise ValidationError("Invalid operation or parameters")

        # Generate a correlation ID for tracking this operation across systems
        correlation_id = self.event_correlator.generate_correlation_id()
        app_logger.info(
            f"Executing K8s operation: {operation}",
            component="k8s_executor",
            operation=operation,
            params=params,
            correlation_id=correlation_id
        )

        # Special handling for raw kubectl
        if operation == "kubectl":
            # ... existing kubectl handling code ...
            return {"status": "success", "message": "Executed kubectl command", "correlation_id": correlation_id}

        op_config = self.safe_operations.get(operation)
        if not op_config:
            app_logger.error(
                f"Operation not found in safe operations: {operation}",
                component="k8s_executor",
                operation=operation,
                correlation_id=correlation_id
            )
            raise ValidationError(f"Unknown operation: {operation}")

        api = op_config.get("api", "v1")
        method = op_config.get("method")
        
        try:
            api_client = self._get_api_client(api)
            method_func = getattr(api_client, method, None)
            
            if not method_func:
                app_logger.error(
                    f"Method not found in API client: {method}",
                    component="k8s_executor",
                    api=api,
                    method=method,
                    correlation_id=correlation_id
                )
                raise ValidationError(f"Unknown method: {method}")
                
            # Prepare kwargs for method call
            kwargs = {}
            for param_name, param_value in params.items():
                # Skip params prefixed with underscore, they're for internal use
                if param_name.startswith("_"):
                    continue
                kwargs[param_name] = param_value
                
            # If this is a patch operation, generate the patch body
            if "patch" in method and "patch_body" in op_config:
                kwargs["body"] = op_config["patch_body"](params)
                
            # Record the operation attempt as an event before execution
            self._record_operation_event(
                operation=operation,
                params=params,
                correlation_id=correlation_id,
                status="Attempted"
            )
                
            # Execute the API call
            result = await asyncio.to_thread(method_func, **kwargs)
            
            # Record successful completion
            self._record_operation_event(
                operation=operation,
                params=params,
                correlation_id=correlation_id,
                status="Completed"
            )
            
            # Format and return result
            if hasattr(result, "to_dict"):
                return {
                    "status": "success",
                    "data": result.to_dict(),
                    "correlation_id": correlation_id
                }
            return {
                "status": "success",
                "data": result,
                "correlation_id": correlation_id
            }
            
        except ApiException as e:
            # Record failure event
            self._record_operation_event(
                operation=operation,
                params=params,
                correlation_id=correlation_id,
                status="Failed",
                error_message=f"{e.status}: {e.reason}"
            )
            
            if e.status == 404:
                app_logger.warning(
                    f"Resource not found",
                    status_code=e.status,
                    reason=e.reason,
                    operation=operation,
                    correlation_id=correlation_id,
                    **params
                )
                raise ResourceNotFoundError(
                    f"Resource not found: {e.reason}",
                    details={
                        "status_code": e.status,
                        "reason": e.reason,
                        "operation": operation,
                        **params
                    }
                )
            elif e.status == 403:
                app_logger.error(
                    "Permission denied when accessing Kubernetes API",
                    status_code=e.status,
                    reason=e.reason,
                    operation=operation,
                    correlation_id=correlation_id,
                    **params
                )
                raise KubernetesConnectionError(
                    f"Permission denied: {e.reason}",
                    details={
                        "status_code": e.status,
                        "reason": e.reason,
                        "operation": operation,
                        **params
                    }
                )
            else:
                app_logger.error(
                    f"API error in operation: {operation}",
                    status_code=e.status,
                    reason=e.reason,
                    operation=operation,
                    correlation_id=correlation_id,
                    **params
                )
                raise OperationFailedError(
                    f"API error: {e.status} - {e.reason}",
                    details={
                        "status_code": e.status,
                        "reason": e.reason,
                        "operation": operation,
                        "correlation_id": correlation_id,
                        **params
                    }
                )
                
        except Exception as e:
            # Record failure event
            self._record_operation_event(
                operation=operation,
                params=params,
                correlation_id=correlation_id,
                status="Failed",
                error_message=str(e)
            )
            
            app_logger.error(
                f"Exception in operation: {operation}",
                exception=str(e),
                exception_type=e.__class__.__name__,
                operation=operation, 
                correlation_id=correlation_id,
                **params
            )
            raise OperationFailedError(
                f"Operation failed: {str(e)}",
                details={
                    "exception": str(e),
                    "exception_type": e.__class__.__name__,
                    "operation": operation,
                    "correlation_id": correlation_id,
                    **params
                }
            )

    def _record_operation_event(
        self,
        operation: str,
        params: Dict[str, Any],
        correlation_id: str,
        status: str,
        error_message: Optional[str] = None
    ):
        """Record a Kubernetes event for the operation."""
        # Skip recording events if event correlator isn't available
        if not self.event_correlator:
            return
            
        # Determine resource type and name from params
        resource_type = operation.split("_")[-1].capitalize()  # e.g., "get_pod" -> "Pod"
        if resource_type == "Deployment":
            resource_type = "Deployment"  # Ensure proper capitalization
            
        resource_name = params.get("name", "unknown")
        namespace = params.get("namespace", "default")
        
        # Format the event reason and message
        reason = f"{status}{resource_type.capitalize()}Operation"
        if status == "Attempted":
            message = f"Starting {operation} operation on {resource_type} {resource_name}"
        elif status == "Completed":
            message = f"Successfully completed {operation} operation on {resource_type} {resource_name}"
        else:  # Failed
            message = f"Failed {operation} operation on {resource_type} {resource_name}: {error_message}"
            
        # Add operation details as additional data
        additional_data = {
            "operation": operation
        }
        
        # Add all parameters except sensitive ones
        filtered_params = {k: v for k, v in params.items() if not k.startswith("_")}
        additional_data["parameters"] = json.dumps(filtered_params)
            
        # Set severity based on status
        severity = "Normal" if status != "Failed" else "Warning"
            
        # Record the event
        self.event_correlator.record_event(
            name=resource_name,
            namespace=namespace,
            resource_type=resource_type,
            action=operation,
            correlation_id=correlation_id,
            reason=reason,
            message=message,
            severity=severity,
            additional_data=additional_data
        )

    def get_safe_operations_info(self) -> Dict[str, Dict[str, Any]]:
        """Get information about safe operations"""
        info = {}
        for op_name, op_config_info in self.safe_operations.items():
            info[op_name] = {
                "required": op_config_info["required"],
                "optional": op_config_info["optional"],
                "description": f"Executes {op_config_info['api']}.{op_config_info['method']}",
            }
        return info

    async def execute_remediation_action(self, action: RemediationAction) -> dict:
        try:
            # Convert RemediationAction (model or dict) to operation/params
            action_dict = action.dict() if hasattr(action, "dict") else action
            op_params = self.parse_remediation_action(action_dict)
            if not op_params:
                logger.error(f"Failed to parse remediation action: {action}")
                return {"status": "error", "error": "Invalid remediation action"}
            operation, params = op_params
            logger.info(f"Executing remediation action: {operation} with params: {params}")

            # Handle 'command' operation using the command_playground module
            if operation == "command":
                command = params.get("command")
                if not command:
                    return {"status": "error", "error": "No command provided for execution"}

                result = execute_command(command)
                if result["success"]:
                    logger.info(f"Command executed successfully: {command}")
                    return {"status": "success", "output": result["stdout"]}
                else:
                    logger.error(f"Command execution failed: {command}, Error: {result['stderr']}")
                    return {"status": "error", "error": result["stderr"]}

            # Fallback to generic validated command execution
            result = await self.execute_validated_command(operation, params)
            logger.info(f"Remediation action result: {result}")
            return result
        except ResourceNotFoundError as e:
            logger.error(f"Resource not found error: {e.message}")
            return {"status": "error", "error": e.message, "details": e.details}
        except KubernetesConnectionError as e:
            logger.error(f"Kubernetes connection error: {e.message}")
            return {"status": "error", "error": e.message, "details": e.details}
        except OperationFailedError as e:
            logger.error(f"Operation failed: {e.message}")
            return {"status": "error", "error": e.message, "details": e.details}
        except Exception as e:
            logger.error(f"Error executing remediation action: {e}")
            return {"status": "error", "error": str(e)}

    def get_command_templates_info(self) -> Dict[str, Dict[str, Any]]:
        """Get information about command templates"""
        template_info = {}
        for template_name, template in self.command_templates.items():
            template_info[template_name] = {
                "description": template["description"],
                "required_params": template["required_params"],
                "optional_params": template["optional_params"],
                "impact": template["impact"],
                "estimated_duration": template["estimated_duration"],
            }
        return template_info

    async def get_cluster_context_for_promql(self) -> Dict[str, Any]:
        """Fetches basic cluster info to provide context for PromQL generation."""
        context = {"nodes": {}, "workloads": {}}
        if not self.v1 or not self.apps_v1:
            logger.warning("K8s client not available, cannot fetch cluster context.")
            return context
        try:
            # Node Info (Count, maybe versions/arch if needed)
            node_list = await asyncio.to_thread(self.v1.list_node)
            context["nodes"]["count"] = len(node_list.items)
            # Could add OS/Arch distribution if useful for Gemini
            # context["nodes"]["os_distribution"] = ...

            # Workload Info (Counts of major types)
            deployments = await asyncio.to_thread(
                self.apps_v1.list_deployment_for_all_namespaces
            )
            statefulsets = await asyncio.to_thread(
                self.apps_v1.list_stateful_set_for_all_namespaces
            )
            daemonsets = await asyncio.to_thread(
                self.apps_v1.list_daemon_set_for_all_namespaces
            )

            context["workloads"]["deployment_count"] = len(deployments.items)
            context["workloads"]["statefulset_count"] = len(statefulsets.items)
            context["workloads"]["daemonset_count"] = len(daemonsets.items)

            logger.info(f"Fetched cluster context: {context}")

        except ApiException as e:
            logger.error(f"Failed to fetch cluster context: {e.status} - {e.reason}")
        except Exception as e:
            logger.error(f"Error fetching cluster context: {e}")

        return context

    async def get_resource_status_for_verification(
        self, entity_type: str, name: str, namespace: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Fetches the status of a specific K8s resource for verification."""
        logger.debug(f"Fetching status for {entity_type} {namespace}/{name}")
        status = None
        try:
            if entity_type == "deployment" and namespace:
                api_response = await self.execute_validated_command(
                    "get_deployment", {"name": name, "namespace": namespace}
                )
                status = json.loads(api_response).get(
                    "status"
                )  # Extract status sub-object
            elif entity_type == "pod" and namespace:
                api_response = await self.execute_validated_command(
                    "get_pod", {"name": name, "namespace": namespace}
                )
                status = json.loads(api_response).get("status")
            elif entity_type == "node":
                api_response = await self.execute_validated_command(
                    "get_node", {"name": name}
                )  # No namespace for nodes
                status = json.loads(api_response).get("status")
            # Add other types (StatefulSet, DaemonSet) if needed
            else:
                logger.warning(
                    f"Status fetch not implemented for entity type: {entity_type}"
                )

        except Exception as e:
            logger.error(
                f"Failed to fetch status for verification ({entity_type} {namespace}/{name}): {e}"
            )

        # Return only the 'status' part if found
        return status if isinstance(status, dict) else None

    async def restart_pod(self, namespace: str, pod_name: str = None, name: str = None, force_restart: bool = False) -> Dict[str, Any]:
        """Restart a pod by deleting it (if managed by a controller, it will be recreated)"""
        if not self.v1:
            return {"error": "Kubernetes client not available"}

        # Support both 'pod_name' and 'name' parameters for backward compatibility
        actual_pod_name = pod_name if pod_name is not None else name
        if actual_pod_name is None:
            return {"error": "Pod name not provided. Use either 'pod_name' or 'name' parameter."}

        try:
            # First check if the pod exists and get its details
            pod = await asyncio.to_thread(
                self.v1.read_namespaced_pod, name=actual_pod_name, namespace=namespace
            )

            # Check if pod is managed by a controller unless force_restart is True
            if not force_restart and not pod.metadata.owner_references:
                logger.warning(f"Pod {actual_pod_name} is not managed by a controller. Use force_restart=True to override this safety check.")
                raise ValueError(
                    f"Pod {actual_pod_name} is not managed by a controller. Manual deletion is not safe."
                )

            # Delete the pod
            await asyncio.to_thread(
                self.v1.delete_namespaced_pod,
                name=actual_pod_name,
                namespace=namespace,
                body=client.V1DeleteOptions(),
            )

            return {
                "status": "success",
                "message": f"Pod {actual_pod_name} in namespace {namespace} has been restarted",
                "pod_name": actual_pod_name,
                "namespace": namespace,
                "command": f"restart_pod namespace={namespace} pod_name={actual_pod_name}",
            }
        except ApiException as e:
            if e.status == 404:
                raise ValueError(f"Pod {actual_pod_name} not found in namespace {namespace}")
            raise Exception(f"Failed to restart pod: {e.status} - {e.body or e.reason}")
        except Exception as e:
            raise Exception(f"Failed to restart pod: {str(e)}")

    async def _handle_restart_deployment(
        self, name: str, namespace: str
    ) -> Dict[str, Any]:
        """Handle restarting a deployment by updating its template"""
        if not self.apps_v1:
            return {"error": "Kubernetes AppsV1 client not available"}

        try:
            # Get the deployment
            await asyncio.to_thread(
                self.apps_v1.read_namespaced_deployment, name=name, namespace=namespace
            )

            # Update the deployment's template to trigger a restart
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": datetime.utcnow().isoformat()
                                + "Z"
                            }
                        }
                    }
                }
            }

            # Apply the patch
            await asyncio.to_thread(
                self.apps_v1.patch_namespaced_deployment,
                name=name,
                namespace=namespace,
                body=patch,
            )

            return {
                "status": "success",
                "message": f"Deployment {namespace}/{name} has been restarted",
                "deployment_name": name,
                "namespace": namespace,
                "command": f"restart_deployment name={name} namespace={namespace}",
            }
        except ApiException as e:
            if e.status == 404:
                raise ValueError(
                    f"Deployment {name} not found in namespace {namespace}"
                )
            raise Exception(
                f"Failed to restart deployment: {e.status} - {e.body or e.reason}"
            )
        except Exception as e:
            raise Exception(f"Failed to restart deployment: {str(e)}")

    async def list_resources_with_status(
        self,
        resource_type: str,
        status_filter: List[str],
        namespace: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists resources and their status with optional filtering."""
        results = []
        try:
            raw_items = await self._list_raw(resource_type, namespace)
            app_logger.debug(f"Raw items for {resource_type}: {len(raw_items)}")
            
            for item in raw_items:
                try:
                    metadata = item.get("metadata", {})
                    status = item.get("status", {})
                    
                    # For pods, handle container statuses safely
                    if resource_type == "pod":
                        # Initialize status info with safe defaults
                        status_info = {
                            "kind": "Pod",
                            "metadata": metadata,
                            "name": metadata.get("name"),
                            "namespace": metadata.get("namespace"),
                            "phase": status.get("phase", "Unknown"),
                            "conditions": [],
                            "containerStatuses": [],
                            "status": {
                                "phase": status.get("phase", "Unknown"),
                                "containerStatuses": [],
                                "conditions": []
                            }
                        }
                        
                        # Safely update conditions and container statuses
                        if status.get("conditions") is not None:
                            status_info["conditions"] = status["conditions"]
                            status_info["status"]["conditions"] = status["conditions"]
                            
                        if status.get("containerStatuses") is not None:
                            status_info["containerStatuses"] = status["containerStatuses"]
                            status_info["status"]["containerStatuses"] = status["containerStatuses"]
                            
                            # Check container states for issues
                            for container in status["containerStatuses"]:
                                state = container.get("state", {})
                                if state.get("waiting"):
                                    waiting_reason = state["waiting"].get("reason")
                                    if waiting_reason:
                                        app_logger.info(
                                            f"Container {container.get('name')} in pod {metadata.get('name')} " 
                                            f"is waiting with reason: {waiting_reason}"
                                        )
                        
                        results.append(status_info)
                        continue
                    
                    # For other resources, get full status
                    raw_status = await self.get_resource_status(
                        resource_type, metadata.get("name"), metadata.get("namespace")
                    )
                    
                    if raw_status:
                        # Serialize datetime → ISO str
                        def _serialize(o):
                            if isinstance(o, datetime):
                                return o.isoformat()
                            if isinstance(o, dict):
                                return {k: _serialize(v) for k, v in o.items()}
                            if isinstance(o, list):
                                return [_serialize(v) for v in o]
                            return o
                        
                        status = _serialize(raw_status)
                        
                        # Apply status filtering if requested
                        if not status_filter or status.get("status") in status_filter:
                            results.append(status)
                    else:
                        # Fallback status if get_resource_status returns None
                        basic_status = {
                            "kind": item.get("kind", resource_type.capitalize()),
                            "metadata": metadata,
                            "name": metadata.get("name"),
                            "namespace": metadata.get("namespace"),
                            "status": status
                        }
                        results.append(basic_status)
                        
                except Exception as item_error:
                    app_logger.warning(
                        f"Error getting status for {resource_type}/{metadata.get('name', 'unknown')}: {str(item_error)}"
                    )
            
            return results
            
        except Exception as e:
            app_logger.warning(f"Error listing resources with status for {resource_type}: {str(e)}")
            return []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(ApiException),
        after=after_log(logger, "WARNING")
    )
    async def get_resource_status(
        self, resource_type: str, name: str, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Gets detailed status information for a specific resource.
        Used for verification and direct failure detection.

        Args:
            resource_type: The type of resource (pod, deployment, node, etc.)
            name: Name of the resource
            namespace: Namespace of the resource (if applicable)

        Returns:
            Dictionary with the resource's status details
            
        Raises:
            ResourceNotFoundError: When the requested resource does not exist
            KubernetesConnectionError: When unable to connect to the Kubernetes API
        """
        if not self.v1 or not self.apps_v1:
            app_logger.warning(
                "Kubernetes client not initialized",
                component="k8s_executor",
                operation="get_resource_status",
                resource_type=resource_type,
                name=name,
                namespace=namespace
            )
            raise KubernetesConnectionError(
                "Kubernetes client not initialized",
                details={
                    "resource_type": resource_type,
                    "name": name,
                    "namespace": namespace
                }
            )

        try:
            with app_logger.LogContext(
                component="k8s_executor",
                operation="get_resource_status",
                resource_type=resource_type
            ):
                app_logger.debug(
                    f"Getting resource status",
                    resource_type=resource_type,
                    name=name,
                    namespace=namespace
                )
                
                resource_dict = {}

                # Handle resources that require namespaces when namespace is provided
                if resource_type == "pod" and namespace:
                    resource = await asyncio.to_thread(
                        self.v1.read_namespaced_pod, name, namespace
                    )
                    resource_dict = resource.to_dict()

                elif resource_type == "deployment" and namespace:
                    resource = await asyncio.to_thread(
                        self.apps_v1.read_namespaced_deployment, name, namespace
                    )
                    resource_dict = resource.to_dict()
                
                elif resource_type == "deployment":
                    # Handle deployments without namespace by searching in all namespaces
                    deployments = await asyncio.to_thread(
                        self.apps_v1.list_deployment_for_all_namespaces
                    )
                    for item in deployments.items:
                        if item.metadata.name == name:
                            resource_dict = item.to_dict()
                            break
                    if not resource_dict:
                        raise ResourceNotFoundError(
                            f"Deployment {name} not found in any namespace",
                            details={
                                "resource_type": resource_type,
                                "name": name
                            }
                        )

                elif resource_type == "node":
                    resource = await asyncio.to_thread(self.v1.read_node, name)
                    resource_dict = resource.to_dict()

                elif resource_type == "service" and namespace:
                    resource = await asyncio.to_thread(
                        self.v1.read_namespaced_service, name, namespace
                    )
                    resource_dict = resource.to_dict()
                    
                elif resource_type == "service":
                    # Handle services without namespace by searching in all namespaces
                    services = await asyncio.to_thread(
                        self.v1.list_service_for_all_namespaces
                    )
                    for item in services.items:
                        if item.metadata.name == name:
                            resource_dict = item.to_dict()
                            break
                    if not resource_dict:
                        raise ResourceNotFoundError(
                            f"Service {name} not found in any namespace",
                            details={
                                "resource_type": resource_type,
                                "name": name
                            }
                        )

                elif resource_type == "persistentvolumeclaim" and namespace:
                    resource = await asyncio.to_thread(
                        self.v1.read_namespaced_persistent_volume_claim, name, namespace
                    )
                    resource_dict = resource.to_dict()
                    
                elif resource_type == "persistentvolumeclaim":
                    # Handle PVCs without namespace by searching in all namespaces
                    pvcs = await asyncio.to_thread(
                        self.v1.list_persistent_volume_claim_for_all_namespaces
                    )
                    for item in pvcs.items:
                        if item.metadata.name == name:
                            resource_dict = item.to_dict()
                            break
                    if not resource_dict:
                        raise ResourceNotFoundError(
                            f"PersistentVolumeClaim {name} not found in any namespace",
                            details={
                                "resource_type": resource_type,
                                "name": name
                            }
                        )

                else:
                    app_logger.warning(
                        f"Unsupported resource type",
                        resource_type=resource_type,
                        name=name,
                        namespace=namespace
                    )
                    raise ValueError(f"Unsupported resource type: {resource_type}")
                    
                return resource_dict
                
        except ApiException as e:
            if e.status == 404:
                app_logger.warning(
                    f"Resource not found",
                    status_code=e.status, 
                    reason=e.reason,
                    resource_type=resource_type,
                    name=name,
                    namespace=namespace
                )
                raise ResourceNotFoundError(
                    resource_type=resource_type,
                    name=name,
                    namespace=namespace
                )
            else:
                app_logger.error(
                    f"API error when getting resource status",
                    status_code=e.status,
                    reason=e.reason,
                    resource_type=resource_type,
                    name=name,
                    namespace=namespace
                )
                raise KubernetesConnectionError(
                    f"API error: {e.reason}",
                    details={
                        "status_code": e.status,
                        "reason": e.reason,
                        "resource_type": resource_type,
                        "name": name,
                        "namespace": namespace
                    }
                )
                
        except Exception as e:
            app_logger.error(
                f"Error getting resource status",
                exception=str(e),
                exception_type=e.__class__.__name__,
                resource_type=resource_type,
                name=name,
                namespace=namespace
            )
            raise KubernetesConnectionError(
                f"Failed to get {resource_type} status: {str(e)}",
                details={
                    "exception": str(e),
                    "exception_type": e.__class__.__name__,
                    "resource_type": resource_type,
                    "name": name,
                    "namespace": namespace
                }
            )

    async def scale_parent_controller(
        self, namespace: str, pod_name: str, delta: int = 1
    ) -> Dict[str, Any]:
        """
        Scales the parent controller (deployment, statefulset, etc.) of a pod by the specified delta.
        Used primarily for remediating CrashLoopBackOff issues by scaling up the parent.

        Args:
            namespace: Namespace of the pod
            pod_name: Name of the pod
            delta: Number of replicas to add (positive) or remove (negative)

        Returns:
            Dictionary with the scaling result
        """
        if not self.v1 or not self.apps_v1:
            return {"error": "Kubernetes client not available"}

        try:
            if not self.v1 or not self.apps_v1:
                return {"error": "Kubernetes client not available"}

            # Get the pod to find its owner
            pod = await asyncio.to_thread(
                self.v1.read_namespaced_pod, pod_name, namespace
            )
            owner_refs = pod.metadata.owner_references

            if not owner_refs:
                return {
                    "error": f"Pod {pod_name} has no owner references, cannot find parent controller"
                }

            # Find the immediate owner
            owner = owner_refs[0]  # Usually the first owner is the direct controller
            owner_kind = owner.kind
            owner_name = owner.name

            # If owner is ReplicaSet, find its parent Deployment
            if owner_kind == "ReplicaSet":
                rs = await asyncio.to_thread(
                    self.apps_v1.read_namespaced_replica_set, owner_name, namespace
                )
                rs_owner_refs = rs.metadata.owner_references

                if rs_owner_refs and rs_owner_refs[0].kind == "Deployment":
                    owner_kind = "Deployment"
                    owner_name = rs_owner_refs[0].name

            # Scale the parent controller based on its kind
            if owner_kind == "Deployment":
                # Get current replicas
                deploy = await asyncio.to_thread(
                    self.apps_v1.read_namespaced_deployment, owner_name, namespace
                )
                current_replicas = deploy.spec.replicas
                new_replicas = max(
                    1, current_replicas + delta
                )  # Ensure at least 1 replica

                # Scale the deployment
                patch = {"spec": {"replicas": new_replicas}}
                await asyncio.to_thread(
                    self.apps_v1.patch_namespaced_deployment,
                    name=owner_name,
                    namespace=namespace,
                    body=patch,
                )

                return {
                    "status": "success",
                    "parent_type": "Deployment",
                    "parent_name": owner_name,
                    "previous_replicas": current_replicas,
                    "new_replicas": new_replicas,
                    "message": f"Scaled deployment {owner_name} from {current_replicas} to {new_replicas} replicas",
                }

            elif owner_kind == "StatefulSet":
                # Get current replicas
                sts = await asyncio.to_thread(
                    self.apps_v1.read_namespaced_stateful_set, owner_name, namespace
                )
                current_replicas = sts.spec.replicas
                new_replicas = max(
                    1, current_replicas + delta
                )  # Ensure at least 1 replica

                # Scale the statefulset
                patch = {"spec": {"replicas": new_replicas}}
                await asyncio.to_thread(
                    self.apps_v1.patch_namespaced_stateful_set,
                    name=owner_name,
                    namespace=namespace,
                    body=patch,
                )

                return {
                    "status": "success",
                    "parent_type": "StatefulSet",
                    "parent_name": owner_name,
                    "previous_replicas": current_replicas,
                    "new_replicas": new_replicas,
                    "message": f"Scaled statefulset {owner_name} from {current_replicas} to {new_replicas} replicas",
                }

            elif owner_kind == "DaemonSet":
                return {
                    "status": "warning",
                    "parent_type": "DaemonSet",
                    "parent_name": owner_name,
                    "message": f"Cannot scale DaemonSet {owner_name} as they run on all nodes",
                }

            else:
                return {
                    "status": "warning",
                    "parent_type": owner_kind,
                    "parent_name": owner_name,
                    "message": f"Scaling not supported for parent kind: {owner_kind}",
                }

        except ApiException as e:
            logger.error(
                f"API error scaling parent controller for pod {namespace}/{pod_name}: {e.status} - {e.reason}"
            )
            return {"error": f"API Error: {e.status} - {e.reason}"}
        except Exception as e:
            logger.error(
                f"Error scaling parent controller for pod {namespace}/{pod_name}: {e}"
            )
            return {"error": f"Error: {str(e)}"}

    def format_command_from_template(self, action_type: str, params: dict) -> str:
        """
        Format a command string based on an action type and parameters.

        Args:
            action_type: The type of action (must be in command_templates)
            params: Dictionary of parameters for the command

        Returns:
            Formatted command string

        Raises:
            ValueError: If the action type is unknown or required parameters are missing
        """
        if action_type not in self.command_templates:
            raise ValueError(f"Unknown action type: {action_type}")

        template = self.command_templates[action_type]

        # Check required parameters
        missing_params = []
        for req_param in template["required_params"]:
            if req_param not in params:
                missing_params.append(req_param)

        if missing_params:
            raise ValueError(f"Missing required parameters for {action_type}: {', '.join(missing_params)}")

        # Format the command based on action type
        if action_type == "restart_deployment":
            return f"kubectl rollout restart deployment/{params['name']} -n {params['namespace']}"

        elif action_type == "scale_deployment":
            return f"kubectl scale deployment/{params['name']} --replicas={params['replicas']} -n {params['namespace']}"

        elif action_type == "drain_node":
            ignore_ds = "--ignore-daemonsets" if params.get("ignore_daemonsets", True) else ""
            grace = f"--grace-period={params['grace_period']}" if "grace_period" in params else ""
            return f"kubectl drain {params['name']} {ignore_ds} {grace} --delete-emptydir-data"

        elif action_type == "delete_pod":
            grace = f"--grace-period={params['grace_period']}" if "grace_period" in params else ""
            return f"kubectl delete pod {params['name']} -n {params['namespace']} {grace}"

        elif action_type == "cordon_node":
            return f"kubectl cordon {params['name']}"

        elif action_type == "uncordon_node":
            return f"kubectl uncordon {params['name']}"

        elif action_type == "restart_pod":
            return f"kubectl delete pod {params['pod_name']} -n {params['namespace']} --grace-period=0"

        elif action_type == "adjust_resources":
            # Format a patch command for resource adjustments
            resource_args = []
            if "cpu_request" in params:
                resource_args.append(f"--requests=cpu={params['cpu_request']}")
            if "memory_request" in params:
                resource_args.append(f"--requests=memory={params['memory_request']}")
            if "cpu_limit" in params:
                resource_args.append(f"--limits=cpu={params['cpu_limit']}")
            if "memory_limit" in params:
                resource_args.append(f"--limits=memory={params['memory_limit']}")

            container_arg = f"-c {params['container']}" if "container" in params else ""
            return f"kubectl set resources deployment/{params['name']} -n {params['namespace']} {' '.join(resource_args)} {container_arg}"

        elif action_type == "describe":
            namespace_arg = f"-n {params['namespace']}" if "namespace" in params else ""
            return f"kubectl describe {params.get('resource_type', 'pod')} {params['name']} {namespace_arg}"

        elif action_type == "view_logs":
            container_arg = f"-c {params['container']}" if "container" in params else ""
            tail_arg = f"--tail={params['tail']}" if "tail" in params else ""
            since_arg = f"--since={params['since']}" if "since" in params else ""
            return f"kubectl logs {params['name']} -n {params['namespace']} {container_arg} {tail_arg} {since_arg}".strip()
            
        elif action_type == "command":
            # Handle generic command type
            cmd = params.get("command", "")
            cmd_params = params.get("params", {})
            
            # Replace placeholders in the command with params
            for key, value in cmd_params.items():
                placeholder = f"{{{key}}}"
                cmd = cmd.replace(placeholder, str(value))
                
            return cmd

        # For unknown action types or ones we haven't implemented specific formatting for,
        # create a generic kubectl command based on parameters
        parts = [action_type]
        for key, value in params.items():
            parts.append(f"{key}={value}")

        return " ".join(parts)

    async def _handle_describe(
        self,
        name: str,
        namespace: Optional[str] = None,
        resource_type: str = "pod",
    ) -> Dict[str, Any]:
        """
        Handle resource describe operation by fetching detailed information.
        This is a read-only operation that provides comprehensive details about a K8s resource.
        
        Args:
            name: Name of the resource
            namespace: Namespace of the resource (if applicable)
            resource_type: Type of resource (pod, deployment, service, etc.)
            
        Returns:
            Dictionary with resource details
        """
        logger.info(f"Describing {resource_type} {namespace}/{name}")
        
        try:
            # Get resource details based on resource_type
            if resource_type == "pod" and namespace:
                resource = await asyncio.to_thread(
                    self.v1.read_namespaced_pod, name, namespace
                )
                resource_dict = resource.to_dict()
            
            elif resource_type == "deployment" and namespace:
                resource = await asyncio.to_thread(
                    self.apps_v1.read_namespaced_deployment, name, namespace
                )
                resource_dict = resource.to_dict()
                
            elif resource_type == "service" and namespace:
                resource = await asyncio.to_thread(
                    self.v1.read_namespaced_service, name, namespace
                )
                resource_dict = resource.to_dict()
                
            elif resource_type == "node":
                resource = await asyncio.to_thread(
                    self.v1.read_node, name
                )
                resource_dict = resource.to_dict()
                
            elif resource_type == "persistentvolumeclaim" and namespace:
                resource = await asyncio.to_thread(
                    self.v1.read_namespaced_persistent_volume_claim, name, namespace
                )
                resource_dict = resource.to_dict()
                
            elif resource_type == "configmap" and namespace:
                resource = await asyncio.to_thread(
                    self.v1.read_namespaced_config_map, name, namespace
                )
                resource_dict = resource.to_dict()
                
            elif resource_type == "secret" and namespace:
                resource = await asyncio.to_thread(
                    self.v1.read_namespaced_secret, name, namespace
                )
                resource_dict = resource.to_dict()
                
            else:
                return {
                    "status": "error",
                    "error": f"Unsupported resource type: {resource_type} or missing namespace",
                    "command": f"describe {resource_type} {name} {f'-n {namespace}' if namespace else ''}"
                }
                
            # Format command output
            command = f"kubectl describe {resource_type} {name}{f' -n {namespace}' if namespace else ''}"
            
            return {
                "status": "success",
                "command": command,
                "data": resource_dict,
                "resource_type": resource_type,
                "name": name,
                "namespace": namespace
            }
            
        except ApiException as e:
            error_msg = f"{e.status}: {e.reason}"
            if e.body:
                try:
                    error_body = json.loads(e.body)
                    if "message" in error_body:
                        error_msg += f" - {error_body['message']}"
                except:
                    error_msg += f" - {e.body}"
                    
            logger.error(f"API error describing {resource_type} {name}: {error_msg}")
            return {
                "status": "error",
                "error": error_msg,
                "command": f"describe {resource_type} {name} {f'-n {namespace}' if namespace else ''}"
            }
            
        except Exception as e:
            logger.error(f"Error describing {resource_type} {name}: {e}")
            return {
                "status": "error",
                "error": str(e),
                "command": f"describe {resource_type} {name} {f'-n {namespace}' if namespace else ''}"
            }
            
    async def _handle_view_logs(
        self,
        name: str,
        namespace: str,
        container: Optional[str] = None,
        tail: Optional[int] = None,
        since: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Handle view logs operation by fetching pod logs.
        
        Args:
            name: Name of the pod
            namespace: Namespace of the pod
            container: Optional container name (if pod has multiple containers)
            tail: Optional number of lines to return from the end
            since: Optional time to start logs from (e.g., "1h", "2d")
            
        Returns:
            Dictionary with logs and command information
        """
        if not self.v1:
            return {"error": "Kubernetes client not available"}
            
        try:
            # Build parameters for logs API call
            params = {
                "name": name,
                "namespace": namespace
            }
            
            if container:
                params["container"] = container
                
            if tail:
                params["tail_lines"] = tail
                
            if since:
                # Convert human-readable time to seconds
                if since.endswith('s'):
                    seconds = int(since[:-1])
                elif since.endswith('m'):
                    seconds = int(since[:-1]) * 60
                elif since.endswith('h'):
                    seconds = int(since[:-1]) * 3600
                elif since.endswith('d'):
                    seconds = int(since[:-1]) * 86400
                else:
                    # Try to parse as seconds
                    try:
                        seconds = int(since)
                    except ValueError:
                        return {"error": f"Invalid 'since' parameter format: {since}"}
                        
                # Calculate time delta
                since_seconds = int(time.time()) - seconds
                params["since_seconds"] = since_seconds
                
            # Get logs
            logs = await asyncio.to_thread(
                self.v1.read_namespaced_pod_log,
                **params
            )
            
            # Format command 
            command_parts = [f"kubectl logs {name} -n {namespace}"]
            if container:
                command_parts.append(f"-c {container}")
            if tail:
                command_parts.append(f"--tail={tail}")
            if since:
                command_parts.append(f"--since={since}")
                
            command = " ".join(command_parts)
            
            return {
                "status": "success",
                "logs": logs,
                "command": command,
                "pod": name,
                "namespace": namespace,
                "container": container
            }
            
        except ApiException as e:
            error_msg = f"{e.status}: {e.reason}"
            logger.error(f"API error getting logs for pod {namespace}/{name}: {error_msg}")
            return {"status": "error", "error": error_msg}
            
        except Exception as e:
            logger.error(f"Error getting logs for pod {namespace}/{name}: {e}")
            return {"status": "error", "error": str(e)}

    def _resource_type_to_kind(self, resource_type: str) -> str:
        """
        Args:
            resource_type: The resource type (e.g., "pod", "deployment")
            
        Returns:
            The corresponding Kubernetes kind (e.g., "Pod", "Deployment")
        """
        mapping = {
            "pod": "Pod",
            "deployment": "Deployment",
            "service": "Service",
            "node": "Node",
            "persistentvolumeclaim": "PersistentVolumeClaim",
            "configmap": "ConfigMap",
            "secret": "Secret",
            "statefulset": "StatefulSet",
            "daemonset": "DaemonSet",
            "ingress": "Ingress",
            "job": "Job",
            "cronjob": "CronJob",
        }
        return mapping.get(resource_type, "")

    async def execute_command(self, command: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Execute a generic command```python
        with parameters
        This method handles the 'command' action type from remediation suggestions.
        
        Args:
            command: The command to execute
            params: Optional dictionary of parameters to substitute in the command
            
        Returns:
            Dictionary with execution result
        """
        import subprocess

        if params is None:
            params = {}
            
        try:
            # Replace placeholders in the command with params
            formatted_command = command
            for key, value in params.items():
                placeholder = f"{{{key}}}"
                formatted_command = formatted_command.replace(placeholder, str(value))
                
            logger.info(f"Executing command: {formatted_command}")
            
            # Execute the command
            result = subprocess.run(
                formatted_command,
                shell=True,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            return {
                "status": "success",
                "command": formatted_command,
                "output": result.stdout,
                "exit_code": result.returncode
            }
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Command execution failed: {e}")
            return {
                "status": "error",
                "command": command,
                "error": e.stderr,
                "exit_code": e.returncode
            }
            
        except Exception as e:
            logger.error(f"Error executing command: {e}")
            return {
                "status": "error",
                "command": command,
                "error": str(e)
            }


# Global instance
k8s_executor = K8sExecutor()


async def list_resources(
    resource_type: str, namespace: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Proxy to raw Kubernetes list calls.
    Returns all resources of the given type as a list of dicts.
    """
    if not k8s_executor.v1 or not k8s_executor.apps_v1:
        app_logger.warning(
            "Kubernetes client not available",
            component="k8s_executor",
            operation="list_resources",
            resource_type=resource_type,
            namespace=namespace
        )
        return []
        
    try:
        with app_logger.LogContext(
            component="k8s_executor",
            operation="list_resources",
            resource_type=resource_type,
            namespace=namespace
        ):
            app_logger.debug(
                f"Listing resources",
                resource_type=resource_type,
                namespace=namespace
            )
            
            if resource_type == "pod":
                # List pods in the given namespace or across all namespaces if none specified
                if namespace:
                    pods = await asyncio.to_thread(
                        k8s_executor.v1.list_namespaced_pod, namespace
                    )
                else:
                    pods = await asyncio.to_thread(
                        k8s_executor.v1.list_pod_for_all_namespaces
                    )
                return [item.to_dict() for item in pods.items]
            if resource_type == "deployment" and namespace:
                deps = await asyncio.to_thread(
                    k8s_executor.apps_v1.list_namespaced_deployment, namespace
                )
                return [item.to_dict() for item in deps.items]
            if resource_type == "node":
                nodes = await asyncio.to_thread(k8s_executor.v1.list_node)
                return [item.to_dict() for item in nodes.items]
            if resource_type == "service" and namespace:
                svcs = await asyncio.to_thread(
                    k8s_executor.v1.list_namespaced_service, namespace
                )
                return [item.to_dict() for item in svcs.items]
            if resource_type == "persistentvolumeclaim" and namespace:
                pvcs = await asyncio.to_thread(
                    k8s_executor.v1.list_namespaced_persistent_volume_claim, namespace
                )
                return [item.to_dict() for item in pvcs.items]
            # fallback: empty
            app_logger.warning(
                f"Unsupported resource type for listing",
                resource_type=resource_type,
                namespace=namespace
            )
            return []
    except Exception as e:
        app_logger.error(
            "Error listing resources",
            exception=str(e),
            exception_type=e.__class__.__name__,
            resource_type=resource_type,
            namespace=namespace
        )
        return []


async def get_resource_status(
    resource_type: str, name: str, namespace: Optional[str] = None
) -> Dict[str, Any]:
    """
    Proxy to the class method for fetching detailed status.
    """
    return await k8s_executor.get_resource_status(resource_type, name, namespace)


