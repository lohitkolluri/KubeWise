from kubernetes import client, config
from kubernetes.client.rest import ApiException
from typing import List, Dict, Any, Optional, Tuple, Set
from loguru import logger
import json
from datetime import datetime
import asyncio
import time

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

    def _init_client(self):
        try:
            # Load kubernetes configuration from default location
            config.load_kube_config()
            self.v1 = client.CoreV1Api()
            self.apps_v1 = client.AppsV1Api()
            self.batch_v1 = client.BatchV1Api()
            logger.info("Successfully initialized Kubernetes client")
        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")
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
                "handler": "_handle_restart_deployment"
            },
            "scale_deployment": {
                "description": "Scale a Kubernetes deployment to specified replicas",
                "required_params": ["name", "namespace", "replicas"],
                "optional_params": ["incremental", "max_surge_percent", "wait_for_ready"],
                "impact": "low-moderate",
                "estimated_duration": "varies by size",
                "handler": "_handle_scale_deployment"
            },
            "drain_node": {
                "description": "Drain workloads from a Kubernetes node",
                "required_params": ["name"],
                "optional_params": ["grace_period", "timeout", "ignore_daemonsets"],
                "impact": "high",
                "estimated_duration": "1-5m",
                "handler": "_handle_drain_node"
            },
            "delete_pod": {
                "description": "Delete a problematic pod (it will be recreated if managed by controller)",
                "required_params": ["name", "namespace"],
                "optional_params": ["grace_period"],
                "impact": "moderate",
                "estimated_duration": "10-30s",
                "handler": "_handle_delete_pod"
            },
            "cordon_node": {
                "description": "Mark a node as unschedulable",
                "required_params": ["name"],
                "optional_params": [],
                "impact": "low",
                "estimated_duration": "5s",
                "handler": "_handle_cordon_node"
            },
            "uncordon_node": {
                "description": "Mark a node as schedulable",
                "required_params": ["name"],
                "optional_params": [],
                "impact": "low",
                "estimated_duration": "5s",
                "handler": "_handle_uncordon_node"
            },
            "adjust_resources": {
                "description": "Adjust CPU/memory requests and limits for a deployment",
                "required_params": ["name", "namespace"],
                "optional_params": ["cpu_request", "cpu_limit", "memory_request", "memory_limit", "container"],
                "impact": "moderate",
                "estimated_duration": "30-60s",
                "handler": "_handle_adjust_resources"
            }
        }

    def _define_safe_operations(self):
        """Define safe operations that can be executed"""
        self.safe_operations = {
            # Read Operations (Generally Safe)
            "get_pod": {
                "api": "v1", "method": "read_namespaced_pod",
                "required": ["name", "namespace"], "optional": [],
                "validation": lambda p: True # Simple validation
            },
            "get_deployment": {
                "api": "apps_v1", "method": "read_namespaced_deployment",
                "required": ["name", "namespace"], "optional": [],
                "validation": lambda p: True
            },
            "list_pods": {
                "api": "v1", "method": "list_namespaced_pod",
                "required": ["namespace"], "optional": ["label_selector", "field_selector"],
                 "validation": lambda p: True
            },
             "get_logs": {
                "api": "v1", "method": "read_namespaced_pod_log",
                "required": ["name", "namespace"], "optional": ["container", "tail_lines"],
                "validation": lambda p: True # Assuming logs are read-only safe
            },
            # Modify Operations (Require stricter validation)
            "restart_deployment": {
                "api": "apps_v1", "method": "patch_namespaced_deployment",
                "required": ["name", "namespace"], "optional": [],
                # Simple validation: just check required fields exist
                "validation": lambda p: "name" in p and "namespace" in p,
                "patch_body": lambda p: { # Generate the patch body for restart
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "kubectl.kubernetes.io/restartedAt": datetime.utcnow().isoformat() + "Z"
                                }}}}}
            },
             "scale_deployment": {
                "api": "apps_v1", "method": "patch_namespaced_deployment",
                "required": ["name", "namespace", "replicas"], "optional": [],
                # Validation: Ensure replicas is a non-negative integer
                "validation": lambda p: "name" in p and "namespace" in p and \
                                        isinstance(p.get("replicas"), int) and p["replicas"] >= 0,
                 "patch_body": lambda p: {"spec": {"replicas": p["replicas"]}}
            },
            "cordon_node": {
                "api": "v1", "method": "patch_node",
                "required": ["name"], "optional": [],
                "validation": lambda p: "name" in p,
                "patch_body": lambda p: {"spec": {"unschedulable": True}}
            },
            "uncordon_node": {
                "api": "v1", "method": "patch_node",
                "required": ["name"], "optional": [],
                "validation": lambda p: "name" in p,
                "patch_body": lambda p: {"spec": {"unschedulable": False}}
            },
            "delete_pod": {
                "api": "v1", "method": "delete_namespaced_pod",
                "required": ["name", "namespace"], "optional": ["grace_period_seconds"],
                "validation": lambda p: "name" in p and "namespace" in p,
                # No patch_body for delete operations
            },
            # Add get_node if needed for context/verification
             "get_node": {
                "api": "v1", "method": "read_node",
                "required": ["name"], "optional": [],
                "validation": lambda p: True
            },
            "list_dependencies": {
                "api": "apps_v1", "method": "list_namespaced_deployment",
                "required": ["namespace"], "optional": ["label_selector"],
                "validation": lambda p: "namespace" in p,
                # Custom processing in execute_validated_command
            },
        }

    def _get_api_client(self, api_version: str):
        """Get the appropriate API client based on version"""
        if api_version == "v1" and self.v1: return self.v1
        if api_version == "apps_v1" and self.apps_v1: return self.apps_v1
        if api_version == "batch_v1" and self.batch_v1: return self.batch_v1
        logger.error(f"Kubernetes API client {api_version} not initialized.")
        raise ConnectionError(f"Kubernetes API client {api_version} not available.")

    def parse_and_validate_command(self, command_str: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Parse and validate a command string"""
        parts = command_str.split()
        if not parts: return None
        operation = parts[0]

        if operation not in self.safe_operations:
            logger.warning(f"Command '{operation}' is not defined as a safe operation.")
            return None

        op_config = self.safe_operations[operation]
        params = {}
        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                # Basic type conversion attempt (enhance as needed)
                try: params[key] = int(value)
                except ValueError: params[key] = value
            else:
                logger.warning(f"Invalid parameter format in command '{command_str}': {part}")
                return None # Reject commands with invalid param formats

        # Check required parameters
        for req_param in op_config["required"]:
            if req_param not in params:
                logger.warning(f"Missing required parameter '{req_param}' for operation '{operation}'.")
                return None

        # Perform custom validation
        validator = op_config.get("validation")
        if validator and not validator(params):
            logger.warning(f"Parameter validation failed for operation '{operation}' with params: {params}")
            return None

        logger.debug(f"Command validated: op='{operation}', params={params}")
        return operation, params

    # NEW METHOD: Parse template-based remediation action
    def parse_remediation_action(self, action: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
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
                logger.warning(f"Missing required parameter '{req_param}' for action '{action_type}'")
                return None

        return action_type, params

    # NEW METHOD: Check for dependencies
    async def check_dependencies(self, resource_type: str, name: str, namespace: str) -> Dict[str, Any]:
        """Check if other resources depend on this resource."""
        if not self.v1 or not self.apps_v1:
            return {"error": "Kubernetes client not available"}

        dependencies = {
            "has_dependencies": False,
            "dependent_resources": [],
            "critical_dependencies": False
        }

        try:
            if resource_type == "deployment":
                # Check if there are services pointing to this deployment
                deploy = await asyncio.to_thread(self.apps_v1.read_namespaced_deployment, name, namespace)
                selector = deploy.spec.selector.match_labels

                # Check services that might select this deployment
                services = await asyncio.to_thread(self.v1.list_namespaced_service, namespace)
                for svc in services.items:
                    if not svc.spec.selector:
                        continue

                    # Check if service selector matches deployment selector
                    matches = all(item in svc.spec.selector.items() for item in selector.items())
                    if matches:
                        dependencies["has_dependencies"] = True
                        dependencies["dependent_resources"].append({
                            "kind": "Service",
                            "name": svc.metadata.name,
                            "namespace": svc.metadata.namespace
                        })

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
    async def _handle_scale_deployment(self, name: str, namespace: str, target_replicas: int,
                                     incremental: bool = False, max_surge_percent: int = 25,
                                     wait_for_ready: bool = True) -> Dict[str, Any]:
        """Handle scaling with optional incremental/phased approach."""
        if not self.apps_v1:
            return {"error": "Kubernetes client not available"}

        results = {"phases": [], "final_status": None}

        try:
            # Get current deployment
            deploy = await asyncio.to_thread(self.apps_v1.read_namespaced_deployment, name, namespace)
            current_replicas = deploy.spec.replicas

            if target_replicas == current_replicas:
                return {"info": f"Deployment {namespace}/{name} already at desired scale: {target_replicas}"}

            if not incremental or abs(target_replicas - current_replicas) <= 1:
                # Simple direct scaling
                patch = {"spec": {"replicas": target_replicas}}
                result = await asyncio.to_thread(
                    self.apps_v1.patch_namespaced_deployment,
                    name=name,
                    namespace=namespace,
                    body=patch
                )
                results["phases"].append({
                    "from": current_replicas,
                    "to": target_replicas,
                    "timestamp": datetime.utcnow().isoformat()
                })

                if wait_for_ready:
                    await self._wait_for_deployment_ready(name, namespace)
            else:
                # Incremental scaling
                step_size = max(1, abs(target_replicas - current_replicas) * max_surge_percent // 100)

                if target_replicas > current_replicas:
                    # Scaling up
                    for replicas in range(current_replicas + step_size, target_replicas + 1, step_size):
                        # Cap at target
                        replicas = min(replicas, target_replicas)

                        patch = {"spec": {"replicas": replicas}}
                        await asyncio.to_thread(
                            self.apps_v1.patch_namespaced_deployment,
                            name=name,
                            namespace=namespace,
                            body=patch
                        )

                        results["phases"].append({
                            "from": current_replicas,
                            "to": replicas,
                            "timestamp": datetime.utcnow().isoformat()
                        })

                        if wait_for_ready:
                            await self._wait_for_deployment_ready(name, namespace)

                        current_replicas = replicas
                else:
                    # Scaling down
                    for replicas in range(current_replicas - step_size, target_replicas - 1, -step_size):
                        # Cap at target
                        replicas = max(replicas, target_replicas)

                        patch = {"spec": {"replicas": replicas}}
                        await asyncio.to_thread(
                            self.apps_v1.patch_namespaced_deployment,
                            name=name,
                            namespace=namespace,
                            body=patch
                        )

                        results["phases"].append({
                            "from": current_replicas,
                            "to": replicas,
                            "timestamp": datetime.utcnow().isoformat()
                        })

                        if wait_for_ready:
                            await self._wait_for_deployment_ready(name, namespace)

                        current_replicas = replicas

            # Get final status
            final_deploy = await asyncio.to_thread(self.apps_v1.read_namespaced_deployment, name, namespace)
            results["final_status"] = {
                "replicas": final_deploy.spec.replicas,
                "ready_replicas": final_deploy.status.ready_replicas or 0,
                "available_replicas": final_deploy.status.available_replicas or 0,
                "unavailable_replicas": final_deploy.status.unavailable_replicas or 0
            }

            return results

        except ApiException as e:
            logger.error(f"Error scaling deployment: {e.status} - {e.reason}")
            return {"error": f"API Error: {e.status} - {e.reason}"}
        except Exception as e:
            logger.error(f"Error scaling deployment: {e}")
            return {"error": f"Error: {str(e)}"}

    # Helper method to wait for deployment to be ready
    async def _wait_for_deployment_ready(self, name: str, namespace: str, timeout_seconds: int = 300) -> bool:
        """Wait for deployment to reach ready state."""
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            try:
                deploy = await asyncio.to_thread(self.apps_v1.read_namespaced_deployment, name, namespace)

                # Check if deployment is ready
                if (deploy.status.updated_replicas == deploy.spec.replicas and
                    deploy.status.replicas == deploy.spec.replicas and
                    deploy.status.available_replicas == deploy.spec.replicas and
                    deploy.status.observed_generation >= deploy.metadata.generation):
                    return True

                # Wait before checking again
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error checking deployment status: {e}")
                await asyncio.sleep(5)

        return False

    # NEW METHOD: Resource adjustment
    async def _handle_adjust_resources(self, name: str, namespace: str,
                                     cpu_request: Optional[str] = None,
                                     cpu_limit: Optional[str] = None,
                                     memory_request: Optional[str] = None,
                                     memory_limit: Optional[str] = None,
                                     container: Optional[str] = None) -> Dict[str, Any]:
        """Adjust resource requests and limits for a deployment."""
        if not self.apps_v1:
            return {"error": "Kubernetes client not available"}

        try:
            # Get current deployment
            deploy = await asyncio.to_thread(self.apps_v1.read_namespaced_deployment, name, namespace)

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
                updates.append({
                    "container": c.name,
                    "original": {
                        "cpu_request": requests.get("cpu"),
                        "cpu_limit": limits.get("cpu"),
                        "memory_request": requests.get("memory"),
                        "memory_limit": limits.get("memory")
                    },
                    "new": {}
                })

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
                    requests=updated_requests,
                    limits=updated_limits
                )
                c.resources = resources

            # Apply patch
            body = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": c.name,
                                    "resources": c.resources.to_dict()
                                }
                                for c in containers
                                if not container or c.name == container
                            ]
                        }
                    }
                }
            }

            result = await asyncio.to_thread(
                self.apps_v1.patch_namespaced_deployment,
                name=name,
                namespace=namespace,
                body=body
            )

            return {
                "status": "success",
                "resource_updates": updates,
                "timestamp": datetime.utcnow().isoformat()
            }

        except ApiException as e:
            logger.error(f"Error adjusting resources: {e.status} - {e.reason}")
            return {"error": f"API Error: {e.status} - {e.reason}"}
        except Exception as e:
            logger.error(f"Error adjusting resources: {e}")
            return {"error": f"Error: {str(e)}"}

    async def execute_validated_command(self, operation: str, params: Dict[str, Any]) -> Any:
        """Execute a validated command with the given parameters"""
        if operation not in self.safe_operations:
             raise ValueError(f"Attempted to execute non-validated operation: {operation}")

        # Check for template handlers
        if operation in self.command_templates:
            handler_name = self.command_templates[operation].get("handler")
            if handler_name and hasattr(self, handler_name):
                handler = getattr(self, handler_name)
                return await handler(**params)

        op_config = self.safe_operations[operation]
        api_client = self._get_api_client(op_config["api"])
        method_name = op_config["method"]
        method_to_call = getattr(api_client, method_name)

        # Prepare arguments for the API call
        api_kwargs = {}
        all_valid_params = op_config["required"] + op_config["optional"]
        for param_name, param_value in params.items():
            if param_name in all_valid_params:
                 # Map common shortcuts like 'ns' to 'namespace' if needed by client lib
                 k8s_param_name = "namespace" if param_name == "ns" else param_name
                 api_kwargs[k8s_param_name] = param_value

        # Handle patch operations specifically
        if "patch_body" in op_config:
             body = op_config["patch_body"](params) # Generate body using original params
             required_keys = ["name", "namespace"] # Patch methods usually require name, namespace
             call_args = {k: api_kwargs[k] for k in required_keys if k in api_kwargs}
             call_args["body"] = body
        else:
             call_args = api_kwargs

        try:
            logger.info(f"Executing K8s operation: {method_name} with args: {call_args}")
            # Use asyncio.to_thread for blocking K8s client calls
            result = await asyncio.to_thread(method_to_call, **call_args)
            logger.info(f"K8s operation '{operation}' successful.")
            # Serialize result
            try:
                if hasattr(result, 'to_dict'):
                    return json.dumps(result.to_dict(), indent=2, default=str)
                elif isinstance(result, str):
                     return result
                else:
                    return json.dumps(result, indent=2, default=str)
            except Exception:
                return str(result)
        except ApiException as e:
            logger.error(f"Kubernetes API call failed for '{operation}': {e.status} - {e.reason} - {e.body}")
            raise Exception(f"K8s API call failed: {e.status} - {e.body or e.reason}") from e
        except Exception as e:
            logger.error(f"Operation '{operation}' failed during execution: {e}")
            raise

    def get_safe_operations_info(self) -> Dict[str, Dict[str, Any]]:
        """Get information about safe operations"""
        info = {}
        for op_name, config in self.safe_operations.items():
            info[op_name] = {
                "required": config["required"],
                "optional": config["optional"],
                "description": f"Executes {config['api']}.{config['method']}"
            }
        return info

    def get_command_templates_info(self) -> Dict[str, Dict[str, Any]]:
        """Get information about command templates"""
        template_info = {}
        for template_name, template in self.command_templates.items():
            template_info[template_name] = {
                "description": template["description"],
                "required_params": template["required_params"],
                "optional_params": template["optional_params"],
                "impact": template["impact"],
                "estimated_duration": template["estimated_duration"]
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
            deployments = await asyncio.to_thread(self.apps_v1.list_deployment_for_all_namespaces)
            statefulsets = await asyncio.to_thread(self.apps_v1.list_stateful_set_for_all_namespaces)
            daemonsets = await asyncio.to_thread(self.apps_v1.list_daemon_set_for_all_namespaces)

            context["workloads"]["deployment_count"] = len(deployments.items)
            context["workloads"]["statefulset_count"] = len(statefulsets.items)
            context["workloads"]["daemonset_count"] = len(daemonsets.items)

            logger.info(f"Fetched cluster context: {context}")

        except ApiException as e:
            logger.error(f"Failed to fetch cluster context: {e.status} - {e.reason}")
        except Exception as e:
            logger.error(f"Error fetching cluster context: {e}")

        return context

    async def get_resource_status_for_verification(self, entity_type: str, name: str, namespace: Optional[str]) -> Optional[Dict[str, Any]]:
        """Fetches the status of a specific K8s resource for verification."""
        logger.debug(f"Fetching status for {entity_type} {namespace}/{name}")
        status = None
        try:
            if entity_type == "deployment" and namespace:
                 api_response = await self.execute_validated_command("get_deployment", {"name": name, "namespace": namespace})
                 status = json.loads(api_response).get("status") # Extract status sub-object
            elif entity_type == "pod" and namespace:
                 api_response = await self.execute_validated_command("get_pod", {"name": name, "namespace": namespace})
                 status = json.loads(api_response).get("status")
            elif entity_type == "node":
                 api_response = await self.execute_validated_command("get_node", {"name": name}) # No namespace for nodes
                 status = json.loads(api_response).get("status")
            # Add other types (StatefulSet, DaemonSet) if needed
            else:
                 logger.warning(f"Status fetch not implemented for entity type: {entity_type}")

        except Exception as e:
            logger.error(f"Failed to fetch status for verification ({entity_type} {namespace}/{name}): {e}")

        # Return only the 'status' part if found
        return status if isinstance(status, dict) else None

# Global instance
k8s_executor = K8sExecutor()
