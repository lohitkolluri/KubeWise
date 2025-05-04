import asyncio
import datetime
import random
import time # Added for monotonic time checks
import time
from contextlib import AsyncExitStack
from functools import wraps
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional, Set, Tuple, TypeVar, Union

import motor.motor_asyncio
from kubernetes_asyncio import client, config, watch
from loguru import logger
from pydantic import ValidationError

from kubewise.models import KubernetesEvent
from kubewise.utils.retry import with_exponential_backoff # Import from utility file

# --- Type Variables for Generic Functions ---
T = TypeVar('T')

# --- Constants ---

# Define relevant event types and reasons to focus on
RELEVANT_EVENT_TYPES: Set[str] = {"Warning"}
RELEVANT_EVENT_REASONS: Set[str] = {
    # Pod failures
    "Failed",
    "FailedScheduling",
    "CrashLoopBackOff",
    "BackOff", 
    "Evicted",
    "Unhealthy",
    "OOMKilled",
    "ContainersNotReady",
    
    # Resource exhaustion
    "NodeNotReady",
    "NodeNotSchedulable",
    "FailedNodeAllocatableEnforcement",
    "NodeHasDiskPressure",
    "FreeDiskSpaceFailed",
    
    # Network/connectivity issues
    "HostPortConflict",
    "FailedAttachVolume",
    "FailedDetachVolume",
    "FailedMount",
    "NetworkNotReady",
    
    # Container/Image issues
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerError",
}

# --- State Persistence Constants ---
STATE_COLLECTION_NAME = "_kubewise_state"
WATCHER_STATE_DOC_ID = "k8s_event_watcher_state"

# --- Connection Configuration ---
WATCH_TIMEOUT_SECONDS = 3600  # 1 hour server-side timeout
HEALTH_CHECK_INTERVAL = 300.0  # Check connection health every 5 minutes

# --- Utility Functions ---

@with_exponential_backoff(max_retries=5)
async def load_k8s_config() -> None:
    """
    Load Kubernetes configuration (in-cluster or local kubeconfig) with retry logic.
    """
    try:
        # Try loading in-cluster configuration first
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration")
    except config.ConfigException:
        # If in-cluster fails, try kubeconfig
        await config.load_kube_config()
        logger.info("Loaded Kubernetes configuration from kubeconfig file")


def parse_k8s_event(raw_event: dict) -> Optional[KubernetesEvent]:
    """
    Parse a raw Kubernetes event dictionary into a KubernetesEvent model.
    
    Performs initial filtering and ensures proper structure before full validation.
    Filters out events that are too old or likely resolved already.

    Args:
        raw_event: The raw event dictionary from the Kubernetes API.

    Returns:
        A KubernetesEvent object or None if parsing fails or event is irrelevant.
    """
    try:
        # Basic filtering before full parsing
        event_type = raw_event.get("type")
        reason = raw_event.get("reason")
        involved_object = raw_event.get("involvedObject", {})
        metadata = raw_event.get("metadata", {})

        # Skip events that don't match our criteria for relevant types
        if not event_type or event_type not in RELEVANT_EVENT_TYPES:
            return None
            
        # Additionally filter by reason if RELEVANT_EVENT_REASONS is defined
        if RELEVANT_EVENT_REASONS and reason not in RELEVANT_EVENT_REASONS:
            return None
            
        # Filter out events with missing involved object information
        involved_object_name = involved_object.get("name")
        involved_object_kind = involved_object.get("kind")
        
        # Skip events with missing critical involved object details
        if not involved_object_name or not involved_object_kind:
            logger.debug(f"Skipping event with missing involved object details - Name: {involved_object_name}, Kind: {involved_object_kind}, Reason: {reason}")
            return None

        # Filter out old events - only process events from the last 15 minutes
        # This prevents reporting failures that have likely been resolved
        current_time = datetime.datetime.now(datetime.timezone.utc)
        max_event_age_minutes = 15
        
        # Get the most recent timestamp from last_timestamp or first_timestamp
        last_timestamp = raw_event.get("lastTimestamp")
        first_timestamp = raw_event.get("firstTimestamp")
        
        if last_timestamp:
            # Convert string timestamp to datetime if needed
            if isinstance(last_timestamp, str):
                try:
                    last_timestamp = datetime.datetime.fromisoformat(last_timestamp.replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    last_timestamp = None
        
        if first_timestamp and not last_timestamp:
            # Convert string timestamp to datetime if needed
            if isinstance(first_timestamp, str):
                try:
                    first_timestamp = datetime.datetime.fromisoformat(first_timestamp.replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    first_timestamp = None
        
        event_timestamp = last_timestamp or first_timestamp
        
        # Skip events that are too old (likely already resolved)
        if event_timestamp and (current_time - event_timestamp).total_seconds() > (max_event_age_minutes * 60):
            logger.debug(f"Skipping old event: {reason} from {event_timestamp} (more than {max_event_age_minutes} minutes old)")
            return None

        # Add event_id using metadata.uid
        raw_event["event_id"] = metadata.get("uid", f"unknown-{datetime.datetime.now().isoformat()}")

        # Pydantic will handle alias mapping (e.g., involvedObjectKind -> involved_object_kind)
        parsed_event = KubernetesEvent.model_validate(raw_event)
        
        # Double-check that the parsed event has the required object information
        if not parsed_event.involved_object_name or not parsed_event.involved_object_kind:
            logger.debug(f"Rejecting event after validation due to missing involved object details: {reason}")
            return None
            
        return parsed_event

    except ValidationError as e:
        event_name = raw_event.get("metadata", {}).get("name", "unknown")
        logger.warning(f"Failed to validate Kubernetes event '{event_name}': {e}")
        return None
    except Exception as e:
        event_name = raw_event.get("metadata", {}).get("name", "unknown")
        logger.error(f"Unexpected error parsing Kubernetes event '{event_name}': {e}")
        return None


class KubernetesEventWatcher:
    """
    Fully async Kubernetes event watcher with robust reconnection handling.
    
    Features:
    - Proper resourceVersion tracking with bookmarks
    - Persisted state for resuming watch after restarts
    - Exponential backoff for connection failures
    - Full async design with event queue
    - Structured logging with context fields
    - Tracking of resource states to avoid detecting resolved failures
    """
    
    def __init__(
        self,
        events_queue: asyncio.Queue[KubernetesEvent],
        db: motor.motor_asyncio.AsyncIOMotorDatabase,
        k8s_client: client.ApiClient, # Accept the client instance
        watch_timeout: int = 300,
        resource_version_collection: str = "k8s_resource_versions",
        **kwargs
    ):
        """
        Initialize the Kubernetes event watcher.
        
        Args:
            events_queue: Queue to put parsed events onto
            db: MongoDB database connection
            k8s_client: Kubernetes API client instance
            watch_timeout: Timeout for watch connection in seconds
            resource_version_collection: Collection name for persisting resource version
        """
        self._events_queue = events_queue
        self._db = db
        self._resource_version = None
        self._watch_timeout = watch_timeout
        self._resource_version_collection = resource_version_collection
        self._shutdown_event = asyncio.Event()
        self._k8s_client = k8s_client
        self._api_client = None  # Will be initialized in start()
        self._core_api = None  # Will be initialized in start()
        self._watch_task = None
        self._health_check_task = None
        self._resource_state_task = None
        
        # Track Kubernetes resource states to avoid detecting resolved failures
        self._resource_states = {}  # {resource_key: {"healthy": bool, "checked_at": datetime}}
        self._resource_state_queue = asyncio.Queue()  # Queue for resource states updates
        
        # Shared state between watch tasks
        self._last_successful_watch = None
        self._watch_failures = 0
        self._watch_restarts = 0
        self._connection_backoff = 1.0  # Initial backoff in seconds
        
        # Logger initialization
        self._logger = logger.bind(
            module="k8s_events",
            component="watcher"
        )
        
        self._logger.info("Initialized KubernetesEventWatcher")

    async def start(self) -> None:
        """Start the event watcher with proper connection setup."""
        if self._watch_task:
            logger.warning("KubernetesEventWatcher is already running")
            return
            
        logger.info("Starting Kubernetes event watcher")

        # Configuration should be loaded before watcher initialization
        # Remove: await load_k8s_config()

        # Get last resource version from database
        self._resource_version = await self._get_last_resource_version()
        
        # Start background tasks
        self._watch_task = asyncio.create_task(
            self._event_watch_loop(),
            name="kubernetes_event_watcher"
        )
        self._health_check_task = asyncio.create_task(
            self._health_check_loop(), 
            name="kubernetes_event_watcher_health"
        )
        self._resource_state_task = asyncio.create_task(
            self._resource_state_loop(),
            name="kubernetes_resource_state_checker"
        )
        
        logger.info(f"Kubernetes event watcher started successfully with resource_version: {self._resource_version or 'latest'}")
    
    async def stop(self) -> None:
        """Stop the event watcher and clean up connections."""
        if not self._watch_task:
            logger.debug("KubernetesEventWatcher is not running")
            return
            
        logger.info("Stopping Kubernetes event watcher...")
        self._shutdown_event.set() # Signal loops to stop

        # Save current resource version before stopping (with timeout)
        if self._resource_version:
            try:
                logger.info("Attempting to save resource version before stopping...")
                await asyncio.wait_for(self._save_resource_version(self._resource_version), timeout=10.0) # Add 10s timeout
                logger.info("Successfully saved resource version.")
            except asyncio.TimeoutError:
                logger.error("Timeout occurred while saving resource version during stop.")
            except Exception as e:
                logger.exception(f"Error saving resource version during stop: {e}")


        # Cancel tasks
        tasks_to_await = []
        for task in [self._watch_task, self._health_check_task, self._resource_state_task]:
            if task and not task.done():
                logger.info(f"Cancelling task: {task.get_name()}")
                task.cancel()
                tasks_to_await.append(task)

        # Wait for cancelled tasks to finish (with timeout)
        if tasks_to_await:
            logger.info(f"Waiting for {len(tasks_to_await)} tasks to finish after cancellation...")
            # Wait for all cancelled tasks concurrently with a timeout
            # Use wait instead of gather to handle timeouts more gracefully for individual tasks
            done, pending = await asyncio.wait(tasks_to_await, timeout=15.0, return_when=asyncio.ALL_COMPLETED)

            if pending:
                logger.warning(f"{len(pending)} tasks did not finish within the 15s timeout after cancellation:")
                for task in pending:
                    # Attempt to log task state if possible (may vary by Python version)
                    try:
                        logger.warning(f"  - Task still pending: {task.get_name()} (State: {task._state})")
                    except AttributeError:
                         logger.warning(f"  - Task still pending: {task.get_name()}")
            else:
                 logger.info("All cancelled tasks finished within timeout.")

            # Log any exceptions from tasks that completed (even if cancelled)
            for task in done:
                 if task.cancelled():
                     logger.debug(f"Task {task.get_name()} was cancelled successfully.")
                 elif task.exception():
                     # Log the exception raised by the task
                     try:
                         task.result() # This will re-raise the exception for logging
                     except Exception as task_exc:
                         logger.error(f"Task {task.get_name()} finished with exception: {task_exc!r}")


        # Close the exit stack (with timeout)
        try:
            logger.info("Closing async exit stack...")
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=5.0) # Add 5s timeout
            logger.info("Async exit stack closed.")
        except asyncio.TimeoutError:
            logger.error("Timeout occurred while closing async exit stack.")
        except Exception as e:
             logger.exception(f"Error closing async exit stack: {e}")

        logger.info("Kubernetes event watcher stop sequence finished.")
    
    @with_exponential_backoff(max_retries=None) # Use decorator for retries, retry indefinitely until stop is called
    async def _watch_stream_with_backoff(self, v1: client.CoreV1Api, w: watch.Watch) -> None:
        """Starts and processes the Kubernetes event watch stream."""
        logger.info(
            f"Starting Kubernetes event watch stream with resource_version='{self._resource_version or 'latest'}'"
        )

        # Configure watch options
        watch_options = {
            'timeout_seconds': self._watch_timeout,
            'resource_version': self._resource_version,
            'allow_watch_bookmarks': True  # Enable bookmarks for tracking resourceVersion
        }

        # Start the watch stream
        stream = w.stream(v1.list_event_for_all_namespaces, **watch_options)

        # Process events from stream
        async for event_obj in stream:
            if not self._shutdown_event.is_set():
                await self._process_event(event_obj)
                self._last_successful_watch = time.monotonic()
            else:
                logger.info("Watch stream stopped during stream processing")
                break

        logger.info("Watch stream ended normally.") # No longer "Will restart." as the loop handles restart

    async def _event_watch_loop(self) -> None:
        """Main loop for watching Kubernetes events."""
        logger.info("Starting Kubernetes event watch loop")

        # Use the provided ApiClient instance
        if not self._k8s_client:
             logger.error("Kubernetes API client not provided to watcher. Exiting loop.")
             return
        v1 = client.CoreV1Api(self._k8s_client)
        w = watch.Watch()

        while not self._shutdown_event.is_set():
            try:
                # Call the decorated function
                await self._watch_stream_with_backoff(v1, w)

            except client.ApiException as e:
                self._watch_failures += 1

                if e.status == 410:  # Gone - resource version too old
                    # Log as INFO since this is expected behavior when RV expires
                    logger.info(f"Resource version '{self._resource_version}' too old (410 Gone). Resetting watch to latest.")
                    self._resource_version = ""
                    await self._save_resource_version(self._resource_version) # Save the reset state
                else:
                    # Generic API errors are handled by the decorator's retry logic
                    # If the decorator's retries are exhausted, the exception will propagate here
                    logger.error(
                        f"Kubernetes API error during event watch after retries: {e.status} - {e.reason}. "
                        f"Restarting watch loop." # The while loop handles the restart
                    )

            except asyncio.TimeoutError:
                # Expected timeout after watch_timeout
                logger.info("Kubernetes watch stream timed out as expected. Reconnecting with same resource_version.")
                # The while loop will continue, effectively reconnecting

            except asyncio.CancelledError:
                logger.info("Kubernetes event watch task cancelled")
                await self._save_resource_version(self._resource_version)
                break

            except Exception as e:
                self._watch_failures += 1
                logger.exception(
                    f"Unexpected error in Kubernetes event watch loop: {e}. "
                    f"Restarting watch loop." # The while loop handles the restart
                )
    
    async def _process_event(self, event_obj: Dict[str, Any]) -> None:
        """Process a single event from the watch stream."""
        try:
            event_type = event_obj.get("type")  # ADDED, MODIFIED, DELETED, BOOKMARK, ERROR
            raw_event = event_obj.get("object")
            
            if not raw_event:
                logger.warning(f"Received event type {event_type} with no object data")
                return
                
            if event_type == "ERROR":
                # Handle API errors reported through the watch stream
                status = getattr(raw_event, 'status', 'Unknown')
                message = getattr(raw_event, 'message', 'No message')
                reason = getattr(raw_event, 'reason', 'Unknown')
                code = getattr(raw_event, 'code', 'N/A')
                
                logger.error(
                    f"Kubernetes watch stream error: Status={status}, Reason={reason}, "
                    f"Code={code}, Message={message}"
                )
                
                if reason == "Expired":
                    logger.warning("Kubernetes watch resource version expired. Restarting with latest.")
                    self._resource_version = ""  # Reset to get latest
                    await self._save_resource_version(self._resource_version)
                    # Let the watch loop handle the reconnection
                    raise RuntimeError("Resource version expired")
                    
            elif event_type == "BOOKMARK":
                # Update resource_version from bookmark
                if hasattr(raw_event, 'metadata') and raw_event.metadata and hasattr(raw_event.metadata, 'resource_version'):
                    new_rv = raw_event.metadata.resource_version
                    if new_rv != self._resource_version:
                        logger.debug(f"Received watch bookmark. Updated resource_version to {new_rv}")
                        self._resource_version = new_rv
                        # Save bookmarks periodically
                        await self._save_resource_version(self._resource_version)
                else:
                     # Log as DEBUG as this is an edge case but doesn't break functionality
                    logger.debug("Received BOOKMARK event without resource_version in metadata, ignoring.")

            elif event_type in ["ADDED", "MODIFIED"]:
                # Update resource_version from the event
                if hasattr(raw_event, 'metadata') and raw_event.metadata and hasattr(raw_event.metadata, 'resource_version'):
                    self._resource_version = raw_event.metadata.resource_version
                
                # Convert to dict if needed
                raw_event_dict = None
                try:
                    if hasattr(raw_event, 'to_dict'):
                        raw_event_dict = raw_event.to_dict()
                    else:
                        # If it's already a dict
                        raw_event_dict = raw_event
                    
                    # Skip if we couldn't convert to a dict
                    if not raw_event_dict:
                        logger.warning(f"Could not convert event to dictionary: {type(raw_event)}")
                        return
                    
                    # Update resource state tracking for involved object 
                    if hasattr(raw_event, 'involved_object') and raw_event.involved_object:
                        # It's a CoreV1Event object
                        involved_object_dict = raw_event.involved_object.to_dict() if hasattr(raw_event.involved_object, 'to_dict') else {}
                        kind = getattr(raw_event.involved_object, 'kind', None) 
                        namespace = getattr(raw_event.involved_object, 'namespace', None)
                        name = getattr(raw_event.involved_object, 'name', None)
                    else:
                        # It's a dictionary
                        involved_object_dict = raw_event_dict.get("involvedObject", {})
                        kind = involved_object_dict.get("kind")
                        namespace = involved_object_dict.get("namespace")
                        name = involved_object_dict.get("name")
                    
                    # Extract reason and message safely
                    reason = None
                    message = None
                    if hasattr(raw_event, 'reason'):
                        reason = getattr(raw_event, 'reason')
                    else:
                        reason = raw_event_dict.get("reason", "Unknown")
                        
                    if hasattr(raw_event, 'message'):
                        message = getattr(raw_event, 'message')
                    else:
                        message = raw_event_dict.get("message", "")
                    
                    # Log events with missing kind or name for troubleshooting
                    if not kind or not name:
                        logger.debug(f"Event missing kind ({kind}) or name ({name}): {reason} - {message}")
                    
                    if kind and name:
                        resource_key = f"{kind}/{namespace or 'cluster'}/{name}"
                        
                        # Store basic resource info
                        self._resource_states[resource_key] = {
                            "kind": kind,
                            "name": name,
                            "last_event_time": datetime.datetime.now(datetime.timezone.utc),
                            "last_event_reason": reason
                        }
                        
                        if namespace:
                            self._resource_states[resource_key]["namespace"] = namespace
                    
                    # Check if the resource is known to be healthy before processing event
                    try:
                        if self._should_skip_event_for_healthy_resource(raw_event):
                            logger.debug(f"Skipping event for healthy resource: {reason} - {message}")
                            return
                    except Exception as e:
                        logger.warning(f"Error checking if event should be skipped: {e}")
                    
                    try:
                        parsed_event = parse_k8s_event(raw_event_dict)
                        if parsed_event:
                            # Successfully parsed a relevant event
                            self._watch_restarts += 1
                            if parsed_event.involved_object_name:
                                logger.debug(
                                    f"Processing K8s event: {parsed_event.involved_object_kind}/{parsed_event.involved_object_namespace or 'cluster'}/{parsed_event.involved_object_name} - "
                                    f"{parsed_event.reason} (RV: {self._resource_version})"
                                )
                            else:
                                # This shouldn't happen now with our updated validation, but log it if it does
                                logger.warning(
                                    f"Processing K8s event with empty name/namespace: {parsed_event.reason} - "
                                    f"Type: {parsed_event.type}, RV: {self._resource_version}, "
                                    f"Message: {parsed_event.message[:100] if parsed_event.message else 'None'}"
                                )
                                # Log the raw event for debugging
                                logger.debug(f"Raw event data: {str(raw_event_dict)[:500]}")
                                
                            # Add to processing queue
                            await self._events_queue.put(parsed_event)
                    except Exception as e:
                        logger.error(f"Error parsing event: {e}, event data: {raw_event_dict.get('reason')} - {raw_event_dict.get('message', '')[:100]}")
                except Exception as e:
                    logger.error(f"Error processing event object: {e}")
                    
            elif event_type == "DELETED":
                # Usually not relevant for anomaly detection based on warnings
                # But update resource_version
                if hasattr(raw_event, 'metadata') and raw_event.metadata and hasattr(raw_event.metadata, 'resource_version'):
                    self._resource_version = raw_event.metadata.resource_version
                    
            else:
                logger.warning(f"Unknown event type received from watch stream: {event_type}")
            
            # After parsing the event, update resource state tracking
            try:
                self._update_resource_state_from_event(raw_event)
            except Exception as e:
                logger.error(f"Error updating resource state from event: {e}")
        except Exception as e:
            logger.error(f"Unhandled error in _process_event: {e}")
    
    async def _health_check_loop(self) -> None:
        """Periodically check the health of the watcher connection."""
        logger.info(f"Starting Kubernetes event watcher health check loop (every {HEALTH_CHECK_INTERVAL}s)")
        
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                
                # Skip health check if we've had a recent event
                if (self._last_successful_watch and 
                    time.monotonic() - self._last_successful_watch < HEALTH_CHECK_INTERVAL):
                    logger.debug("Skipping health check, recent successful event processing")
                    continue
                
                # Check if watch task is still running
                if not self._watch_task or self._watch_task.done():
                    if self._watch_task and self._watch_task.exception():
                        logger.error(f"Watch task failed with exception: {self._watch_task.exception()}")
                    
                    logger.warning("Watch task is not running. Restarting event watcher.")
                    # Restart the watch task
                    self._watch_task = asyncio.create_task(
                        self._event_watch_loop(),
                        name="kubernetes_event_watcher_restarted"
                    )
                    
            except asyncio.CancelledError:
                logger.info("Health check loop cancelled")
                break
                
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(5.0)  # Short sleep on error
    
    @with_exponential_backoff(max_retries=3)
    async def _get_last_resource_version(self) -> str:
        """
        Retrieve the last known resource version from MongoDB with retry logic.
        
        On restart, we'll always start from the latest events (empty string)
        to avoid the "resourceVersion too old" error (410 Gone).
        """
        # Always start from latest events on application restart
        # This avoids the "resourceVersion too old" error (410 Gone)
        logger.info("Starting watch from latest events to avoid resource version expiration")
        return ""
        
    @with_exponential_backoff(max_retries=3)
    async def _save_resource_version(self, resource_version: str) -> None:
        """
        Save the current resource version to MongoDB with retry logic.
        """
        if not resource_version:  # Don't save empty string
            return
        
        await self._db[STATE_COLLECTION_NAME].update_one(
            {"_id": WATCHER_STATE_DOC_ID},
            {"$set": {
                "last_resource_version": resource_version,
                "updated_at": datetime.datetime.now(datetime.timezone.utc)
            }},
            upsert=True
        )
        logger.debug(f"Saved resource_version {resource_version} to DB")

    async def _resource_state_loop(self) -> None:
        """
        Background task to periodically check the health status of all resources
        that have been involved in events, to avoid detecting issues that have already
        been resolved.
        """
        try:
            check_interval = 300.0  # Check resource states every 5 minutes
            self._logger.info(f"Starting Kubernetes resource state checker loop (every {check_interval}s)")
            
            while not self._shutdown_event.is_set():
                try:
                    # Check health of resources that have had events
                    await self._check_all_resources_health()
                    
                    # Process any pending resource state updates
                    await self._process_resource_state_queue()
                    
                    # Share the resource states with the detector
                    await self._share_resource_states()
                    
                    # Wait for the next check interval or until shutdown
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=check_interval
                        )
                    except asyncio.TimeoutError:
                        # This is expected - just continue to the next check
                        pass
                        
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._logger.exception(f"Error in resource state checker loop: {e}")
                    # Wait a bit before retrying
                    await asyncio.sleep(min(60.0, check_interval / 2))
                    
        except asyncio.CancelledError:
            self._logger.info("Resource state checker loop cancelled")
            
    async def _check_all_resources_health(self):
        """
        Check the health status of all resources that have been involved in events.
        """
        try:
            # Group resources by kind for efficient batch retrieval
            resources_by_kind = {}
            for resource_key in list(self._resource_states.keys()):
                try:
                    kind, namespace, name = resource_key.split("/", 2)
                    if kind not in resources_by_kind:
                        resources_by_kind[kind] = []
                    resources_by_kind[kind].append((namespace, name))
                except ValueError:
                    # Invalid resource key format
                    self._logger.warning(f"Invalid resource key format: {resource_key}")
                    continue
                    
            # Check health of pods
            if "Pod" in resources_by_kind:
                await self._check_pods_health(resources_by_kind["Pod"])
                
            # Check health of nodes
            if "Node" in resources_by_kind:
                await self._check_nodes_health(resources_by_kind["Node"])
                
            # Check health of deployments
            if "Deployment" in resources_by_kind:
                await self._check_deployments_health(resources_by_kind["Deployment"])
                
            # Check health of statefulsets
            if "StatefulSet" in resources_by_kind:
                await self._check_statefulsets_health(resources_by_kind["StatefulSet"])
                
            # Check health of daemonsets
            if "DaemonSet" in resources_by_kind:
                await self._check_daemonsets_health(resources_by_kind["DaemonSet"])
                
        except Exception as e:
            self._logger.exception(f"Error checking resource health: {e}")
            
    async def _check_pods_health(self, pods: List[Tuple[str, str]]):
        """
        Check the health status of pods.
        
        Args:
            pods: List of (namespace, name) tuples for pods to check
        """
        try:
            # Group pods by namespace for more efficient querying
            pods_by_namespace = {}
            for namespace, name in pods:
                if namespace not in pods_by_namespace:
                    pods_by_namespace[namespace] = []
                pods_by_namespace[namespace].append(name)
                
            for namespace, pod_names in pods_by_namespace.items():
                try:
                    # Query all pods in this namespace
                    # Make sure to wait for the API call result
                    pod_list = await self._get_pods(namespace)
                    
                    # Create a lookup for quick matching
                    pod_status = {pod.metadata.name: pod.status for pod in pod_list.items}
                    
                    # Update health status for each pod we're tracking
                    for pod_name in pod_names:
                        resource_key = f"Pod/{namespace}/{pod_name}"
                        
                        if pod_name in pod_status:
                            status = pod_status[pod_name]
                            
                            # Consider a pod healthy if it's running and ready
                            phase = getattr(status, "phase", "")
                            
                            # Check container statuses for issues
                            container_statuses = getattr(status, "container_statuses", []) or []
                            
                            # Pod is healthy if:
                            # 1. Phase is "Running" 
                            # 2. All containers are ready
                            # 3. No containers are in CrashLoopBackOff, Error, or other bad states
                            is_healthy = (
                                phase == "Running" and
                                all(getattr(cs, "ready", False) for cs in container_statuses) and
                                not any(
                                    getattr(getattr(cs, "state", None), "waiting", None) is not None and
                                    getattr(getattr(cs, "state", None).waiting, "reason", "") in [
                                        "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                                        "ContainerCreating"
                                    ]
                                    for cs in container_statuses
                                )
                            )
                            
                            # Update resource state
                            self._resource_states[resource_key] = {
                                "healthy": is_healthy,
                                "checked_at": datetime.datetime.now(datetime.timezone.utc),
                                "status": phase
                            }
                        else:
                            # Pod not found - could be terminated/deleted
                            # Consider it "healthy" (i.e., not failing) to avoid false positives
                            # for pods that no longer exist
                            self._resource_states[resource_key] = {
                                "healthy": True,  # No current failures since it doesn't exist
                                "checked_at": datetime.datetime.now(datetime.timezone.utc),
                                "status": "NotFound"
                            }
                except Exception as e:
                    self._logger.exception(f"Error checking pods in namespace {namespace}: {e}")
                        
        except Exception as e:
            self._logger.exception(f"Error checking pods health: {e}")
            
    async def _check_nodes_health(self, nodes: List[Tuple[str, str]]):
        """
        Check the health status of nodes.
        
        Args:
            nodes: List of (namespace, name) tuples for nodes to check
        """
        try:
            # For nodes, namespace is usually ignored (nodes are cluster-wide)
            node_names = [name for _, name in nodes]
            
            # Get all nodes (more efficient than individual queries)
            # Make sure to wait for the API call result
            node_list = await self._get_nodes()
            
            # Create a lookup for quick matching
            node_statuses = {node.metadata.name: node.status for node in node_list.items}
            
            # Update health status for each node we're tracking
            for node_name in node_names:
                resource_key = f"Node/cluster/{node_name}"
                
                if node_name in node_statuses:
                    status = node_statuses[node_name]
                    
                    # Check node conditions
                    conditions = getattr(status, "conditions", []) or []
                    
                    # Node is healthy if:
                    # 1. Ready condition is True
                    # 2. No pressure conditions (Memory, Disk, PID) are True
                    ready_condition = next((c for c in conditions if c.type == "Ready"), None)
                    memory_pressure = next((c for c in conditions if c.type == "MemoryPressure"), None)
                    disk_pressure = next((c for c in conditions if c.type == "DiskPressure"), None)
                    pid_pressure = next((c for c in conditions if c.type == "PIDPressure"), None)
                    
                    is_healthy = (
                        ready_condition is not None and ready_condition.status == "True" and
                        (memory_pressure is None or memory_pressure.status != "True") and
                        (disk_pressure is None or disk_pressure.status != "True") and
                        (pid_pressure is None or pid_pressure.status != "True")
                    )
                    
                    # Update resource state
                    self._resource_states[resource_key] = {
                        "healthy": is_healthy,
                        "checked_at": datetime.datetime.now(datetime.timezone.utc),
                        "status": "Ready" if is_healthy else "NotReady"
                    }
                else:
                    # Node not found - could be removed from cluster
                    self._resource_states[resource_key] = {
                        "healthy": True,  # No current failures since it doesn't exist
                        "checked_at": datetime.datetime.now(datetime.timezone.utc),
                        "status": "NotFound"
                    }
                    
        except Exception as e:
            self._logger.exception(f"Error checking nodes health: {e}")
            
    async def _check_deployments_health(self, deployments: List[Tuple[str, str]]):
        """
        Check the health status of deployments.
        
        Args:
            deployments: List of (namespace, name) tuples for deployments to check
        """
        try:
            # Group deployments by namespace for more efficient querying
            deployments_by_namespace = {}
            for namespace, name in deployments:
                if namespace not in deployments_by_namespace:
                    deployments_by_namespace[namespace] = []
                deployments_by_namespace[namespace].append(name)
                
            apps_api = client.AppsV1Api(self._k8s_client)
            
            for namespace, deployment_names in deployments_by_namespace.items():
                try:
                    # Query all deployments in this namespace
                    deployment_list = await asyncio.to_thread(
                        apps_api.list_namespaced_deployment,
                        namespace=namespace
                    )
                    
                    # Create a lookup for quick matching
                    deployment_statuses = {
                        d.metadata.name: d.status for d in deployment_list.items
                    }
                    
                    # Update health status for each deployment we're tracking
                    for deployment_name in deployment_names:
                        resource_key = f"Deployment/{namespace}/{deployment_name}"
                        
                        if deployment_name in deployment_statuses:
                            status = deployment_statuses[deployment_name]
                            
                            # Deployment is healthy if:
                            # 1. Available replicas match desired replicas
                            # 2. No failed conditions
                            available = getattr(status, "available_replicas", 0) or 0
                            desired = getattr(status, "replicas", 0) or 0
                            
                            # Consider it healthy if at least 1 replica is available when desired > 0
                            # Some apps can function with reduced replicas
                            is_healthy = (
                                (desired == 0 or available > 0) and
                                (available >= desired * 0.5)  # At least 50% of desired replicas
                            )
                            
                            # Update resource state
                            self._resource_states[resource_key] = {
                                "healthy": is_healthy,
                                "checked_at": datetime.datetime.now(datetime.timezone.utc),
                                "status": f"{available}/{desired} available"
                            }
                        else:
                            # Deployment not found - could be deleted
                            self._resource_states[resource_key] = {
                                "healthy": True,  # No current failures since it doesn't exist
                                "checked_at": datetime.datetime.now(datetime.timezone.utc),
                                "status": "NotFound"
                            }
                except Exception as e:
                    self._logger.exception(f"Error checking deployments in namespace {namespace}: {e}")
                    
        except Exception as e:
            self._logger.exception(f"Error checking deployments health: {e}")
            
    async def _check_statefulsets_health(self, statefulsets: List[Tuple[str, str]]):
        """
        Check the health status of statefulsets.
        
        Args:
            statefulsets: List of (namespace, name) tuples for statefulsets to check
        """
        try:
            # Group statefulsets by namespace for more efficient querying
            statefulsets_by_namespace = {}
            for namespace, name in statefulsets:
                if namespace not in statefulsets_by_namespace:
                    statefulsets_by_namespace[namespace] = []
                statefulsets_by_namespace[namespace].append(name)
                
            apps_api = client.AppsV1Api(self._k8s_client)
            
            for namespace, statefulset_names in statefulsets_by_namespace.items():
                try:
                    # Query all statefulsets in this namespace
                    statefulset_list = await asyncio.to_thread(
                        apps_api.list_namespaced_stateful_set,
                        namespace=namespace
                    )
                    
                    # Create a lookup for quick matching
                    statefulset_statuses = {
                        s.metadata.name: s.status for s in statefulset_list.items
                    }
                    
                    # Update health status for each statefulset we're tracking
                    for statefulset_name in statefulset_names:
                        resource_key = f"StatefulSet/{namespace}/{statefulset_name}"
                        
                        if statefulset_name in statefulset_statuses:
                            status = statefulset_statuses[statefulset_name]
                            
                            # StatefulSet is healthy if:
                            # 1. Ready replicas match desired replicas
                            # 2. No failed conditions
                            ready = getattr(status, "ready_replicas", 0) or 0
                            desired = getattr(status, "replicas", 0) or 0
                            
                            # Consider it healthy if at least 1 replica is ready when desired > 0
                            # For StatefulSets, even partial availability is useful
                            is_healthy = (
                                (desired == 0 or ready > 0) and
                                (ready >= desired * 0.5)  # At least 50% of desired replicas
                            )
                            
                            # Update resource state
                            self._resource_states[resource_key] = {
                                "healthy": is_healthy,
                                "checked_at": datetime.datetime.now(datetime.timezone.utc),
                                "status": f"{ready}/{desired} ready"
                            }
                        else:
                            # StatefulSet not found - could be deleted
                            self._resource_states[resource_key] = {
                                "healthy": True,  # No current failures since it doesn't exist
                                "checked_at": datetime.datetime.now(datetime.timezone.utc),
                                "status": "NotFound"
                            }
                except Exception as e:
                    self._logger.exception(f"Error checking statefulsets in namespace {namespace}: {e}")
                    
        except Exception as e:
            self._logger.exception(f"Error checking statefulsets health: {e}")
            
    async def _check_daemonsets_health(self, daemonsets: List[Tuple[str, str]]):
        """
        Check the health status of daemonsets.
        
        Args:
            daemonsets: List of (namespace, name) tuples for daemonsets to check
        """
        try:
            # Group daemonsets by namespace for more efficient querying
            daemonsets_by_namespace = {}
            for namespace, name in daemonsets:
                if namespace not in daemonsets_by_namespace:
                    daemonsets_by_namespace[namespace] = []
                daemonsets_by_namespace[namespace].append(name)
                
            apps_api = client.AppsV1Api(self._k8s_client)
            
            for namespace, daemonset_names in daemonsets_by_namespace.items():
                try:
                    # Query all daemonsets in this namespace
                    daemonset_list = await asyncio.to_thread(
                        apps_api.list_namespaced_daemon_set,
                        namespace=namespace
                    )
                    
                    # Create a lookup for quick matching
                    daemonset_statuses = {
                        d.metadata.name: d.status for d in daemonset_list.items
                    }
                    
                    # Update health status for each daemonset we're tracking
                    for daemonset_name in daemonset_names:
                        resource_key = f"DaemonSet/{namespace}/{daemonset_name}"
                        
                        if daemonset_name in daemonset_statuses:
                            status = daemonset_statuses[daemonset_name]
                            
                            # DaemonSet is healthy if:
                            # 1. Current number matches desired number
                            # 2. No failed pods
                            current = getattr(status, "current_number_scheduled", 0) or 0
                            desired = getattr(status, "desired_number_scheduled", 0) or 0
                            ready = getattr(status, "number_ready", 0) or 0
                            
                            # Consider it healthy if at least 80% of nodes have it ready
                            # DaemonSets should run on all nodes, but some nodes might be cordoned
                            is_healthy = (
                                desired == 0 or 
                                (current == desired and ready >= desired * 0.8)
                            )
                            
                            # Update resource state
                            self._resource_states[resource_key] = {
                                "healthy": is_healthy,
                                "checked_at": datetime.datetime.now(datetime.timezone.utc),
                                "status": f"{ready}/{desired} ready"
                            }
                        else:
                            # DaemonSet not found - could be deleted
                            self._resource_states[resource_key] = {
                                "healthy": True,  # No current failures since it doesn't exist
                                "checked_at": datetime.datetime.now(datetime.timezone.utc),
                                "status": "NotFound"
                            }
                except Exception as e:
                    self._logger.exception(f"Error checking daemonsets in namespace {namespace}: {e}")
                    
        except Exception as e:
            self._logger.exception(f"Error checking daemonsets health: {e}")
            
    async def _process_resource_state_queue(self):
        """
        Process any pending resource state updates from the queue.
        """
        try:
            # Process at most 100 updates at once to avoid blocking too long
            for _ in range(min(100, self._resource_state_queue.qsize())):
                try:
                    resource_key, state = self._resource_state_queue.get_nowait()
                    self._resource_states[resource_key] = state
                    self._resource_state_queue.task_done()
                except asyncio.QueueEmpty:
                    break
        except Exception as e:
            self._logger.exception(f"Error processing resource state queue: {e}")
            
    async def _share_resource_states(self):
        """
        Share the current resource states with the detector.
        """
        try:
            # This method would update the detector's resource state tracking
            # We'll implement it when integrating with the detector
            # For now, just log the number of resources we're tracking
            self._logger.debug(f"Tracking health status of {len(self._resource_states)} resources")
            
            # Share with the detector through detector task
            detector_task = asyncio.current_task().get_name() if hasattr(asyncio.current_task(), "get_name") else None
            if detector_task and hasattr(self, "_detector") and self._detector is not None:
                self._detector._resource_states = self._resource_states.copy()
                
        except Exception as e:
            self._logger.exception(f"Error sharing resource states: {e}")
            
    # Helper methods to get resources (separated for testability)
    async def _get_pods(self, namespace="default"):
        """Get pods in the specified namespace."""
        try:
            core_api = client.CoreV1Api(self._k8s_client)
            # This method returns a coroutine that must be awaited
            pod_list = await core_api.list_namespaced_pod(namespace=namespace)
            # Ensure we have a response with items attribute
            if hasattr(pod_list, 'items'):
                return pod_list
            else:
                self._logger.error(f"Pod list response has no 'items' attribute: {type(pod_list)}")
                # Return an empty list as fallback
                return type('EmptyPodList', (), {'items': []})
        except Exception as e:
            self._logger.exception(f"Error getting pods in namespace {namespace}: {e}")
            # Return an empty list as fallback
            return type('EmptyPodList', (), {'items': []})
        
    async def _get_nodes(self):
        """Get all nodes in the cluster."""
        try:
            core_api = client.CoreV1Api(self._k8s_client)
            # This method returns a coroutine that must be awaited
            node_list = await core_api.list_node()
            # Ensure we have a response with items attribute
            if hasattr(node_list, 'items'):
                return node_list
            else:
                self._logger.error(f"Node list response has no 'items' attribute: {type(node_list)}")
                # Return an empty list as fallback
                return type('EmptyNodeList', (), {'items': []})
        except Exception as e:
            self._logger.exception(f"Error getting nodes: {e}")
            # Return an empty list as fallback
            return type('EmptyNodeList', (), {'items': []})
        
    def _parse_involved_object(self, event: dict) -> Tuple[str, str, str]:
        """
        Extract key information about the involved object from an event.
        
        Args:
            event: Raw Kubernetes event dictionary
            
        Returns:
            Tuple of (kind, namespace, name)
        """
        involved_object = event.get("involvedObject", {})
        kind = involved_object.get("kind", "")
        namespace = involved_object.get("namespace", "")
        name = involved_object.get("name", "")
        
        # For cluster-wide resources like nodes, use a special namespace
        if kind == "Node":
            namespace = "cluster"
            
        return kind, namespace, name
        
    def _update_resource_state_from_event(self, event: dict):
        """
        Update resource state tracking based on an event.
        
        Args:
            event: Raw Kubernetes event object (CoreV1Event or dict)
        """
        try:
            # Handle both dictionary and CoreV1Event objects
            if hasattr(event, 'to_dict'):
                # It's a CoreV1Event object, convert to dict first
                event_dict = event.to_dict()
                event_type = getattr(event, 'type', None)
                reason = getattr(event, 'reason', None)
                message = getattr(event, 'message', None)
                involved_object = event.involved_object if hasattr(event, 'involved_object') else None
            else:
                # It's already a dictionary
                event_dict = event
                event_type = event.get("type")
                reason = event.get("reason", "")
                message = event.get("message", "")
                involved_object = event.get("involvedObject", {})
            
            # Skip event processing if essential fields are missing
            if not event_type or not reason:
                return

            # Parse involved object safely
            if isinstance(involved_object, dict):
                kind = involved_object.get("kind", "")
                namespace = involved_object.get("namespace", "")
                name = involved_object.get("name", "")
            else:
                # It's a CoreV1Event involved_object
                kind = getattr(involved_object, "kind", "") if involved_object else ""
                namespace = getattr(involved_object, "namespace", "") if involved_object else ""
                name = getattr(involved_object, "name", "") if involved_object else ""
            
            if not kind or not name:
                return
                
            # Create a resource key for tracking
            resource_key = f"{kind}/{namespace or 'cluster'}/{name}"
            
            # Check if the event indicates the resource is now healthy
            # Ensure message is not None before trying to use it
            message_lower = message.lower() if message is not None else ""
            is_healthy = (
                # Normal events usually indicate healthy resources
                event_type == "Normal" or
                # Look for resolution messages in Warning events
                (event_type == "Warning" and message is not None and any(
                    term in message_lower for term in 
                    ["resolved", "succeeded", "recovered", "completed", "created", "started"]
                ))
            )
            
            # These reasons indicate the resource is definitely not healthy
            unhealthy_reasons = [
                "Failed", "FailedScheduling", "FailedMount", "FailedAttachVolume",
                "FailedCreatePodContainer", "FailedSync", "FailedValidation",
                "CrashLoopBackOff", "BackOff", "ImagePullBackOff", "ErrImagePull",
                "Unhealthy", "NodeNotReady", "NodeNotSchedulable"
            ]
            
            # Override for known unhealthy states
            if reason in unhealthy_reasons:
                is_healthy = False
                
            # Queue update to resource state
            self._resource_state_queue.put_nowait((
                resource_key,
                {
                    "healthy": is_healthy,
                    "checked_at": datetime.datetime.now(datetime.timezone.utc),
                    "last_event": reason,
                    "message": message if message is not None else ""
                }
            ))
            
        except Exception as e:
            self._logger.exception(f"Error updating resource state from event: {e}")
            
    def _should_skip_event_for_healthy_resource(self, raw_event: Union[Dict[str, Any], object]) -> bool:
        """
        Check if an event should be skipped because the resource is now healthy.
        
        Args:
            raw_event: The raw event (either dictionary or CoreV1Event object)
            
        Returns:
            True if the event should be skipped, False otherwise
        """
        try:
            # If raw_event is a CoreV1Event object, handle attributes differently
            is_core_v1_event = hasattr(raw_event, 'type') and hasattr(raw_event, 'involved_object')
            
            # Skip if event is not the type we care about for failures
            if is_core_v1_event:
                event_type = raw_event.type
            else:
                event_type = raw_event.get("type")
                
            if event_type != "Warning":
                return False
                
            # Get resource info
            if is_core_v1_event:
                involved_object = raw_event.involved_object if hasattr(raw_event, 'involved_object') else None
                if not involved_object:
                    return False
                    
                kind = getattr(involved_object, 'kind', None)
                namespace = getattr(involved_object, 'namespace', None)
                name = getattr(involved_object, 'name', None)
            else:
                involved_object = raw_event.get("involvedObject", {})
                if not involved_object:
                    return False
                    
                kind = involved_object.get("kind")
                namespace = involved_object.get("namespace")
                name = involved_object.get("name")
            
            if not kind or not name:
                logger.debug(f"Skipping health check for event with missing kind or name: {event_type} - {raw_event.get('reason') if not is_core_v1_event else getattr(raw_event, 'reason', 'Unknown')}")
                return False
                
            # Resource key for lookup
            resource_key = f"{kind}/{namespace or 'cluster'}/{name}"
            
            # Check if we have health info for this resource
            if resource_key not in self._resource_states:
                return False
                
            # Get resource state
            resource_state = self._resource_states.get(resource_key, {})
            
            # Skip if resource is healthy and event is older than 5 minutes
            is_healthy = resource_state.get("healthy", False)
            checked_at = resource_state.get("checked_at")
            
            if is_healthy and checked_at:
                # Get event timestamp
                if is_core_v1_event:
                    last_timestamp = getattr(raw_event, 'last_timestamp', None)
                    first_timestamp = getattr(raw_event, 'first_timestamp', None)
                else:
                    last_timestamp = raw_event.get("lastTimestamp")
                    first_timestamp = raw_event.get("firstTimestamp")
                
                if isinstance(last_timestamp, str):
                    try:
                        last_timestamp = datetime.datetime.fromisoformat(last_timestamp.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        last_timestamp = None
                        
                if isinstance(first_timestamp, str) and not last_timestamp:
                    try:
                        first_timestamp = datetime.datetime.fromisoformat(first_timestamp.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        first_timestamp = None
                        
                event_timestamp = last_timestamp or first_timestamp
                
                # If event is older than our resource health check, and resource is healthy, skip it
                if event_timestamp and event_timestamp < checked_at:
                    reason = getattr(raw_event, 'reason', None) if is_core_v1_event else raw_event.get("reason", "Unknown")
                    logger.debug(f"Skipping event {reason} for healthy resource {resource_key}. Event from {event_timestamp}, resource checked at {checked_at}")
                    return True
            
            return False
            
        except Exception as e:
            # If any error occurs during this check, log it and don't skip the event
            logger.warning(f"Error in _should_skip_event_for_healthy_resource: {e}")
            return False


# --- Legacy API for backward compatibility ---

async def watch_kubernetes_events(db: motor.motor_asyncio.AsyncIOMotorDatabase) -> AsyncGenerator[KubernetesEvent, None]:
    """
    Legacy function for backward compatibility. 
    Watches Kubernetes API for relevant events across all namespaces, persisting the resource_version.
    
    Args:
        db: MongoDB database for state persistence.
        
    Yields:
        KubernetesEvent objects matching the criteria.
    """
    logger.warning(
        "Using legacy watch_kubernetes_events function. "
        "Consider migrating to KubernetesEventWatcher class for better performance."
    )
    
    # Create a queue to pass events back
    event_queue = asyncio.Queue()
    
    # Create and start the watcher
    watcher = KubernetesEventWatcher(event_queue, db)
    
    try:
        await watcher.start()
        
        # Retrieve and yield events from the queue
        while True:
            try:
                event = await event_queue.get()
                yield event
                event_queue.task_done()
            except asyncio.CancelledError:
                logger.info("watch_kubernetes_events cancelled")
                break
    finally:
        await watcher.stop()


# Example usage (for testing or direct script execution):
if __name__ == "__main__":
    from kubewise.logging import setup_logging
    from kubewise.config import settings
    setup_logging()

    async def test_legacy_api():
        """Test the legacy API function"""
        mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
            str(settings.mongo_uri), serverSelectionTimeoutMS=5000
        )
        test_db = mongo_client[settings.mongo_db_name]
        
        event_count = 0
        try:
            logger.info("Testing legacy watch_kubernetes_events function")
            async for event in watch_kubernetes_events(test_db):
                print("-" * 50)
                print(f"Event {event_count + 1}:")
                print(f"  Type: {event.type}")
                print(f"  Reason: {event.reason}")
                print(f"  Object: {event.involved_object_kind}/{event.involved_object_namespace}/{event.involved_object_name}")
                print(f"  Message: {event.message}")
                print(f"  Source: {event.source_component}")
                print("-" * 50)
                
                event_count += 1
                if event_count >= 5:  # Limit test to 5 events
                    break
        except Exception as e:
            logger.exception(f"Error in legacy API test: {e}")
        finally:
            mongo_client.close()

    async def test_watcher_class():
        """Test the new KubernetesEventWatcher class"""
        mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
            str(settings.mongo_uri), serverSelectionTimeoutMS=5000
        )
        test_db = mongo_client[settings.mongo_db_name]
        
        # Create a queue to receive events
        events_queue = asyncio.Queue()
        
        # Create and start the watcher
        watcher = KubernetesEventWatcher(events_queue, test_db)
        try:
            await watcher.start()
            
            logger.info("Waiting for events to be processed...")
            event_count = 0
            start_time = time.monotonic()
            
            # Process events from the queue with a timeout
            while time.monotonic() - start_time < 60:  # Run for up to 60 seconds
                try:
                    # Wait for an event with a timeout
                    event = await asyncio.wait_for(events_queue.get(), timeout=5.0)
                    
                    print("-" * 50)
                    print(f"Event {event_count + 1}:")
                    print(f"  Type: {event.type}")
                    print(f"  Reason: {event.reason}")
                    print(f"  Object: {event.involved_object_kind}/{event.involved_object_namespace}/{event.involved_object_name}")
                    print(f"  Message: {event.message}")
                    print(f"  Source: {event.source_component}")
                    print("-" * 50)
                    
                    events_queue.task_done()
                    event_count += 1
                    
                    if event_count >= 5:  # Limit test to 5 events
                        break
                        
                except asyncio.TimeoutError:
                    logger.info("No events received within timeout")
                    if event_count > 0:
                        break  # Exit if we've seen some events already
            
            logger.info(f"Watcher stats: processed={watcher._watch_restarts}, restarts={watcher._watch_restarts}")
            
        finally:
            await watcher.stop()
            mongo_client.close()

    async def main():
        """Run both tests sequentially"""
        logger.info("=== Testing KubernetesEventWatcher Class ===")
        await test_watcher_class()
        
        logger.info("\n=== Testing Legacy API ===")
        await test_legacy_api()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Tests stopped by user")
