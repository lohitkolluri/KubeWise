import asyncio
import datetime
import random
import time
from functools import wraps
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, TypeVar, Union

import motor.motor_asyncio
from bson import ObjectId # Add this import
from kubernetes_asyncio import client
from loguru import logger
from pydantic import ValidationError
from pydantic_ai import Agent

from kubewise.collector.k8s_events import load_k8s_config
from kubewise.config import settings
from kubewise.models import (
    ActionType,
    AnomalyRecord,
    PyObjectId, # Ensure PyObjectId is imported
    ExecutedActionRecord,
    RemediationAction,
    RemediationPlan,
)
from kubewise.remediation.planner import generate_remediation_plan, load_static_plan
from kubewise.utils.retry import with_exponential_backoff # Import from utility file

# --- Type Definitions ---
T = TypeVar('T')
ActionCoroutine = Callable[[client.ApiClient, Dict[str, Any]], Coroutine[Any, Any, Tuple[bool, str]]]

# --- Constants ---
# MAX_RETRY_ATTEMPTS, INITIAL_RETRY_DELAY, etc. are now defaults in with_exponential_backoff
# ACTION_TIMEOUT is specific to action execution, keep it here
ACTION_TIMEOUT = 60.0  # seconds before action times out

# --- Registry for Remediation Actions ---
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


# --- Registered DSL Actions ---

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
    namespace = params.get("namespace", "default") # Default to 'default' if not provided

    if not name or replicas is None:
        return False, "Missing 'name' or 'replicas' parameter for scale_deployment"

    # --- Debugging Replica Value ---
    logger.debug(f"scale_deployment_action received replicas: type={type(replicas)}, value='{replicas}'")
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
        logger.info(f"Deleting pod '{namespace}/{name}'...")
        # Default grace period, can be customized via V1DeleteOptions if needed
        await core_v1_api.delete_namespaced_pod(name=name, namespace=namespace)
        msg = f"Successfully initiated deletion for pod '{namespace}/{name}'."
        logger.info(msg)
        # Note: Deletion is asynchronous in K8s. We report success on initiating.
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
    namespace = params.get("namespace", "default")  # Default to 'default' if not provided

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



async def execute_action_with_timeout(
    action: RemediationAction,
    api_client: client.ApiClient,
    action_idx: int,
    total_actions: int,
    anomaly_id: PyObjectId, # <--- Change type hint from str
    action_log_collection: motor.motor_asyncio.AsyncIOMotorCollection,
    entity_id: str,
    anomaly_score: float, # Combined score
    source_type: str,
    app_context=None # Optional AppContext for metrics
) -> Tuple[bool, str]:
    """
    Executes a single remediation action with timeout.
    
    Args:
        action: The RemediationAction to execute
        api_client: Kubernetes API client
        action_idx: Index of this action in the plan (1-based)
        total_actions: Total number of actions in the plan
        anomaly_id: ID of the anomaly being remediated
        action_log_collection: MongoDB collection to log action results
        entity_id: ID of the entity being remediated
        anomaly_score: Score of the anomaly
        source_type: Source type of the anomaly
        
    Returns:
        Tuple of (success: bool, message: str)
    """
    # Convert anomaly_id (which is PyObjectId now) back to string only for logging if needed
    anomaly_id_str_for_log = str(anomaly_id)
    action_context = f"action {action_idx}/{total_actions} ({action.action_type})"
    logger.info(
        f"Executing {action_context} with params: {action.parameters}. "
        f"Anomaly ID: {anomaly_id_str_for_log}" # Use string for logging
    )

    handler = ACTION_REGISTRY.get(action.action_type)
    success = False
    msg = ""
    
    if not handler:
        msg = f"No handler registered for action type '{action.action_type}'"
        logger.error(f"{msg}. Anomaly ID: {anomaly_id}")
    else:
        # Create a retry function that captures the current context
        async def retry_action() -> Tuple[bool, str]:
            return await handler(api_client, action.parameters)
        
        try:
            # Apply exponential backoff to the retry function
            decorated_retry = with_exponential_backoff()(retry_action)
            # Call the decorated function to get a coroutine
            retry_coroutine = decorated_retry()
            # Create task and apply timeout
            retry_task = asyncio.create_task(retry_coroutine)
            
            # Record start time for metrics
            start_time = time.time()
            # Apply timeout
            success, msg = await asyncio.wait_for(retry_task, timeout=ACTION_TIMEOUT)
            # Record duration for metrics
            duration = time.time() - start_time
            
            # Record metrics if app_context is available
            if app_context and hasattr(app_context, 'record_remediation'):
                try:
                    # Extract namespace from entity_id or parameters
                    namespace = "unknown"
                    if action.parameters.get("namespace"):
                        namespace = action.parameters["namespace"]
                    elif "/" in entity_id:
                        namespace = entity_id.split("/")[0]
                        
                    # Extract entity type if available
                    entity_type = action.entity_type or "unknown"
                    
                    app_context.record_remediation(
                        action_type=str(action.action_type),
                        entity_type=entity_type,
                        namespace=namespace,
                        success=success,
                        duration=duration
                    )
                except Exception as metrics_err:
                    logger.warning(f"Failed to record remediation metrics: {metrics_err}")
            
        except asyncio.TimeoutError:
            msg = f"Action timed out after {ACTION_TIMEOUT}s"
            logger.error(f"{msg}. Anomaly ID: {anomaly_id_str_for_log}") # Use string for logging
            success = False
            
            # Record timeout metrics if app_context is available
            if app_context and hasattr(app_context, 'record_remediation'):
                try:
                    namespace = action.parameters.get("namespace", "unknown")
                    entity_type = action.entity_type or "unknown"
                    
                    app_context.record_remediation(
                        action_type=str(action.action_type),
                        entity_type=entity_type,
                        namespace=namespace,
                        success=False,
                        duration=ACTION_TIMEOUT
                    )
                except Exception as metrics_err:
                    logger.warning(f"Failed to record timeout metrics: {metrics_err}")

    # Log the execution result with structured context
    action_record = ExecutedActionRecord(
        # id should be generated by default_factory
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        anomaly_id=anomaly_id,  # Now passing PyObjectId directly
        action_type=action.action_type,
        parameters=action.parameters,
        status="success" if success else "failure",
        details=msg,
        entity_type="pod",  # Default to pod since most actions are pod-related
        entity_id=entity_id,
        success=success, 
        message=msg,
        duration_seconds=0.0,  # Will be updated with actual duration
        context={
            "remediation_plan_id": str(anomaly_id), # Store as string in context if preferred
            "entity": entity_id,
            "action_number": action_idx,
            "total_actions": total_actions,
            "anomaly_score": anomaly_score,
            "source_type": source_type
        }
    )

    try:
        # Prepare the record for insertion, relying on default_factory for _id
        # Use exclude_none=True to avoid inserting fields that are None
        record_to_insert = action_record.model_dump(by_alias=True, exclude_none=True)

        # Check if _id is present after dump (it should be due to default_factory)
        if "_id" not in record_to_insert or record_to_insert["_id"] is None:
             # This case indicates a problem with Pydantic/ObjectId handling
             logger.error(f"Critical: _id is missing or None in record_to_insert before MongoDB insert for anomaly {anomaly_id_str_for_log}. Record: {record_to_insert}")
             success = False # Mark action as failed if logging fails critically
             msg = "Failed to log action due to missing or null _id before insert"
        else:
            # Insert the record
            await action_log_collection.insert_one(record_to_insert)
            # Log the ID that was actually inserted
            inserted_id = record_to_insert.get("_id", "UNKNOWN_ID")
            logger.debug(f"Logged execution result for {action_context} with DB ID {inserted_id}. Anomaly ID: {anomaly_id_str_for_log}")

    except motor.motor_asyncio.DuplicateKeyError as log_e: # Catch specific error
        # More specific logging for the duplicate key error
        # Log the ID we *tried* to insert
        attempted_id = record_to_insert.get("_id", "UNKNOWN_ID") if 'record_to_insert' in locals() else "UNKNOWN_ID"
        dup_key_value = log_e.details.get('keyValue', {}).get('_id', 'UNKNOWN_KEY')
        # Log the record we *tried* to insert for better debugging
        record_str = str(record_to_insert) if 'record_to_insert' in locals() else "Record not available"
        logger.error(
            f"DuplicateKeyError inserting action log. Attempted ID: {attempted_id}, Duplicate Key in DB: {dup_key_value}. "
            f"Error Details: {log_e.details}. Record attempted: {record_str}. Anomaly ID: {anomaly_id_str_for_log}"
        )
        # Even if logging fails with DupKey, the action itself might have succeeded.
        # We don't change the 'success' status here, just log the logging failure.
    except Exception as log_e:
        logger.exception(f"Failed to log executed action record: {log_e}. Anomaly ID: {anomaly_id_str_for_log}")
        # Decide if logging failure should mark the action as failed.
        # For now, assume the action might have succeeded but logging failed.

    return success, msg


async def execute_remediation_plan(
    plan: RemediationPlan,
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    k8s_client: client.ApiClient,
    app_context=None,  # Optional AppContext for metrics
) -> bool:
    """
    Executes the actions defined in a RemediationPlan with robust retry logic.
    
    Implements:
    - Thorough validation of plans
    - Exponential backoff retries for failing actions
    - Comprehensive logging of all actions with context fields
    - Parallel execution of independent actions when possible
    - Metrics tracking for operations monitoring
    
    Args:
        plan: The RemediationPlan to execute
        anomaly_record: The anomaly that triggered remediation
        db: Database instance for storing execution results
        k8s_client: Kubernetes API client
        app_context: Optional AppContext for recording metrics
        
    Returns:
        True if all actions executed successfully, False otherwise
    """
    action_log_collection = db["executed_actions"]
    anomaly_collection = db["anomalies"]

    if not anomaly_record.id:
        logger.error("Cannot execute plan: AnomalyRecord is missing database ID")
        return False

    anomaly_id_obj: PyObjectId = anomaly_record.id # Assign the PyObjectId
    anomaly_id_str = str(anomaly_id_obj) # Keep string version for logging

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
        await anomaly_collection.update_one(
            {"_id": anomaly_record.id}, 
            {"$set": {"remediation_status": "completed", "remediation_message": validated_plan.reasoning}}
        )
        return True

    # Get entity ID for structured logging
    entity_id = "unknown"
    if anomaly_record.entity_id:
        entity_id = anomaly_record.entity_id

    logger.info(
        f"Executing remediation plan for anomaly {anomaly_id_str}. "
        f"Entity: {entity_id}. "
        f"Actions: {len(validated_plan.actions)}. Reasoning: {validated_plan.reasoning}"
    )
    
    await anomaly_collection.update_one(
        {"_id": anomaly_record.id}, {"$set": {"remediation_status": "executing"}}
    )

    # Determine if actions can be executed in parallel
    # For simplicity, we'll consider each action independent for now
    # In a more advanced version, you might analyze dependencies between actions
    # Use the configurable setting for max parallel actions
    can_parallelize = len(validated_plan.actions) > 1 and len(validated_plan.actions) <= settings.max_parallel_remediation_actions

    overall_success = True
    final_message = ""

    if can_parallelize:
        # Execute actions in parallel
        logger.info(
            f"Executing {len(validated_plan.actions)} remediation actions in parallel (max {settings.max_parallel_remediation_actions}) for anomaly {anomaly_id_str}"
        )
        
        # Create tasks for all actions
        tasks = []
        for i, action in enumerate(validated_plan.actions):
            task = execute_action_with_timeout(
                action=action,
                api_client=k8s_client,
                action_idx=i+1,
                total_actions=len(validated_plan.actions),
                anomaly_id=anomaly_id_obj, # <--- Pass PyObjectId
                action_log_collection=action_log_collection,
                entity_id=entity_id,
                anomaly_score=anomaly_record.anomaly_score, # Use combined score
                source_type=anomaly_record.data_source or "unknown",
                app_context=app_context
            )
            tasks.append(task)
        
        # Execute all tasks concurrently with proper handling
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Action {i+1} failed with error: {result}")
                overall_success = False
                final_message = f"Action {i+1} failed with error: {result}"
            else:
                success, msg = result
                if not success:
                    overall_success = False
                    final_message = msg
    else:
        # Execute actions sequentially
        for i, action in enumerate(validated_plan.actions):
            success, msg = await execute_action_with_timeout(
                action=action,
                api_client=k8s_client,
                action_idx=i+1,
                total_actions=len(validated_plan.actions),
                anomaly_id=anomaly_id_obj, # <--- Pass PyObjectId
                action_log_collection=action_log_collection,
                entity_id=entity_id,
                anomaly_score=anomaly_record.anomaly_score, # Use combined score
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
        remediation_plan.anomaly_id = anomaly_record.id
        
        # Execute the remediation plan
        success = await execute_remediation_plan(
            plan=remediation_plan,
            anomaly_record=anomaly_record,
            db=db,
            k8s_client=k8s_client
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
        anomaly_record = await db["anomalies"].find_one({"_id": ObjectId(anomaly_id)})
        
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
