import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import datetime # Needed for timezone aware datetime
import time # For rate limiting check
from typing import Optional, Set, Dict, Tuple # Added Tuple for helper return
import json

import httpx
import motor.motor_asyncio
from fastapi import FastAPI
import aiohttp # Import aiohttp
from kubernetes_asyncio import client, config # Import kubernetes client, config
from kubernetes_asyncio.client import rest # Import rest from kubernetes_asyncio.client
from loguru import logger
from prometheus_client import Gauge # Import Gauge for queue metrics
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn # Import rich progress components
from rich import box # Import rich box styles


# Import deps here to use get_settings, but avoid circular imports later
from kubewise.api import routers
# Import the watcher class directly
from kubewise.collector.k8s_events import KubernetesEventWatcher, load_k8s_config
from kubewise.collector import prometheus
from kubewise.config import settings # Use settings directly
# Logging setup is now done automatically on import in logging.py
# from kubewise.logging import setup_logging
from kubewise.models.detector import OnlineAnomalyDetector, SequentialAnomalyDetector, detection_loop
from kubewise.models import AnomalyRecord, KubernetesEvent, MetricPoint
from kubewise.remediation import engine, planner
# Imports for Gemini Agent
from pydantic_ai import Agent
# Corrected imports based on pydantic-ai docs
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider
# Import AppContext from the new context module
from kubewise.api.context import AppContext
from prometheus_client import REGISTRY # Import REGISTRY

# Import the dependency factory module
from kubewise.api.factory import (
    initialize_app_context, 
    create_prometheus_fetcher
)

# Custom JSON encoder to handle datetime serialization
class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return super().default(obj)

# --- Constants ---
# Remediation cooldown is now configured through settings.remediation_cooldown_seconds

ASCII_BANNER = """
██╗  ██╗██╗   ██╗██████╗ ███████╗██╗    ██╗██╗███████╗███████╗  
██║ ██╔╝██║   ██║██╔══██╗██╔════╝██║    ██║██║██╔════╝██╔════╝  
█████╔╝ ██║   ██║██████╔╝█████╗  ██║ █╗ ██║██║███████╗█████╗    
██╔═██╗ ██║   ██║██╔══██╗██╔══╝  ██║███╗██║██║╚════██║██╔══╝    
██║  ██╗╚██████╔╝██████╔╝███████╗╚███╔███╔╝██║███████║███████╗  
╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝ ╚══╝╚══╝ ╚═╝╚══════╝╚══════╝  
"""

async def fetch_kubernetes_nodes(k8s_client: client.ApiClient) -> list[str]:
    """Fetch the list of Kubernetes node names."""
    try:
        await config.load_kube_config()  # Use kubeconfig if running locally
    except Exception:
        await config.load_incluster_config()  # Use in-cluster config if running inside a Pod
    v1 = client.CoreV1Api(api_client=k8s_client) # Use the provided client instance
    nodes = await v1.list_node()
    return [node.metadata.name for node in nodes.items]


# --- Startup and Shutdown Logic ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Async context manager for FastAPI lifespan events.
    Handles startup and shutdown logic.
    """
    # === Startup ===
    logger.info("Starting KubeWise lifespan...")

    console = Console()

    # 1. Show KubeWise banner
    console.clear()
    console.print(Panel.fit(ASCII_BANNER, title="[bold green]KubeWise", subtitle="AI-Powered Kubernetes Guardian 🚀", style="bold cyan", box=box.DOUBLE))

    # 2. Simulated loading steps + Fetch nodes dynamically
    console.rule("[bold green]Starting KubeWise Server...")

    # Initialize the application context using our factory
    app_context = await initialize_app_context()
    app.state.app_context = app_context

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        # Display progress
        task = progress.add_task("[yellow]Connecting to Kubernetes cluster...", total=None)
        
        # Fetch cluster nodes if Kubernetes client is available
        progress.update(task, description="[cyan]Fetching cluster nodes...")
        try:
            # Pass the shared k8s_api_client from the context
            if app_context.k8s_api_client is not None:
                nodes = await fetch_kubernetes_nodes(app_context.k8s_api_client)
                logger.info(f"Fetched nodes: {nodes}")
            else:
                nodes = []
                logger.error("Cannot fetch nodes: Kubernetes client not available")
        except Exception as e:
            nodes = []
            console.print(f"[bold red]Failed to fetch nodes: {e}")
            logger.error(f"Error fetching nodes: {e}")

        if nodes:
            console.print("\n[bold green]🖥️  Connected Nodes:")
            for node in nodes:
                console.print(f"   - [cyan]{node}[/cyan]")
        else:
            console.print("[bold yellow]⚠️  No nodes found or unable to fetch!")

        # Initialize Prometheus
        progress.update(task, description="[magenta]Initializing Prometheus Metrics Scraper...")
        
        # Initialize anomaly detection
        progress.update(task, description="[blue]Initializing Anomaly Detection Models...")
        
        # Initialize remediation
        progress.update(task, description="[green]Setting up Remediation Agents...")
        
        # Initialize API endpoints
        progress.update(task, description="[green]Finalizing API endpoints...")

    console.rule("[bold blue]KubeWise Server Started Successfully 🎯")

    # Display settings
    settings_display = {
        "Log Level": settings.log_level,
        "Prometheus URL": str(settings.prom_url),
        "Mongo DB Name": settings.mongo_db_name,
        "Anomaly Thresholds": str(settings.anomaly_thresholds),
        "Gemini Model ID": settings.gemini_model_id,
        "Gemini API Key": f"{settings.gemini_api_key.get_secret_value()[:4]}... (masked)",
    }
    settings_text = "\n".join(f"[cyan]{key}:[/] [yellow]{value}[/]" for key, value in settings_display.items())

    console.print(
        Panel(
            settings_text,
            title="[bold green]KubeWise Configuration[/]",
            border_style="green",
            expand=False,
        )
    )
    logger.info("KubeWise startup sequence initiated.")

    # --- Start Background Tasks & Watchers ---
    
    # Start event watcher if available
    if app_context.k8s_event_watcher is not None:
        await app_context.k8s_event_watcher.start()
        logger.info("Kubernetes event watcher started.")
    else:
        logger.warning("Kubernetes event watcher not available - event monitoring disabled.")
    
    # Start Prometheus metrics fetcher if available
    try:
        prom_fetcher = await create_prometheus_fetcher(app_context)
        if prom_fetcher is not None:
            await prom_fetcher.start()
            logger.info("Prometheus metrics fetcher started successfully.")
        else:
            logger.warning("Prometheus metrics fetcher not available. Metrics collection disabled.")
    except Exception as e:
        logger.error(f"Failed to start Prometheus metrics fetcher: {e}")
        logger.warning("Prometheus metrics fetching disabled.")
    
    # Start detection loop
    if app_context.anomaly_detector is not None and app_context.db is not None:
        try:
            detector_task = asyncio.create_task(
                detection_loop(
                    detector=app_context.anomaly_detector,
                    metric_queue=app_context.metric_queue,
                    event_queue=app_context.event_queue,
                    remediation_queue=app_context.remediation_queue,
                    db=app_context.db,
                    app_context=app_context,
                ),
                name="anomaly_detection_loop"
            )
            app_context.processing_tasks.add(detector_task)
            logger.info("Anomaly detection loop started.")
        except Exception as detector_err:
            logger.exception(f"Failed to start detection loop: {detector_err}")
            logger.warning("Anomaly detection disabled.")
    else:
        logger.warning("Anomaly detector or database not available - anomaly detection disabled.")
    
    # Start remediation loop
    if app_context.db is not None and app_context.k8s_api_client is not None and app_context.remediation_queue is not None:
        try:
            from kubewise.api.factory import create_planner_dependencies
            planner_deps = await create_planner_dependencies(app_context)
            
            remediation_task = asyncio.create_task(
                run_remediation_trigger(app_context),
                name="remediation_trigger_loop"
            )
            app_context.processing_tasks.add(remediation_task)
            logger.info("Remediation trigger loop started.")
        except Exception as remediation_err:
            logger.exception(f"Failed to start remediation loop: {remediation_err}")
            logger.warning("Automatic remediation disabled.")
    else:
        logger.warning("Required dependencies not available - automatic remediation disabled.")
    
    # Start queue metrics updater
    try:
        # Create Prometheus gauges for queue sizes
        metric_queue_gauge = Gauge(
            "kubewise_metric_queue_size", 
            "Current number of items in the metric queue"
        )
        event_queue_gauge = Gauge(
            "kubewise_event_queue_size", 
            "Current number of items in the event queue"
        )
        remediation_queue_gauge = Gauge(
            "kubewise_remediation_queue_size", 
            "Current number of items in the remediation queue"
        )
        
        metrics_task = asyncio.create_task(
            run_queue_metrics_updater(
                app_context,
                metric_queue_gauge,
                event_queue_gauge,
                remediation_queue_gauge
            ),
            name="queue_metrics_updater"
        )
        app_context.processing_tasks.add(metrics_task)
        logger.info("Queue metrics updater started.")
    except Exception as metrics_err:
        logger.exception(f"Failed to start queue metrics updater: {metrics_err}")
    
    # Start detector state saver
    if app_context.anomaly_detector is not None:
        try:
            saver_task = asyncio.create_task(
                run_detector_state_saver(app_context),
                name="detector_state_saver"
            )
            app_context.processing_tasks.add(saver_task)
            logger.info("Detector state saver started.")
        except Exception as saver_err:
            logger.exception(f"Failed to start detector state saver: {saver_err}")
    
    logger.info("KubeWise server startup completed.")

    # --- Yield to HTTP middleware ---
    yield

    # === Shutdown ===
    logger.info("Shutting down KubeWise server...")

    # Cancel all background tasks
    if app_context.processing_tasks:
        await cancel_and_wait(app_context.processing_tasks, "Processing tasks", 10.0)
    
    if app_context.datasource_tasks:
        await cancel_and_wait(app_context.datasource_tasks, "Data source tasks", 10.0)
    
    # Stop event watcher if available
    if app_context.k8s_event_watcher is not None:
        await app_context.k8s_event_watcher.stop()
        logger.info("Kubernetes event watcher stopped.")
    
    # Save detector state before shutdown
    if app_context.anomaly_detector is not None:
        try:
            await app_context.anomaly_detector.save_all_state()
            logger.info("Detector state saved.")
        except Exception as save_err:
            logger.error(f"Failed to save detector state: {save_err}")
    
    # Close Kubernetes client
    if app_context.k8s_api_client:
        await app_context.k8s_api_client.close()
        logger.info("Kubernetes client closed.")
    
    # Final cleanup before exit
    logger.info("Performing final application cleanup...")
    
    # Close the HTTP client and any aiohttp sessions
    if app_context.http_client is not None:
        await app_context.http_client.aclose()
        logger.info("HTTP client closed.")
        
    # Close any aiohttp session if present
    if app_context.aiohttp_session is not None:
        await app_context.aiohttp_session.close()
        logger.info("aiohttp session closed.")
        
    # Close MongoDB connection
    if app_context.mongo_client is not None:
        app_context.mongo_client.close()
        logger.info("MongoDB client closed.")
    
    logger.info("KubeWise server shutdown completed.")


# --- Helper Functions ---

def get_entity_id_from_anomaly(anomaly: AnomalyRecord) -> Optional[str]:
    """Helper to extract a consistent entity ID (e.g., namespace/pod_name) from an anomaly record."""
    # AnomalyRecord already contains the entity_id field, so we can use it directly
    if anomaly.entity_id:
        return anomaly.entity_id
    
    # Fallback: construct from namespace and name if entity_id not available
    if anomaly.namespace and anomaly.name:
        return f"{anomaly.namespace}/{anomaly.name}"
    
    logger.warning(f"Could not determine entity ID for rate limiting from anomaly: {anomaly.id}")
    return None


def is_likely_false_positive(anomaly: AnomalyRecord) -> bool:
    """
    Helper to determine if an anomaly is likely a false positive based on heuristics.
    Returns True if the anomaly appears to be a false positive.
    """
    # Check for large discrepancy between HST and River scores
    # If HST score is high but River score is very low, it might be a false positive
    # Use thresholds from settings
    hst_threshold = settings.fp_hst_score_threshold
    river_threshold = settings.fp_river_score_threshold
    mem_threshold = settings.fp_system_mem_mib_threshold
    diff_threshold = settings.fp_score_difference_threshold # Load the new difference threshold

    # Heuristic 1: High HST score AND Low River score
    high_hst_low_river = anomaly.hst_score > hst_threshold and anomaly.river_score < river_threshold
    # Heuristic 2: High HST score AND Large difference between HST and River scores
    high_hst_large_diff = anomaly.hst_score > hst_threshold and (anomaly.hst_score - anomaly.river_score) > diff_threshold

    # If either heuristic triggers, log and potentially apply system namespace checks
    if high_hst_low_river or high_hst_large_diff:
        log_reason = []
        if high_hst_low_river:
            log_reason.append(f"high HST ({anomaly.hst_score:.3f} > {hst_threshold}) and low River ({anomaly.river_score:.3f} < {river_threshold})")
        if high_hst_large_diff:
             log_reason.append(f"high HST ({anomaly.hst_score:.3f} > {hst_threshold}) and large score diff ({anomaly.hst_score - anomaly.river_score:.3f} > {diff_threshold})")

        logger.info(f"Potential false positive for {anomaly.entity_id}: Triggered by ({' or '.join(log_reason)})")

        # For system namespaces, apply stricter memory check if it's a memory anomaly
        # Use the configurable list from settings
        if anomaly.namespace in settings.system_namespaces:
            # For memory usage anomalies in system pods, check if it's within reasonable range (using standardized MiB metric)
            if anomaly.metric_name == "pod_memory_working_set_mib": # Use the new standardized metric name
                # Value is already in MiB
                memory_mib = anomaly.metric_value # Value is already in MiB
                # If memory usage is below the configured threshold for system pods, likely normal behavior
                if memory_mib < mem_threshold:
                    logger.info(f"System pod {anomaly.entity_id} memory usage ({memory_mib:.2f} MiB < {mem_threshold} MiB) "
                               f"appears normal despite high anomaly score. Marking as false positive.")
                    return True
    
    return False


# --- Background Task Runner Functions (Modified to accept AppContext) ---

# Removed run_k8s_event_watcher as the KubernetesEventWatcher class handles its own loop.

async def run_prometheus_poller(ctx: AppContext):
    """
    Runs the Prometheus poller using the PrometheusFetcher class.
    The fetcher manages its own polling loop and puts metrics onto the queue.
    """
    logger.info("Starting Prometheus poller task...")
    if ctx.http_client is None:
        logger.error("HTTP client not available in context. Exiting poller task.")
        return

    # Instantiate the PrometheusFetcher with the shared client and metric queue
    # Instantiate the PrometheusFetcher with the shared client and metric queue
    fetcher = prometheus.PrometheusFetcher(
        metrics_queue=ctx.metric_queue,
        prometheus_url=str(settings.prom_url), # Pass URL explicitly from settings
        metrics_queries=settings.prom_queries, # Pass queries explicitly from settings
        poll_interval=settings.prom_queries_poll_interval, # Use interval from settings
    )

    try:
        # Start the fetcher's internal polling loop
        await fetcher.start()
        logger.info("PrometheusFetcher started.")

        # Keep this task alive while the fetcher runs
        # The fetcher's internal loop will handle the polling and sleeping
        while fetcher._is_running:
             await asyncio.sleep(1) # Sleep briefly to yield control

    except asyncio.CancelledError:
        logger.info("Prometheus poller task cancelled.")
    except Exception as e:
        logger.exception(f"Prometheus poller task failed: {e}")
    finally:
        # Ensure the fetcher is stopped when this task ends
        await fetcher.stop()
        logger.info("PrometheusFetcher stopped.")

# Renamed from detection_loop to avoid conflict with imported name
async def run_detection_loop(ctx: AppContext):
     """Runs the anomaly detection loop using resources from AppContext."""
     logger.info("Starting anomaly detection loop task...")
     if ctx.anomaly_detector is None: # Explicit None check
         logger.error("Anomaly detector not available in context. Exiting detection loop task.")
         return
     # Call the original detection_loop function, passing queues from context
     await detection_loop(
         detector=ctx.anomaly_detector,
         metric_queue=ctx.metric_queue,
         event_queue=ctx.event_queue,
         remediation_queue=ctx.remediation_queue,
         db=ctx.db, # Pass the database client
         app_context=ctx  # Pass app context for metrics recording
     )


async def run_remediation_trigger(ctx: AppContext):
    """Continuously process anomalies from the remediation queue using resources from AppContext."""
    logger.info("Starting remediation trigger task...")
    if ctx.db is None or ctx.k8s_api_client is None or ctx.anomaly_detector is None: # Explicit None checks
         logger.error("Required resources (DB, K8s client, Detector) not available in context. Exiting remediation task.")
         return
    # Gemini agent check happens later

    # Flag to track if we're in shutdown mode
    in_shutdown_mode = False
    shutdown_start_time = None
    max_shutdown_wait = 30.0  # Maximum time to wait during graceful shutdown

    while True:
        try:
            # Use timeout to periodically check for cancellation
            timeout = 0.5 if in_shutdown_mode else None
            try:
                anomaly_record = await asyncio.wait_for(ctx.remediation_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                # If we're in shutdown mode and queue is empty or timeout expired, exit
                if in_shutdown_mode:
                    time_in_shutdown = time.time() - shutdown_start_time
                    if ctx.remediation_queue.empty() or time_in_shutdown > max_shutdown_wait:
                        logger.info(f"Remediation queue processor shutting down after {time_in_shutdown:.1f}s grace period. Queue empty: {ctx.remediation_queue.empty()}")
                        break
                # Otherwise, check for cancellation and continue waiting
                continue
            # Bind anomaly_id to logger context for this processing block
            anomaly_id_str = str(anomaly_record.id) # Use consistent string ID
            with logger.contextualize(anomaly_id=anomaly_id_str):
                logger.info(f"Processing anomaly from remediation queue.")
                
                # --- False Positive Check ---
                # Check if this anomaly is likely a false positive before proceeding
                if is_likely_false_positive(anomaly_record):
                    logger.warning(f"Skipping remediation for entity '{anomaly_record.entity_id}': Likely false positive.")
                    # Update status to indicate false positive
                    await ctx.db["anomalies"].update_one(
                        {"_id": anomaly_record.id},
                        {"$set": {"remediation_status": "skipped_false_positive", 
                                  "remediation_error": "Anomaly identified as likely false positive based on heuristics"}}
                    )
                    
                    # Record false positive in metrics
                    entity_type = anomaly_record.entity_type or "unknown"
                    namespace = "unknown"
                    if "/" in anomaly_record.entity_id:
                        namespace = anomaly_record.entity_id.split("/")[0]
                    ctx.record_false_positive(entity_type=entity_type, namespace=namespace)
                    
                    ctx.remediation_queue.task_done()  # Mark task as done
                    continue  # Skip to the next anomaly
                
                # --- Early Resource Existence Check ---
                # Check if the resource still exists before proceeding with planning
                entity_id = get_entity_id_from_anomaly(anomaly_record)
                if entity_id and ctx.k8s_api_client is not None:
                    try:
                        # Parse namespace and name from entity_id
                        if "/" in entity_id:
                            namespace, name = entity_id.split("/", 1)
                            entity_exists = True
                            
                            # Create API clients
                            core_v1_api = client.CoreV1Api(ctx.k8s_api_client)
                            apps_v1_api = client.AppsV1Api(ctx.k8s_api_client)
                            
                            # Check existence based on entity type
                            entity_type = anomaly_record.entity_type.lower() if anomaly_record.entity_type else "unknown"
                            logger.info(f"Early check: Verifying if {entity_type} '{namespace}/{name}' exists before planning...")
                            
                            if entity_type == "pod":
                                try:
                                    await core_v1_api.read_namespaced_pod(name=name, namespace=namespace)
                                except client.ApiException as e:
                                    if e.status == 404:
                                        entity_exists = False
                                        logger.info(f"Early check: Pod '{namespace}/{name}' not found, marking as auto-resolved.")
                            
                            elif entity_type == "deployment" or entity_type == "deploy":
                                try:
                                    await apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
                                except client.ApiException as e:
                                    if e.status == 404:
                                        entity_exists = False
                                        logger.info(f"Early check: Deployment '{namespace}/{name}' not found, marking as auto-resolved.")
                            
                            elif entity_type == "service" or entity_type == "svc":
                                try:
                                    await core_v1_api.read_namespaced_service(name=name, namespace=namespace)
                                except client.ApiException as e:
                                    if e.status == 404:
                                        entity_exists = False
                                        logger.info(f"Early check: Service '{namespace}/{name}' not found, marking as auto-resolved.")
                            
                            elif entity_type == "statefulset" or entity_type == "sts":
                                try:
                                    await apps_v1_api.read_namespaced_stateful_set(name=name, namespace=namespace)
                                except client.ApiException as e:
                                    if e.status == 404:
                                        entity_exists = False
                                        logger.info(f"Early check: StatefulSet '{namespace}/{name}' not found, marking as auto-resolved.")
                            
                            elif entity_type == "daemonset" or entity_type == "ds":
                                try:
                                    await apps_v1_api.read_namespaced_daemon_set(name=name, namespace=namespace)
                                except client.ApiException as e:
                                    if e.status == 404:
                                        entity_exists = False
                                        logger.info(f"Early check: DaemonSet '{namespace}/{name}' not found, marking as auto-resolved.")
                            
                            elif entity_type == "node":
                                try:
                                    await core_v1_api.read_node(name=name)
                                except client.ApiException as e:
                                    if e.status == 404:
                                        entity_exists = False
                                        logger.info(f"Early check: Node '{name}' not found, marking as auto-resolved.")
                            
                            # If entity doesn't exist, mark as auto-resolved and skip remediation
                            if not entity_exists:
                                # Update anomaly status
                                await ctx.db["anomalies"].update_one(
                                    {"_id": anomaly_record.id},
                                    {"$set": {
                                        "remediation_status": "auto_resolved", 
                                        "remediation_error": f"{entity_type.capitalize()} '{namespace}/{name}' no longer exists, considering issue resolved."
                                    }}
                                )
                                # Mark task as done and skip to next anomaly
                                ctx.remediation_queue.task_done()
                                continue
                    except Exception as e:
                        logger.error(f"Early check: Error verifying entity existence: {e}")
                        # Continue with remediation despite the error in verification
                
                # --- Remediation Rate Limiting Check ---
                proceed_with_remediation = True
                now = datetime.datetime.now(datetime.timezone.utc) # Get current time once for potential update later
                if entity_id and ctx.anomaly_detector is not None: # Explicit None check
                    # Access entity state to determine resource type
                    entity_state = ctx.anomaly_detector._entity_states.setdefault(entity_id, {})
                    last_remediation_time = entity_state.get("last_remediation_time")

                    if last_remediation_time:
                        time_since_last = (now - last_remediation_time).total_seconds()
                        # Use the configurable cooldown period from settings
                        cooldown_period = settings.remediation_cooldown_seconds
                        if time_since_last < cooldown_period:
                            logger.warning(f"Skipping remediation for entity '{entity_id}': Cooldown active "
                                           f"({time_since_last:.1f}s < {cooldown_period}s).")
                            # Update status to skipped due to cooldown
                            await ctx.db["anomalies"].update_one(
                                {"_id": anomaly_record.id},
                                {"$set": {"remediation_status": "skipped_cooldown", "remediation_error": f"Cooldown active ({time_since_last:.1f}s)"}}
                            )
                            proceed_with_remediation = False
                        # else: Cooldown expired, proceed.
                    # else: No previous remediation, proceed.

                if not proceed_with_remediation:
                    ctx.remediation_queue.task_done() # Mark skipped task as done
                    continue # Skip to the next anomaly in the queue

                # --- Proceed with Planning and Execution ---
                # 1. Generate Plan (Gemini-first)
                generated_plan = None
                if ctx.gemini_agent is not None: # Explicit None check
                    try:
                        # Pass agent and db to the planner function
                        from kubewise.api.factory import create_planner_dependencies
                        planner_deps = await create_planner_dependencies(ctx)
                        generated_plan = await planner.generate_remediation_plan(
                            anomaly_record=anomaly_record,
                            dependencies=planner_deps,
                            agent=ctx.gemini_agent
                        )
                        logger.info(f"Gemini planner generated plan: \n{json.dumps(generated_plan.model_dump(), indent=2, cls=DateTimeEncoder)}")
                    except Exception as plan_err:
                        logger.exception(f"Error during Gemini plan generation: {plan_err}")
                        # Update status to reflect planning error
                        await ctx.db["anomalies"].update_one(
                             {"_id": anomaly_record.id},
                             {"$set": {"remediation_status": "planning_failed", "remediation_error": f"Gemini planning error: {plan_err}"}}
                         )
                        generated_plan = None # Ensure plan is None on error

                # Fallback to static plan if Gemini failed or didn't produce actions
                if not generated_plan or not generated_plan.actions:
                    if ctx.gemini_agent is not None:
                         logger.warning("Gemini plan was empty or failed, falling back to static planner.")
                    else:
                         logger.info("Gemini agent not available, using static planner.")
                    # Load static plan (assuming it doesn't raise exceptions easily)
                    from kubewise.api.factory import create_planner_dependencies
                    planner_deps = await create_planner_dependencies(ctx)
                    generated_plan = await planner.load_static_plan(
                        anomaly_record,
                        planner_deps.db,
                        planner_deps.k8s_client
                    )
                    logger.info(f"Static planner loaded plan: \n{json.dumps(generated_plan.model_dump(), indent=2, cls=DateTimeEncoder) if generated_plan else 'None'}")


                # 2. Execute Plan (if any plan exists with actions)
                if generated_plan and generated_plan.actions:
                    logger.info(f"Generated remediation plan with {len(generated_plan.actions)} action(s).")
                    
                    # --- Plan Validation ---
                    # Check if plan has validation fields (backward compatibility)
                    has_validation_fields = hasattr(generated_plan, 'validation_status')
                    
                    # For plans without validation fields, proceed directly to execution
                    # For plans with validation fields, validate according to validation_status
                    if has_validation_fields and generated_plan.validation_status == "pending":
                        logger.info(f"Validating remediation plan for anomaly {anomaly_id_str}...")
                        
                        # First, update status to indicate validation in progress
                        await ctx.db["anomalies"].update_one(
                            {"_id": anomaly_record.id}, 
                            {"$set": {"remediation_status": "validating"}}
                        )
                        
                        # Execute a dry run to assess plan safety
                        if hasattr(generated_plan, 'requires_dry_run') and generated_plan.requires_dry_run:
                            logger.info(f"Performing dry run to validate plan safety...")
                            
                            dry_run_success = await engine.execute_remediation_plan(
                                plan=generated_plan,
                                anomaly_record=anomaly_record,
                                dependencies=planner_deps,
                                app_context=ctx,
                                dry_run=True  # Execute in dry run mode
                            )
                            
                            # Update plan with validation results based on the dry run
                            safety_score = getattr(generated_plan, 'safety_score', None)
                            if dry_run_success and (safety_score is None or safety_score >= 0.8):
                                generated_plan.validation_status = "validated"
                                generated_plan.validation_details = f"Plan validated via dry run with safety score {safety_score or 0.0:.2f}"
                                logger.info(f"Plan validated successfully. Safety score: {safety_score or 0.0:.2f}")
                            else:
                                generated_plan.validation_status = "rejected"
                                generated_plan.validation_details = f"Plan rejected based on dry run results. Safety score: {safety_score or 0.0:.2f}"
                                logger.warning(f"Plan validation failed. Safety score: {safety_score or 0.0:.2f}")
                                
                                # Update anomaly record with rejected status
                                await ctx.db["anomalies"].update_one(
                                    {"_id": anomaly_record.id},
                                    {"$set": {
                                        "remediation_status": "validation_failed",
                                        "remediation_error": generated_plan.validation_details,
                                        "remediation_plan": generated_plan.model_dump()
                                    }}
                                )
                                
                                # Skip execution since validation failed
                                ctx.remediation_queue.task_done()
                                continue
                        else:
                            # If dry run not required, mark as validated
                            generated_plan.validation_status = "validated"
                            generated_plan.validation_details = "Plan validated (dry run not required)"
                    
                    # Only proceed with validated plans if validation is required
                    should_execute = not has_validation_fields or generated_plan.validation_status == "validated"
                    
                    if should_execute:
                        logger.info(f"Executing remediation plan...")
                        
                        # Use existing planner_deps to provide dependencies
                        execution_success = await engine.execute_remediation_plan(
                            plan=generated_plan,
                            anomaly_record=anomaly_record,
                            dependencies=planner_deps,
                            app_context=ctx,  # Pass context for metrics
                            dry_run=False     # Ensure not in dry run mode
                        )
                        
                        # Update last remediation time in state *only if execution was successful*
                        if execution_success and entity_id and ctx.anomaly_detector is not None:
                            # Use the 'now' timestamp captured earlier
                            ctx.anomaly_detector._entity_states[entity_id]["last_remediation_time"] = now
                            logger.info(f"Updated last remediation timestamp for entity '{entity_id}'.")
                    elif has_validation_fields:
                        logger.warning(f"Skipping execution: Plan validation status is '{generated_plan.validation_status}'")
                        
                        # Update anomaly record to indicate we're skipping due to validation
                        validation_details = getattr(generated_plan, 'validation_details', "No validation details available")
                        await ctx.db["anomalies"].update_one(
                            {"_id": anomaly_record.id},
                            {"$set": {
                                "remediation_status": "validation_failed", 
                                "remediation_error": f"Validation status: {generated_plan.validation_status}. Details: {validation_details}"
                            }}
                        )
                elif generated_plan: # Plan exists but has no actions
                    logger.info(f"Final remediation plan has no actions. Skipping execution.")
                    # Update status only if it wasn't already planning_failed
                    await ctx.db["anomalies"].update_one(
                        {"_id": anomaly_record.id, "remediation_status": {"$ne": "planning_failed"}},
                        {"$set": {"remediation_status": "completed", "remediation_error": "No actions generated by planner(s)."}}
                    )
                else: # No plan could be generated by either method
                    logger.error(f"Skipping execution as no plan could be generated.")
                    # Status should have been updated within the planner function

            ctx.remediation_queue.task_done()

        except asyncio.CancelledError:
            logger.info("Remediation trigger task received cancellation. Entering graceful shutdown.")
            in_shutdown_mode = True
            shutdown_start_time = time.time()
            # If the queue is already empty, exit immediately
            if ctx.remediation_queue.empty():
                logger.info("Remediation queue empty. Exiting immediately.")
            break
            logger.info(f"Waiting up to {max_shutdown_wait}s to process remaining {ctx.remediation_queue.qsize()} items...")
            continue  # Continue the loop, but in shutdown mode now
        except Exception as e:
            # Log exception with bound context if available
            logger.exception(f"Remediation trigger task failed processing an anomaly")
            if 'anomaly_record' in locals() and hasattr(anomaly_record.id, 'id'): # Check if anomaly_record and id exist
                 logger.error(f"Marking task done for anomaly {anomaly_record.id} despite error.")
                 if not ctx.remediation_queue.empty():
                     try: ctx.remediation_queue.task_done()
                     except ValueError: pass
            await asyncio.sleep(5)


async def run_queue_metrics_updater(
    ctx: AppContext,
    metric_gauge: Gauge,
    event_gauge: Gauge,
    remediation_gauge: Gauge,
    interval_seconds: int = 5
):
    """Periodically update Prometheus gauges for queue sizes."""
    logger.info(f"Starting queue metrics updater task (interval: {interval_seconds}s)...")
    while True:
        try:
            metric_qsize = ctx.metric_queue.qsize()
            event_qsize = ctx.event_queue.qsize()
            remediation_qsize = ctx.remediation_queue.qsize()

            # Use the passed gauge objects
            metric_gauge.set(metric_qsize)
            event_gauge.set(event_qsize)
            remediation_gauge.set(remediation_qsize)

            logger.trace(f"Updated queue metrics: metric={metric_qsize}, event={event_qsize}, remediation={remediation_qsize}")

            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Queue metrics updater task cancelled.")
            break
        except Exception as e:
            logger.exception(f"Queue metrics updater task failed: {e}")
            # Avoid tight loop on error
            await asyncio.sleep(interval_seconds * 2)


async def run_detector_state_saver(ctx: AppContext, interval_seconds: Optional[int] = None):
    """Periodically save the anomaly detector's state and clean up memory."""
    if interval_seconds is None:
        interval_seconds = settings.detector_save_interval # Use interval from settings

    logger.info(f"Starting detector state saver task (interval: {interval_seconds}s)...")

    if ctx.anomaly_detector is None:
        logger.error("Anomaly detector not available in context. Exiting state saver task.")
        return

    # Keep track of last memory cleanup time
    last_memory_cleanup = datetime.datetime.now()
    memory_cleanup_interval = interval_seconds * 6  # Clean memory every 6 save intervals
    
    # Import pymongo errors for specific handling
    from pymongo.errors import _OperationCancelled, NetworkTimeout, ConnectionFailure, AutoReconnect
    from kubewise.utils.retry import with_exponential_backoff
    
    # Define save state function with retry decorator
    @with_exponential_backoff(max_retries=3, initial_delay=1.0, max_delay=30.0)
    async def save_detector_state_with_retry():
        """Save detector state with automatic retries on connection errors."""
        await ctx.anomaly_detector.save_state()

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            logger.info("Attempting to save detector state...")
            
            try:
                # Use the retry-wrapped function
                await save_detector_state_with_retry()
                logger.info("Detector state saved successfully.")
            except (_OperationCancelled, NetworkTimeout, ConnectionFailure, AutoReconnect) as db_err:
                # These errors should be handled by the retry decorator, but if we still get here,
                # it means all retries failed
                logger.error(f"MongoDB connection error during state save after multiple retries: {db_err}")
                
                # Attempt to reconnect to MongoDB
                try:
                    logger.info("Attempting to reconnect to MongoDB...")
                    if ctx.mongo_client is not None:
                        # Check if we need to reinitialize the connection
                        try:
                            # Ping the database to check connection
                            await ctx.mongo_client.admin.command('ping')
                            logger.info("MongoDB connection is still valid")
                        except Exception:
                            logger.warning("MongoDB connection is invalid, reinitializing...")
                            # Reinitialize client
                            mongo_connection_uri = str(settings.mongo_uri)
                            ctx.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
                                mongo_connection_uri,
                                maxPoolSize=100, 
                                minPoolSize=10,
                                maxIdleTimeMS=30000,
                                serverSelectionTimeoutMS=5000,
                                connectTimeoutMS=5000,
                                socketTimeoutMS=10000,
                                waitQueueTimeoutMS=5000,
                                retryWrites=True,
                                w="majority"
                            )
                            ctx.db = ctx.mongo_client[settings.mongo_db_name]
                            # Update detector's database reference
                            if ctx.anomaly_detector is not None:
                                ctx.anomaly_detector.db = ctx.db
                except Exception as reconnect_err:
                    logger.exception(f"Failed to reconnect to MongoDB: {reconnect_err}")
            except Exception as e:
                logger.exception(f"Error saving detector state: {e}")
                
            # Periodic memory cleanup to avoid unbounded growth
            now = datetime.datetime.now()
            time_since_cleanup = (now - last_memory_cleanup).total_seconds()
            
            if time_since_cleanup >= memory_cleanup_interval and ctx.anomaly_detector is not None:
                logger.info("Performing memory cleanup for detector state...")
                try:
                    cleanup_count = ctx.anomaly_detector.cleanup_old_data()
                    last_memory_cleanup = now
                    logger.info(f"Memory cleanup complete. Removed {cleanup_count} old records.")
                except Exception as cleanup_err:
                    logger.exception(f"Error during memory cleanup: {cleanup_err}")
                    
        except asyncio.CancelledError:
            logger.info("Detector state saver task cancelled.")
            break
        except Exception as e:
            logger.exception(f"Unhandled error in detector state saver task: {e}")
            await asyncio.sleep(min(60, interval_seconds))  # Wait before retrying, but not too long


# --- FastAPI App Creation ---

def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app = FastAPI(
        title="KubeWise",
        description="""
        Autonomous Kubernetes Anomaly Detection & Self-Remediation
        
        KubeWise monitors Kubernetes clusters using Prometheus metrics and Kubernetes API events, 
        detects anomalies using online machine learning (River ML), and attempts automated remediation 
        using AI-generated plans (Pydantic-AI + Gemini) executed via a DSL.

        Core Components:
        
        · Collectors: Poll Prometheus for metrics and watch Kubernetes API events
        · Detector: Process metrics and events to detect anomalies using River ML
        · Remediation Planner: Generate action plans using AI (Gemini)
        · Remediation Engine: Execute action plans through Kubernetes API
        
        API Documentation:
        
        Use the endpoints below to interact with the KubeWise system. Key endpoints include:
        · /health - Basic health check
        · /health/detail - Detailed health information about all system components
        · /metrics - Prometheus metrics for monitoring KubeWise itself
        · /anomalies - List detected anomalies
        · /config - View and update system configuration
        """,
        version="0.1.0",
        lifespan=lifespan, # Use the async context manager for startup/shutdown
        swagger_ui_parameters={
            "docExpansion": "list",  # Expand the operation list by default
            "defaultModelsExpandDepth": 3,  # Expand models to show nested objects
            "deepLinking": True,  # Enable deep linking for sharing URLs to specific operations
            "displayRequestDuration": True,  # Show response time in the UI
            "syntaxHighlight.theme": "monokai",  # Use a more modern syntax highlighting theme
            "filter": True,  # Enable filtering of operations
            "persistAuthorization": True,  # Keep auth data between page refreshes
        },
        contact={
            "name": "Lohit Kolluri",
            "url": "https://github.com/lohitkolluri/KubeWise",
            "email": "me@lohit.is-a.dev",
        },
        license_info={
            "name": "MIT",
            "url": "https://opensource.org/licenses/MIT",
        },
        openapi_tags=[
            {
                "name": "Health",
                "description": "Endpoints for checking system health and status",
            },
            {
                "name": "Metrics",
                "description": "Prometheus metrics endpoint for monitoring KubeWise",
            },
            {
                "name": "Anomalies",
                "description": "Retrieve information about detected anomalies",
            },
            {
                "name": "Configuration",
                "description": "View and update system configuration",
            },
            {
                "name": "Validation",
                "description": "Tools to validate Prometheus queries",
            },
        ],
    )
    
    # Add metrics middleware
    @app.middleware("http")
    async def metrics_middleware(request, call_next):
        # Extract path for metrics labeling - strip query params
        path = request.url.path
        method = request.method
        
        # Record request timing
        start_time = time.time()
        
        # Process the request
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            # Record metrics on exception too
            end_time = time.time()
            from kubewise.api.context import API_REQUEST_DURATION
            API_REQUEST_DURATION.labels(
                endpoint=path, 
                method=method,
                status_code=500
            ).observe(end_time - start_time)
            # Re-raise the exception
            raise
        
        # Record the metrics
        end_time = time.time()
        from kubewise.api.context import API_REQUEST_DURATION
        API_REQUEST_DURATION.labels(
            endpoint=path, 
            method=method,
            status_code=status_code
        ).observe(end_time - start_time)
        
        return response

    # Include API routers
    app.include_router(routers.router)

    logger.info("FastAPI application configured with metrics middleware.")
    return app

# --- Main Entry Point (for Uvicorn) ---
# This allows running with `uvicorn kubewise.api.server:app`
# Note: Uvicorn might call create_app itself depending on factory usage.
# Using the factory pattern `create_app` is generally preferred.

# app = create_app() # Create instance only if run directly? No, uvicorn needs the factory.

if __name__ == "__main__":
    # This block is mainly for debugging the server setup directly,
    # not for production deployment. Use Uvicorn CLI for that.
    print("Running server directly for debug (use Uvicorn in production)...")
    import uvicorn
    # Uvicorn needs the import string for the factory or app instance
    uvicorn.run("kubewise.api.server:create_app", host="0.0.0.0", port=8000, reload=True, factory=True)

async def cancel_and_wait(tasks: Set[asyncio.Task], name: str, timeout: float):
    """Helper to cancel tasks and wait for them with a timeout."""
    tasks_to_cancel = [t for t in list(tasks) if t and not t.done()]
    if not tasks_to_cancel:
        logger.info(f"No active {name} tasks to cancel.")
        return

    logger.info(f"Cancelling {len(tasks_to_cancel)} {name} task(s)...")
    for task in tasks_to_cancel:
        task.cancel()

    logger.info(f"Waiting up to {timeout}s for {name} tasks to finish after cancellation...")
    done, pending = await asyncio.wait(tasks_to_cancel, timeout=timeout, return_when=asyncio.ALL_COMPLETED)

    if pending:
        logger.warning(f"{len(pending)} {name} tasks did not finish within the {timeout}s timeout:")
        for task in pending:
            try:
                logger.warning(f"  - Task still pending: {task.get_name()} (State: {task._state})")
            except AttributeError:
                logger.warning(f"  - Task still pending: {task.get_name()}")
    else:
        logger.info(f"All cancelled {name} tasks finished within timeout.")

    # Log exceptions from completed tasks
    for task in done:
        if task.cancelled():
            logger.debug(f"Task {task.get_name()} was cancelled successfully.")
        elif task.exception():
            try:
                task.result()  # Re-raise to log
            except Exception as task_exc:
                logger.error(f"Task {task.get_name()} finished with exception: {task_exc!r}")
