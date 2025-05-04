import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import datetime # Needed for timezone aware datetime
import time # For rate limiting check
from typing import Optional, Set, Dict, Tuple # Added Tuple for helper return

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
# Corrected imports based on pydantic-ai docs - REMOVED incorrect 'from pydantic_ai.llm import Gemini'
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider
# Import AppContext from the new context module
from kubewise.api.context import AppContext
from prometheus_client import REGISTRY # Import REGISTRY

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
    # Logging is configured on import now.
    logger.info("Starting KubeWise lifespan...")

    # Create the application context instance
    app_context = AppContext()
    app_context.start_time = datetime.datetime.now(datetime.timezone.utc)
    app_context.start_timestamp = time.time()
    app.state.app_context = app_context # Store context in app state

    console = Console() # Keep console for banner and progress

    # 1. Show KubeWise banner
    console.clear() # Clear console for a clean start
    console.print(Panel.fit(ASCII_BANNER, title="[bold green]KubeWise", subtitle="AI-Powered Kubernetes Guardian 🚀", style="bold cyan", box=box.DOUBLE))

    # 2. Simulated loading steps + Fetch nodes dynamically
    console.rule("[bold green]Starting KubeWise Server...")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        # Initialize Kubernetes
        task = progress.add_task("[yellow]Connecting to Kubernetes cluster...", total=None)
        
        # Fetch cluster nodes
        progress.update(task, description="[cyan]Fetching cluster nodes...")
        try:
            # Pass the shared k8s_api_client from the context
            nodes = await fetch_kubernetes_nodes(app_context.k8s_api_client)
            logger.info(f"Fetched nodes: Type={type(nodes)}, Content={nodes}") # Debug log
        except Exception as e:
            nodes = []
            console.print(f"[bold red]Failed to fetch nodes: {e}")
            logger.error(f"Error fetching nodes: {e}") # Log the error

        if nodes:
            console.print("\n[bold green]🖥️  Connected Nodes:")
            # Ensure nodes is iterable before iterating
            if isinstance(nodes, list):
                for node in nodes:
                    console.print(f"   - [cyan]{node}[/cyan]")
            else:
                console.print("[bold red]Error: Fetched nodes is not a list and cannot be iterated.")
                logger.error(f"Fetched nodes is not a list: Type={type(nodes)}")
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

    # Gather settings to display (optional, can keep or remove)
    # Mask sensitive keys like API keys if necessary (GEMINI_API_KEY is Pydantic SecretStr)
    # Use anomaly_thresholds from settings
    settings_display = {
        "Log Level": settings.log_level,
        "Prometheus URL": str(settings.prom_url),
        "Mongo DB Name": settings.mongo_db_name,
        "Anomaly Thresholds": str(settings.anomaly_thresholds), # Use the new dict setting
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

    # --- Initialize Clients and Resources within AppContext ---
    try:
        from kubewise.api.deps import create_mongo_client
        mongo_connection_uri: str = str(settings.mongo_uri)
        app_context.mongo_client = await create_mongo_client(mongo_connection_uri)
        await app_context.mongo_client.admin.command('ping')
        app_context.db = app_context.mongo_client[settings.mongo_db_name]
        logger.info(f"Connected to MongoDB database '{settings.mongo_db_name}' with optimized connection pool")
    except Exception as e:
        logger.exception(f"FATAL: Failed to connect to MongoDB during startup: {e}")
        app_context.mongo_client = None
        app_context.db = None
        logger.critical("MongoDB unavailable – aborting startup")
        raise RuntimeError("MongoDB connection failed during startup")

    # Initialize shared HTTP client (httpx)
    app_context.http_client = httpx.AsyncClient(follow_redirects=True)
    logger.info("Shared HTTP client (httpx) initialized.")

    # Initialize K8s client (needed by watcher and remediation engine) using aiohttp session
    try:
        await load_k8s_config() # Use the imported function
        # Create an aiohttp session explicitly - will be used for app's own HTTP operations
        app_context.aiohttp_session = aiohttp.ClientSession()
        # Initialize ApiClient with proper configuration
        app_context.k8s_api_client = client.ApiClient()
        # Verify connection with a simple API call
        v1 = client.CoreV1Api(api_client=app_context.k8s_api_client)
        await v1.list_namespace(limit=1)
        logger.info("Kubernetes client configured and connection verified.")
    except Exception as e:
        logger.exception(f"FATAL: Failed to configure Kubernetes client during startup: {e}")
        app_context.k8s_api_client = None
        # Close the session if it was created before the error
        if app_context.aiohttp_session and not app_context.aiohttp_session.closed:
             await app_context.aiohttp_session.close()
        app_context.aiohttp_session = None
        # Decide if K8s client failure is fatal or not. For now, allow startup.
        logger.error("Kubernetes client failed to initialize. Event watching will not work.")

    # Initialize Anomaly Detector using settings
    if app_context.db is not None: # Explicit None check
        try:
            app_context.anomaly_detector = SequentialAnomalyDetector(
                db=app_context.db,
                hst_n_trees=settings.hst_n_trees,
                hst_height=settings.hst_height,
                hst_window_size=settings.hst_window_size,
                river_trees=settings.river_trees,
                remediation_cooldown_seconds=settings.remediation_cooldown_seconds,
                temporal_window_seconds=3600,  # 1 hour window for temporal analysis
                max_sequence_history=20  # Keep up to 20 events in sequence history
            )
            logger.info(f"Sequential anomaly detector initialized with settings: HST(trees={settings.hst_n_trees}, height={settings.hst_height}, window={settings.hst_window_size}), River(trees={settings.river_trees}).")
            logger.info("Attempting to load detector state...")
            # Load state explicitly after initialization
            await app_context.anomaly_detector.load_state()
        except Exception as detector_init_err:
            logger.exception(f"Failed to initialize or load state for SequentialAnomalyDetector: {detector_init_err}")
            app_context.anomaly_detector = None # Ensure it's None on failure
            logger.error("Anomaly detector failed to initialize. Anomaly detection will not work.")
    else:
        logger.error("Anomaly detector cannot be initialized because DB connection failed.")
        # Decide if this is fatal. For now, allow startup.

    # Initialize Gemini Agent
    if settings.gemini_api_key and settings.gemini_api_key.get_secret_value() != "changeme":
        try:
            # Initialize provider with API key
            provider = GoogleGLAProvider(api_key=settings.gemini_api_key.get_secret_value())
            # Initialize model with the provider and model ID from settings
            model = GeminiModel(settings.gemini_model_id, provider=provider)
            # Initialize Agent with the configured model
            app_context.gemini_agent = Agent(model)
            logger.info(f"Gemini agent initialized with model '{settings.gemini_model_id}'.")
        except Exception as agent_err:
            logger.exception(f"Failed to initialize Gemini agent: {agent_err}")
            app_context.gemini_agent = None # Ensure it's None on failure
            logger.error("Gemini agent failed to initialize. AI-based remediation planning will not work.")
    else:
        logger.warning("GEMINI_API_KEY not set or is 'changeme'. Gemini agent not initialized.")
        app_context.gemini_agent = None


    # --- Start Background Tasks & Watchers ---
    # Instantiate and start the KubernetesEventWatcher directly
    # Ensure K8s client is available before starting watcher
    if app_context.db is not None and app_context.k8s_api_client is not None: # Explicit None checks for DB and K8s client
        app_context.k8s_event_watcher = KubernetesEventWatcher(
            events_queue=app_context.event_queue,
            db=app_context.db,
            k8s_client=app_context.k8s_api_client # Pass the shared client instance
        )
        # Start the watcher (it manages its own background tasks)
        await app_context.k8s_event_watcher.start()
    elif app_context.db is None:
        logger.error("KubernetesEventWatcher not started because DB connection failed.")
    else:
        logger.error("KubernetesEventWatcher not started because DB connection failed.")

    # Start Prometheus Poller Task
    if app_context.http_client is not None: # Explicit None check
        prom_task = asyncio.create_task(run_prometheus_poller(app_context))
        app_context.datasource_tasks.add(prom_task)
        prom_task.add_done_callback(app_context.datasource_tasks.discard)

    if app_context.anomaly_detector is not None: # Explicit None check
        detector_task = asyncio.create_task(run_detection_loop(app_context))
        app_context.processing_tasks.add(detector_task)
        detector_task.add_done_callback(app_context.processing_tasks.discard)

        if app_context.db is not None and app_context.k8s_api_client is not None: # Explicit None checks
            remediation_task = asyncio.create_task(run_remediation_trigger(app_context))
            app_context.processing_tasks.add(remediation_task)
            remediation_task.add_done_callback(app_context.processing_tasks.discard)
        else:
            logger.error("Remediation trigger task not started due to missing DB or K8s client.")

        # Start detector state saver task if detector initialized
        if app_context.anomaly_detector is not None:
            saver_task = asyncio.create_task(run_detector_state_saver(app_context))
            app_context.processing_tasks.add(saver_task) # Add to processing tasks for tracking
            saver_task.add_done_callback(app_context.processing_tasks.discard)
        else:
            logger.error("Detector state saver task not started because detector failed to initialize.")


    total_tasks = len(app_context.datasource_tasks) + len(app_context.processing_tasks)
    # Define gauges within lifespan to avoid duplicate registration on reload
    # Check if metrics already exist in the registry (handles potential edge cases)
    metric_name = "kubewise_metric_queue_size"
    if metric_name not in REGISTRY._names_to_collectors:
        metric_queue_gauge = Gauge(metric_name, "Current number of items in the metric queue")
    else:
        metric_queue_gauge = REGISTRY._names_to_collectors[metric_name]

    event_name = "kubewise_event_queue_size"
    if event_name not in REGISTRY._names_to_collectors:
        event_queue_gauge = Gauge(event_name, "Current number of items in the event queue")
    else:
        event_queue_gauge = REGISTRY._names_to_collectors[event_name]

    remediation_name = "kubewise_remediation_queue_size"
    if remediation_name not in REGISTRY._names_to_collectors:
        remediation_queue_gauge = Gauge(remediation_name, "Current number of items in the remediation queue")
    else:
        remediation_queue_gauge = REGISTRY._names_to_collectors[remediation_name]


    # Pass gauges to the updater task
    queue_metrics_task = asyncio.create_task(
        run_queue_metrics_updater(
            ctx=app_context,
            metric_gauge=metric_queue_gauge,
            event_gauge=event_queue_gauge,
            remediation_gauge=remediation_queue_gauge
        )
    )
    app_context.processing_tasks.add(queue_metrics_task)
    queue_metrics_task.add_done_callback(app_context.processing_tasks.discard)

    total_tasks = len(app_context.datasource_tasks) + len(app_context.processing_tasks)
    logger.info(f"Started {total_tasks} background task(s).")


    yield # Application runs here

    # === Shutdown ===
    logger.info("Starting KubeWise shutdown sequence...")
    app_context = app.state.app_context # Retrieve context
    shutdown_timeout = 15.0 # General timeout for shutdown steps in seconds

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
                    task.result() # Re-raise to log
                except Exception as task_exc:
                    logger.error(f"Task {task.get_name()} finished with exception: {task_exc!r}")


    # --- Ordered Shutdown ---
    # 1. Stop Kubernetes Event Watcher (uses its own internal timeouts now)
    if app_context.k8s_event_watcher is not None:
        logger.info("Attempting to stop Kubernetes Event Watcher...")
        try:
            # The watcher's stop method now has internal timeouts
            await app_context.k8s_event_watcher.stop()
            logger.info("Kubernetes Event Watcher stop sequence finished.")
        except Exception as e:
            # Log error but continue shutdown
            logger.exception(f"Error during Kubernetes Event Watcher stop: {e}")

    # 2. Cancel Datasource Tasks (Pollers) - Watcher is stopped above
    await cancel_and_wait(app_context.datasource_tasks, "datasource", shutdown_timeout)

    # 3. Optionally wait for queues to drain? (Skipped for simplicity)

    # 4. Cancel Processing Tasks (Detector, Remediation, Queue Metrics)
    await cancel_and_wait(app_context.processing_tasks, "processing", shutdown_timeout)

    # 5. Save final detector state before closing clients (with timeout)
    if app_context.anomaly_detector is not None: # Explicit None check
        logger.info("Attempting to save final detector state...")
        try:
            await asyncio.wait_for(app_context.anomaly_detector.save_state(), timeout=shutdown_timeout)
            logger.info("Successfully saved final detector state.")
        except asyncio.TimeoutError:
            logger.error(f"Timeout ({shutdown_timeout}s) occurred while saving detector state.")
        except Exception as e:
            logger.exception(f"Error saving detector state during shutdown: {e}")

    # 6. Close Async Clients (with timeouts)
    if app_context.http_client is not None: # Explicit None check
        logger.info("Closing shared HTTP client (httpx)...")
        try:
            await asyncio.wait_for(app_context.http_client.aclose(), timeout=5.0)
            logger.info("Shared HTTP client (httpx) closed.")
        except asyncio.TimeoutError:
             logger.error("Timeout occurred while closing HTTP client (httpx).")
        except Exception as e:
             logger.exception(f"Error closing HTTP client (httpx): {e}")

    # Close the aiohttp session first, as k8s_api_client uses it
    if app_context.aiohttp_session is not None: # Explicit None check
        logger.info("Closing aiohttp client session...")
        try:
            # aiohttp session close is async
            await asyncio.wait_for(app_context.aiohttp_session.close(), timeout=5.0)
            logger.info("aiohttp client session closed.")
        except asyncio.TimeoutError:
             logger.error("Timeout occurred while closing aiohttp client session.")
        except Exception as e:
             logger.exception(f"Error closing aiohttp client session: {e}")

    # Closing the k8s_api_client might still be necessary depending on its internal state
    # but closing the aiohttp session is the primary fix for the reported error.
    if app_context.k8s_api_client is not None: # Explicit None check
        logger.info("Closing Kubernetes API client...")
        try:
            # Assuming k8s_client.close() is async and safe to call after session close
            await asyncio.wait_for(app_context.k8s_api_client.close(), timeout=5.0)
            logger.info("Kubernetes client closed.")
        except asyncio.TimeoutError:
             logger.error("Timeout occurred while closing Kubernetes client.")
        except Exception as e:
             logger.exception(f"Error closing Kubernetes client: {e}")


    # 7. Close Sync Clients (no timeout needed for sync calls)
    if app_context.mongo_client is not None: # Explicit None check
        logger.info("Closing MongoDB connection...")
        try:
            app_context.mongo_client.close() # Sync close
            logger.info("MongoDB connection closed.")
        except Exception as e:
             logger.exception(f"Error closing MongoDB connection: {e}")


    logger.info("KubeWise shutdown sequence complete.")


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
                
                # --- Remediation Rate Limiting Check ---
                entity_id = get_entity_id_from_anomaly(anomaly_record)
                proceed_with_remediation = True
                now = datetime.datetime.now(datetime.timezone.utc) # Get current time once for potential update later
                if entity_id and ctx.anomaly_detector is not None: # Explicit None check
                    # Access detector's internal state (use with caution, maybe add a method later)
                    entity_state = ctx.anomaly_detector._entity_state.setdefault(entity_id, {})
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
                        generated_plan = await planner.generate_remediation_plan( # Correct indentation and variables
                            anomaly_record=anomaly_record,
                            db=ctx.db,
                            k8s_api_client=ctx.k8s_api_client,
                            agent=ctx.gemini_agent
                        )
                        logger.info(f"Gemini planner generated plan: {generated_plan.model_dump_json(indent=2) if generated_plan else 'None'}")
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
                    if ctx.gemini_agent:
                         logger.warning("Gemini plan was empty or failed, falling back to static planner.")
                    else:
                         logger.info("Gemini agent not available, using static planner.")
                    # Load static plan (assuming it doesn't raise exceptions easily)
                    generated_plan = await planner.load_static_plan(anomaly_record, ctx.db)
                    logger.info(f"Static planner loaded plan: {generated_plan.model_dump_json(indent=2) if generated_plan else 'None'}")


                # 2. Execute Plan (if any plan exists with actions)
                if generated_plan and generated_plan.actions:
                    logger.info(f"Executing remediation plan with {len(generated_plan.actions)} action(s).")
                    # Pass k8s client from context to engine
                    execution_success = await engine.execute_remediation_plan(
                        plan=generated_plan,
                        anomaly_record=anomaly_record,
                        db=ctx.db,
                        k8s_client=ctx.k8s_api_client, # Pass client
                        app_context=ctx  # Pass context for metrics
                    )
                    # Update last remediation time in state *only if execution was successful*
                    if execution_success and entity_id and ctx.anomaly_detector is not None: # Explicit None check
                         # Use the 'now' timestamp captured earlier
                         ctx.anomaly_detector._entity_state[entity_id]["last_remediation_time"] = now
                         logger.info(f"Updated last remediation timestamp for entity '{entity_id}'.")

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

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            logger.info("Attempting to save detector state...")
            await ctx.anomaly_detector.save_state()
            logger.info("Detector state saved successfully.")
            
            # Perform memory cleanup periodically
            now = datetime.datetime.now()
            time_since_cleanup = (now - last_memory_cleanup).total_seconds()
            if time_since_cleanup >= memory_cleanup_interval:
                logger.info("Performing detector memory cleanup...")
                # Clean up entity state for entities without recent activity
                if hasattr(ctx.anomaly_detector, '_entity_state'):
                    entities_before = len(ctx.anomaly_detector._entity_state)
                    # Keep only entities with activity in the last 24 hours
                    cleanup_cutoff = now - datetime.timedelta(hours=24)
                    stale_entities = []
                    
                    for entity_id, state in ctx.anomaly_detector._entity_state.items():
                        last_activity = state.get('last_activity_time', state.get('last_remediation_time'))
                        if last_activity and last_activity < cleanup_cutoff:
                            stale_entities.append(entity_id)
                    
                    # Remove stale entities
                    for entity_id in stale_entities:
                        del ctx.anomaly_detector._entity_state[entity_id]
                    
                    entities_after = len(ctx.anomaly_detector._entity_state)
                    logger.info(f"Memory cleanup complete: removed {entities_before - entities_after} stale entities")
                
                last_memory_cleanup = now

        except asyncio.CancelledError:
            logger.info("Detector state saver task cancelled.")
            # Attempt one final save on cancellation if possible
            if ctx.anomaly_detector:
                try:
                    logger.info("Attempting final detector state save on cancellation...")
                    await ctx.anomaly_detector.save_state()
                    logger.info("Final detector state saved.")
                except Exception as final_save_err:
                    logger.error(f"Error during final detector state save: {final_save_err}")
            break
        except Exception as e:
            logger.exception(f"Detector state saver task failed during save attempt: {e}")
            # Avoid tight loop on error, wait before retrying
            await asyncio.sleep(interval_seconds)


# --- FastAPI App Creation ---

def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app = FastAPI(
        title="KubeWise",
        description="Autonomous Kubernetes Anomaly Detection & Self-Remediation",
        version="0.1.0",
        lifespan=lifespan, # Use the async context manager for startup/shutdown
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
