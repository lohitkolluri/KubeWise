"""
Factory module for creating and managing application dependencies.

This module provides centralized creation of dependencies like database connections,
API clients, and services. It follows the factory pattern to ensure consistent
dependency creation and management throughout the application.
"""

import asyncio
import datetime
from typing import Optional, Tuple

import httpx
import motor.motor_asyncio
from kubernetes_asyncio import client, config
from loguru import logger
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider

from kubewise.config import settings
from kubewise.api.context import AppContext
from kubewise.collector.k8s_events import KubernetesEventWatcher
from kubewise.collector.prometheus import PrometheusFetcher
from kubewise.models.detector import SequentialAnomalyDetector
from kubewise.remediation.planner import PlannerDependencies
from kubewise.utils.retry import with_exponential_backoff
from kubewise.remediation.diagnosis import DiagnosisEngine


# MongoDB connection pool configuration
MONGO_MAX_POOL_SIZE = 100
MONGO_MIN_POOL_SIZE = 10
MONGO_MAX_IDLE_TIME_MS = 30000  # 30 seconds

# HTTP client configuration
HTTP_TIMEOUT = 30.0
HTTP_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=30)

# Default Prometheus queries if none configured in settings
DEFAULT_PROM_QUERIES = {
    "cpu_usage": 'sum by (namespace,pod) (rate(container_cpu_usage_seconds_total{container!="POD",container!=""}[5m]))',
    "memory_usage": 'sum by (namespace,pod) (container_memory_usage_bytes{container!="POD",container!=""})',
    "container_restart_count": "sum by (namespace,pod) (kube_pod_container_status_restarts_total)",
    "network_rx_rate": "sum by (namespace,pod) (rate(container_network_receive_bytes_total[5m]))",
    "network_tx_rate": "sum by (namespace,pod) (rate(container_network_transmit_bytes_total[5m]))",
    "disk_read_rate": 'sum by (namespace,pod) (rate(container_fs_reads_bytes_total{container!="POD",container!=""}[5m]))',
    "disk_write_rate": 'sum by (namespace,pod) (rate(container_fs_writes_bytes_total{container!="POD",container!=""}[5m]))',
    "cpu_limits": 'sum by (namespace,pod) (kube_pod_container_resource_limits{resource="cpu"})',
    "memory_limits": 'sum by (namespace,pod) (kube_pod_container_resource_limits{resource="memory"})',
}


async def create_mongo_client(uri: str) -> motor.motor_asyncio.AsyncIOMotorClient:
    """Create a properly configured MongoDB client with connection pooling."""
    client = motor.motor_asyncio.AsyncIOMotorClient(
        uri,
        maxPoolSize=MONGO_MAX_POOL_SIZE,
        minPoolSize=MONGO_MIN_POOL_SIZE,
        maxIdleTimeMS=MONGO_MAX_IDLE_TIME_MS,
        serverSelectionTimeoutMS=30000,  # Increased to 30 seconds
        connectTimeoutMS=20000,          # Increased to 20 seconds
        socketTimeoutMS=45000,           # Increased to 45 seconds
        waitQueueTimeoutMS=20000,        # Increased to 20 seconds
        retryWrites=True,
        w="majority",
        tls=True,                        # Use TLS for connection
        tlsAllowInvalidCertificates=True # Temporarily allow invalid certificates
    )
    return client


async def create_http_client() -> httpx.AsyncClient:
    """Create a properly configured HTTP client with connection pooling."""
    client = httpx.AsyncClient(
        limits=HTTP_LIMITS,
        timeout=httpx.Timeout(timeout=HTTP_TIMEOUT),
        http2=False,  # Disable HTTP/2 as 'h2' package might not be installed
        transport=httpx.AsyncHTTPTransport(retries=2),
    )
    return client


async def create_k8s_client() -> Tuple[client.ApiClient, client.CoreV1Api]:
    """Create and configure Kubernetes API client and CoreV1Api."""
    # Load the Kubernetes configuration
    try:
        await config.load_kube_config()  # Use kubeconfig if running locally
    except Exception:
        await (
            config.load_incluster_config()
        )  # Use in-cluster config if running inside a Pod

    # Create the API client
    api_client = client.ApiClient()

    # Create the CoreV1Api using the same client
    core_api = client.CoreV1Api(api_client)

    return api_client, core_api


async def create_gemini_agent(
    api_key: Optional[SecretStr] = None, model_id: Optional[str] = None
) -> Optional[Agent]:
    """Create and configure the Gemini agent for AI-powered remediation."""
    # Use provided parameters or fall back to settings
    api_key = api_key or settings.gemini_api_key
    model_id = model_id or settings.gemini_model_id

    if not api_key or api_key.get_secret_value() == "changeme":
        logger.warning(
            "GEMINI_API_KEY not set or is 'changeme'. Gemini agent not initialized."
        )
        return None

    try:
        # Initialize provider with API key
        provider = GoogleGLAProvider(api_key=api_key.get_secret_value())

        # Initialize model with the provider and model ID from settings
        model = GeminiModel(model_id, provider=provider)

        # Initialize Agent with the configured model
        agent = Agent(model)
        logger.info(f"Gemini agent initialized with model '{model_id}'.")
        return agent
    except Exception as agent_err:
        logger.exception(f"Failed to initialize Gemini agent: {agent_err}")
        return None


async def create_event_watcher(app_context: AppContext) -> KubernetesEventWatcher:
    """Create and configure the Kubernetes event watcher."""
    if app_context.db is None or app_context.k8s_api_client is None:
        raise ValueError("Database and Kubernetes client must be initialized")

    # Create the CoreV1Api using the existing client
    core_api = client.CoreV1Api(app_context.k8s_api_client)

    # Create the event watcher
    watcher = KubernetesEventWatcher(
        events_queue=app_context.event_queue,
        db=app_context.db,
        k8s_client=app_context.k8s_api_client,
        k8s_core_api=core_api,
        watch_timeout=300,
    )
    return watcher


async def create_prometheus_fetcher(
    app_context: AppContext,
) -> Optional[PrometheusFetcher]:
    """Create and configure the Prometheus metrics fetcher.

    Args:
        app_context: The application context containing dependencies

    Returns:
        Configured PrometheusFetcher if successful, None otherwise
    """
    if app_context.metric_queue is None or app_context.http_client is None:
        logger.error(
            "Cannot create Prometheus fetcher: Required dependencies not available"
        )
        return None

    try:
        # Create fetcher with injected dependencies
        try:
            prometheus_url = settings.prom_url
            if not prometheus_url:
                logger.warning(
                    "Prometheus URL not configured. Metrics collection disabled."
                )
                return None

            # Convert HttpUrl to string explicitly to avoid 'rstrip' error
            prometheus_url_str = str(prometheus_url)

            metrics_queries = settings.prom_queries
        except AttributeError as attr_err:
            logger.warning(
                f"Prometheus configuration is incomplete: {attr_err}. Using default values."
            )
            prometheus_url_str = "http://localhost:9090"
            metrics_queries = (
                DEFAULT_PROM_QUERIES if "DEFAULT_PROM_QUERIES" in globals() else {}
            )

        # Create the Prometheus fetcher
        fetcher = PrometheusFetcher(
            metrics_queue=app_context.metric_queue,
            http_client=app_context.http_client,
            prometheus_url=prometheus_url_str,
            metrics_queries=metrics_queries,
            poll_interval=60.0,
            health_check_interval=300.0,
        )
        return fetcher
    except Exception as fetcher_err:
        logger.exception(f"Failed to initialize Prometheus fetcher: {fetcher_err}")
        return None


async def create_anomaly_detector(app_context: AppContext) -> SequentialAnomalyDetector:
    """Create and configure the anomaly detector."""
    if app_context.db is None:
        raise ValueError("Database must be initialized")

    # Create the detector
    detector = SequentialAnomalyDetector(
        db=app_context.db,
        hst_n_trees=settings.hst_n_trees,
        hst_height=settings.hst_height,
        hst_window_size=settings.hst_window_size,
        river_trees=settings.river_trees,
        iso_n_trees=settings.iso_n_trees,
        iso_subspace_size=settings.iso_subspace_size,
        iso_seed=settings.iso_seed,
        remediation_cooldown_seconds=settings.remediation_cooldown_seconds,
        max_sequence_history=20,  # Keep up to 20 events in sequence history
    )

    # Set temporal window value if configured in settings
    if hasattr(settings, "temporal_window_seconds"):
        detector._temporal_window_seconds = settings.temporal_window_seconds

    # Load state after initialization
    await detector.load_state()

    return detector


async def create_planner_dependencies(app_context: AppContext) -> PlannerDependencies:
    """Create planner dependencies container with database, K8s client and diagnostic tools."""
    if app_context.db is None or app_context.k8s_api_client is None:
        raise ValueError("Database and Kubernetes client must be initialized")

    # Import DiagnosticTools here to avoid circular imports
    from kubewise.remediation.planner import DiagnosticTools

    # Create diagnostic tools
    diagnostic_tools = DiagnosticTools(app_context.k8s_api_client)

    return PlannerDependencies(
        db=app_context.db,
        k8s_client=app_context.k8s_api_client,
        diagnostic_tools=diagnostic_tools,
    )


async def create_diagnosis_engine(app_context: AppContext) -> DiagnosisEngine:
    """Create the diagnosis engine with required dependencies."""
    if app_context.db is None or app_context.k8s_api_client is None:
        raise ValueError("Database and Kubernetes client must be initialized")

    # Get AI agent from context if available
    ai_agent = app_context.ai_agent if hasattr(app_context, "ai_agent") else None

    # Create and return the diagnosis engine
    diagnosis_engine = DiagnosisEngine(
        db=app_context.db, k8s_client=app_context.k8s_api_client, ai_agent=ai_agent
    )

    return diagnosis_engine


async def initialize_app_context() -> AppContext:
    """Initialize the application context with all necessary dependencies."""
    app_context = AppContext()
    app_context.start_time = datetime.datetime.now(datetime.timezone.utc)
    app_context.start_timestamp = asyncio.get_running_loop().time()

    # Initialize MongoDB client and database with retry
    @with_exponential_backoff(max_retries_override=5, initial_delay_override=1.0, max_delay=30.0)
    async def init_mongodb():
        mongo_connection_uri = str(settings.mongo_uri)
        mongo_client = await create_mongo_client(mongo_connection_uri)
        await mongo_client.admin.command("ping")
        db = mongo_client[settings.mongo_db_name]
        return mongo_client, db

    try:
        app_context.mongo_client, app_context.db = await init_mongodb()
        logger.info(
            f"Connected to MongoDB database '{settings.mongo_db_name}' with optimized connection pool"
        )
    except Exception as e:
        logger.exception(
            f"FATAL: Failed to connect to MongoDB after multiple retries: {e}"
        )
        app_context.mongo_client = None
        app_context.db = None
        logger.critical("MongoDB unavailable – aborting startup")
        raise RuntimeError("MongoDB connection failed during startup")

    # Initialize HTTPX client for API calls
    try:
        # Use default timeout values instead of missing settings attributes
        timeout = httpx.Timeout(
            connect=10.0,  # Default connect timeout
            read=30.0,  # Default read timeout
            write=10.0,  # Default write timeout
            pool=60.0,  # Default pool timeout
        )
        limits = httpx.Limits(
            max_connections=100,  # Default max connections
            max_keepalive_connections=20,  # Default keepalive connections
            keepalive_expiry=120,  # Default keepalive expiry
        )

        app_context.http_client = httpx.AsyncClient(
            timeout=timeout, limits=limits, follow_redirects=True, http2=True
        )

        # Create aiohttp session for Kubernetes and other clients
        import aiohttp

        app_context.aiohttp_session = aiohttp.ClientSession()
        logger.info("HTTP clients initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize HTTP client: {e}")

    # Initialize Kubernetes client
    try:
        app_context.k8s_api_client, k8s_core_api = await create_k8s_client()
        # Verify connection with a simple API call
        await k8s_core_api.list_namespace(limit=1)
        logger.info("Kubernetes client configured and connection verified.")
    except Exception as e:
        logger.exception(
            f"FATAL: Failed to configure Kubernetes client during startup: {e}"
        )
        app_context.k8s_api_client = None
        logger.error(
            "Kubernetes client failed to initialize. Event watching will not work."
        )

    # Initialize Gemini agent
    app_context.gemini_agent = await create_gemini_agent()

    # Initialize anomaly detector if database is available
    if app_context.db is not None:
        try:
            app_context.anomaly_detector = await create_anomaly_detector(app_context)
            logger.info("Anomaly detector initialized and state loaded.")
        except Exception as detector_init_err:
            logger.exception(
                f"Failed to initialize anomaly detector: {detector_init_err}"
            )
            app_context.anomaly_detector = None
            logger.error(
                "Anomaly detector failed to initialize. Anomaly detection will not work."
            )

    # Initialize Kubernetes event watcher if dependencies are available
    if app_context.db is not None and app_context.k8s_api_client is not None:
        try:
            app_context.k8s_event_watcher = await create_event_watcher(app_context)
            logger.info("Kubernetes event watcher initialized.")
        except Exception as watcher_init_err:
            logger.exception(
                f"Failed to initialize Kubernetes event watcher: {watcher_init_err}"
            )
            app_context.k8s_event_watcher = None
            logger.error(
                "Kubernetes event watcher failed to initialize. Event watching will not work."
            )

    # Initialize AI agent for remediation planning if API key is configured
    if hasattr(settings, "gemini_api_key") and settings.gemini_api_key:
        try:
            app_context.ai_agent = await create_gemini_agent()
            logger.info(f"Initialized AI agent with model {settings.gemini_model_id}")
        except Exception as agent_init_err:
            logger.exception(f"Failed to initialize AI agent: {agent_init_err}")
            app_context.ai_agent = None
            logger.error(
                "AI agent failed to initialize. Will use static remediation plans."
            )
    else:
        app_context.ai_agent = None
        logger.warning(
            "No Gemini API key configured. Will use static remediation plans."
        )

    # Initialize diagnosis engine if diagnosis workflow is enabled
    if (
        hasattr(settings, "enable_diagnosis_workflow")
        and settings.enable_diagnosis_workflow
    ):
        try:
            app_context.diagnosis_engine = await create_diagnosis_engine(app_context)
            logger.info("Diagnosis engine initialized.")
        except Exception as diagnosis_init_err:
            logger.exception(
                f"Failed to initialize diagnosis engine: {diagnosis_init_err}"
            )
            app_context.diagnosis_engine = None
            logger.error(
                "Diagnosis engine failed to initialize. Diagnosis-driven workflow will not be available."
            )
    else:
        app_context.diagnosis_engine = None
        logger.info("Diagnosis-driven workflow is disabled in settings.")

    return app_context
