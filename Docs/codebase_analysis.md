# KubeWise Codebase Analysis

This document provides a detailed analysis of the KubeWise codebase, exploring its architecture, key components, design patterns, and technology stack. It aims to serve as a comprehensive guide for understanding the system's inner workings.

## Table of Contents

1.  [Introduction](#1-introduction)
2.  [Overall Architecture](#2-overall-architecture)
3.  [Key Components Deep Dive](#3-key-components-deep-dive)
    *   [3.1 Collectors](#31-collectors)
    *   [3.2 Processing & Detection](#32-processing--detection)
    *   [3.3 Remediation](#33-remediation)
    *   [3.4 API & CLI](#34-api--cli)
    *   [3.5 Models & Schema](#35-models--schema)
    *   [3.6 Configuration & Logging](#36-configuration--logging)
4.  [Core Concepts & Design Patterns](#4-core-concepts--design-patterns)
    *   [4.1 Asynchronous Programming (`asyncio`)](#41-asynchronous-programming-asyncio)
    *   [4.2 Dependency Injection](#42-dependency-injection)
    *   [4.3 Retry Mechanisms (`with_exponential_backoff`)](#43-retry-mechanisms-with_exponential_backoff)
    *   [4.4 State Persistence](#44-state-persistence)
    *   [4.5 Data Modeling with Pydantic](#45-data-modeling-with-pydantic)
    *   [4.6 Action Registry Pattern](#46-action-registry-pattern)
5.  [Technology Stack Integration](#5-technology-stack-integration)
6.  [Conclusion](#6-conclusion)

## 1. Introduction

KubeWise is an autonomous system designed to enhance the reliability of Kubernetes clusters. It achieves this by continuously monitoring cluster metrics and events, detecting anomalies in real-time using online machine learning, and automatically attempting to remediate issues based on AI-generated plans. This document dissects the codebase to provide a detailed understanding of how these functionalities are implemented.

## 2. Overall Architecture

The architecture of KubeWise is modular and follows a clear data flow pipeline, as depicted in the [architecture diagram](architecture.md). Data originates from the Kubernetes cluster (Prometheus metrics and K8s API events), flows through collectors, is processed and analyzed by the anomaly detector, and, if an anomaly is found, triggers a remediation workflow involving AI planning and action execution.

The main components are:
-   **Collectors:** Gather data from external sources (Prometheus, K8s API).
-   **Processing & Detection:** Consume collected data, extract features, apply online ML models to detect anomalies, and persist results.
-   **Remediation:** Orchestrate the planning (AI or static) and execution of actions to address detected anomalies.
-   **API & CLI:** Provide interfaces for configuration, monitoring, and interaction.
-   **Persistence:** MongoDB Atlas is used for storing anomaly records, executed actions, and potentially raw event data.
-   **Configuration:** Managed via environment variables and a `.env` file.
-   **Logging:** Structured logging for observability.

## 3. Key Components Deep Dive

### 3.1 Collectors

The collectors are responsible for ingesting data from the Kubernetes environment.

#### `kubewise/collector/k8s_events.py`

This module implements the `KubernetesEventWatcher`, which establishes a long-lived watch stream with the Kubernetes API server to receive real-time event notifications. It focuses on filtering for relevant event types and reasons (primarily `Warning` events related to pod and node issues) and places parsed `KubernetesEvent` objects onto an `asyncio.Queue` for the detector.

Key aspects:
-   Uses `kubernetes-asyncio` for asynchronous interaction with the K8s API.
-   Implements robust reconnection logic with exponential backoff using the `@with_exponential_backoff` decorator to handle transient network issues or API errors (like `410 Gone` for old resource versions).
-   Persists the last processed `resourceVersion` to MongoDB to resume watching from the correct point after restarts.
-   Includes a health check loop to monitor the watch task's status and restart it if necessary.
-   Parses raw event dictionaries into the structured `KubernetesEvent` Pydantic model.

```python
# Example from kubewise/collector/k8s_events.py
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

# ...

class KubernetesEventWatcher:
    # ...
    async def _event_watch_loop(self) -> None:
        """Main loop for watching Kubernetes events."""
        # ... retry logic ...
        stream = w.stream(v1.list_event_for_all_namespaces, **watch_options)
        async for event_obj in stream:
            # ... process event ...
            await self._process_event(event_obj)
            # ... update resource_version ...
```

#### `kubewise/collector/prometheus.py`

This module contains the `PrometheusFetcher`, responsible for periodically querying a configured Prometheus instance (`PROM_URL`) for a predefined set of metrics. It uses `httpx` for asynchronous HTTP requests and places fetched `MetricPoint` objects onto another `asyncio.Queue`.

Key aspects:
-   Uses a shared `httpx.AsyncClient` with connection pooling for efficiency.
-   Executes multiple PromQL queries concurrently using `asyncio.gather`.
-   Applies exponential backoff (`@with_exponential_backoff`) for query failures.
-   Includes a health check loop to verify Prometheus connectivity and recreate the HTTP client if needed.
-   Parses Prometheus API responses into the `MetricPoint` Pydantic model, handling different result types (vector, scalar) and label transformations.
-   Defines a set of default PromQL queries targeting common Kubernetes metrics (CPU, memory, network, restarts, etc.).

```python
# Example from kubewise/collector/prometheus.py
class PrometheusFetcher:
    # ...
    async def _fetch_all_metrics(self) -> List[MetricPoint]:
        """
        Fetch all configured metrics concurrently.
        """
        # ... create tasks for each query ...
        tasks = []
        for name, query in self.metrics_queries.items():
            task = self._fetch_single_metric(name, query)
            tasks.append(task)

        # Execute all queries concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # ... process results ...

    @with_exponential_backoff(max_retries=3)
    async def _fetch_single_metric(self, name: str, query: str) -> List[MetricPoint]:
        """
        Fetch and parse a single metric query with retries.
        """
        # ... query Prometheus ...
        response = await self._client.get(
            prometheus_url,
            params=params,
            timeout=self.timeout
        )
        response.raise_for_status()
        # ... parse response ...
```

### 3.2 Processing & Detection

The core anomaly detection logic resides in this component.

#### `kubewise/models/detector.py`

This module implements the `OnlineAnomalyDetector`, which consumes metrics and events from the collector queues, extracts features, and applies online machine learning models to identify anomalies.

Key aspects:
-   **Dual-Model Approach:** Uses a combination of `river.anomaly.HalfSpaceTrees` and a `river.tree.HoeffdingTreeClassifier` (part of River's ensemble capabilities) within `compose.Pipeline`s.
    -   Half-Space Trees (`_hst_pipeline`) are used for raw anomaly scoring.
    -   The Hoeffding Tree Classifier (`_river_pipeline`) is trained online to classify data points as normal (0) or anomalous (1), providing a probability score.
    -   An anomaly is flagged if *either* the HST score *or* the River model's anomaly probability exceeds a configured threshold.
-   **Feature Extraction:** The `_extract_features` method transforms incoming `MetricPoint` and `KubernetesEvent` objects into a unified feature vector. It calculates rates of change for metrics and maintains exponentially decayed counts for specific critical event reasons (`OOMKilled`, `CrashLoopBackOff`, etc.) in an in-memory `_entity_state` dictionary.
-   **Dynamic Thresholds:** Anomaly thresholds can be configured per metric type (CPU, memory, event, etc.) via settings, allowing for more nuanced detection.
-   **State Persistence:** The state of both the HST and River models, as well as the entity state (for feature calculation and cooldowns), is periodically saved to MongoDB using `pickle` and `zlib` compression for the HST model to handle potentially large model sizes. This ensures the models can resume learning from where they left off after application restarts.
-   **Remediation Cooldown:** A per-entity cooldown timer is implemented to prevent triggering multiple remediation actions for the same entity within a short period, avoiding remediation storms.
-   Detected anomalies are stored as `AnomalyRecord` objects in MongoDB and placed onto the `remediation_queue`.

```python
# Example from kubewise/models/detector.py
class OnlineAnomalyDetector:
    # ...
    def _extract_features(
        self, data_point: Union[MetricPoint, KubernetesEvent]
    ) -> Optional[Tuple[str, Dict[str, float]]]:
        """
        Extract features from a metric point or Kubernetes event.
        """
        # ... logic to extract features from metrics and events ...
        # ... calculate decayed event counts and rates ...
        # ... assemble feature vector ...

    async def detect_anomaly(
        self, data_point: Union[MetricPoint, KubernetesEvent]
    ) -> Optional[AnomalyRecord]:
        """
        Process a data point (metric or event) and detect if it represents an anomaly.
        """
        # ... extract features ...
        # ... check cooldown ...
        # ... score with HST and River models ...
        # ... compare scores to threshold ...
        # ... update models (learn_one) ...
        # ... save state periodically ...
        # ... if anomaly, create AnomalyRecord and store ...
```

### 3.3 Remediation

This component handles the process of generating and executing plans to fix detected anomalies.

#### `kubewise/remediation/planner.py`

This module is responsible for generating a `RemediationPlan` for a given `AnomalyRecord`. It employs a two-strategy approach:

1.  **Primary Strategy (AI Generation):** If a Google Gemini AI agent is configured (`pydantic-ai` + Gemini), it gathers context (anomaly details, recent related Kubernetes events, and the state/metadata of the affected resource and its controller) and constructs a detailed prompt for the LLM. The LLM is asked to generate a structured `RemediationPlan` (defined in `schema.py`) containing reasoning and a list of `RemediationAction` objects.
2.  **Fallback Strategy (Static Plans):** If the AI agent is not available or fails to generate a valid plan with actions, the system falls back to predefined static plans (`STATIC_PLAN_MAPPINGS`). These static plans are selected based on the anomaly's metric pattern and entity type and include placeholders (e.g., `{resource_name}`, `{current_replicas + 1}`) that are substituted with actual values from the anomaly and resource metadata.

Key aspects:
-   Uses `pydantic-ai` to interact with the Gemini LLM, leveraging Pydantic models for structured output.
-   Fetches recent related Kubernetes events from MongoDB (`get_recent_events`).
-   Fetches the metadata and status of the affected Kubernetes resource and attempts to trace its owner references (e.g., Pod -> ReplicaSet -> Deployment) to identify the top-level controller (`get_resource_metadata`). This is crucial for actions like scaling deployments.
-   Defines a dictionary (`STATIC_PLAN_MAPPINGS`) holding predefined `RemediationPlan` templates for common anomaly types.
-   Updates the `AnomalyRecord` in MongoDB with the generated plan and its source (AI or static).

```python
# Example from kubewise/remediation/planner.py
async def generate_remediation_plan(
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    k8s_client: client.ApiClient,
    agent: Optional[Agent] = None,
) -> Optional[RemediationPlan]:
    """
    Generates a remediation plan using the following strategy:
    1. First attempt to generate a dynamic plan using Gemini AI (primary strategy)
    2. If Gemini fails or returns empty, fall back to static pre-configured plans
    """
    # ... gather context (events, resource metadata) ...
    # ... construct AI prompt ...
    # ... call agent.run(prompt, output_model=RemediationPlan) ...
    # ... if AI fails, call load_static_plan ...
    # ... update anomaly record in DB ...

async def load_static_plan(
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    k8s_client: Optional[client.ApiClient] = None
) -> Optional[RemediationPlan]:
    """
    Loads a predefined static remediation plan based on anomaly characteristics and metric patterns.
    """
    # ... determine plan based on metric_name and entity_type ...
    # ... substitute placeholders in the plan actions ...
    # ... update anomaly record in DB ...
```

#### `kubewise/remediation/engine.py`

This module contains the `execute_remediation_plan` function, which takes a `RemediationPlan` and executes the sequence of `RemediationAction` objects defined within it.

Key aspects:
-   **Action Registry:** Uses a `@register_action` decorator to map `ActionType` strings (e.g., `"scale_deployment"`, `"delete_pod"`) to specific asynchronous Python functions that interact with the Kubernetes API. This makes the engine extensible; new action types can be added by simply defining a new function and registering it.
-   **Action Execution:** Iterates through the actions in the plan. For each action, it looks up the corresponding handler in the `ACTION_REGISTRY` and executes it.
-   **Retry Logic:** Each action execution is wrapped with the `with_exponential_backoff` helper to automatically retry failing Kubernetes API calls or other transient errors.
-   **Timeout:** Individual action executions are wrapped with `asyncio.wait_for` to enforce a timeout (`ACTION_TIMEOUT`).
-   **Parallel Execution:** Can execute independent actions in parallel if the plan contains multiple actions and the number is within a defined limit (`MAX_PARALLEL_ACTIONS`).
-   **Logging:** Logs the outcome (success/failure, details) of each executed action as an `ExecutedActionRecord` in MongoDB, providing an audit trail.
-   Updates the final `remediation_status` on the `AnomalyRecord` in MongoDB based on the overall success of the plan execution.

```python
# Example from kubewise/remediation/engine.py
ACTION_REGISTRY: Dict[ActionType, ActionCoroutine] = {}

def register_action(action_type: ActionType) -> Callable[[ActionCoroutine], ActionCoroutine]:
    """
    Decorator to register a function as a handler for a specific remediation action.
    """
    def decorator(func: ActionCoroutine) -> ActionCoroutine:
        ACTION_REGISTRY[action_type] = func
        return func
    return decorator

@register_action("scale_deployment")
async def scale_deployment_action(
    api_client: client.ApiClient, params: Dict[str, Any]
) -> Tuple[bool, str]:
    """Scales a Kubernetes Deployment."""
    # ... Kubernetes API call using kubernetes-asyncio ...

async def execute_remediation_plan(
    plan: RemediationPlan,
    anomaly_record: AnomalyRecord,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    k8s_client: client.ApiClient,
) -> bool:
    """
    Executes the actions defined in a RemediationPlan with robust retry logic.
    """
    # ... validate plan ...
    # ... iterate through actions ...
    # ... execute action with timeout and retries (execute_action_with_timeout) ...
    # ... log action result (ExecutedActionRecord) ...
    # ... update final anomaly status ...
```

### 3.4 API & CLI

These components provide interfaces for interacting with the KubeWise system.

#### `kubewise/api/server.py`, `routers.py`, `deps.py`

This is the FastAPI-based REST API.
-   `server.py` contains the main `create_app` function, which sets up the FastAPI application, initializes shared resources (MongoDB client, Kubernetes client, HTTP client), starts background tasks (collectors, detector, remediation trigger), and defines the application lifespan events (startup/shutdown).
-   `routers.py` defines the API endpoints using FastAPI's APIRouter, separating concerns for different resource types (health, metrics, anomalies, config).
-   `deps.py` contains dependency injection functions (`Depends`) for providing access to shared resources like the MongoDB database, Kubernetes API client, HTTP client, and application settings within the API endpoints.
-   `context.py` defines a simple `AppContext` class to hold instances of shared resources, which is then provided via dependency injection.

Key aspects:
-   Uses `FastAPI` for building the asynchronous web API.
-   Leverages `pydantic` for request/response body validation and serialization.
-   Utilizes `lifespan` for managing application startup and shutdown logic, ensuring resources are properly initialized and cleaned up.
-   Runs background tasks (`run_prometheus_poller`, `run_detection_loop`, `run_remediation_trigger`) concurrently using `asyncio.create_task`.
-   Exposes standard health (`/health`, `/livez`) and metrics (`/metrics`) endpoints for monitoring.
-   Provides endpoints for listing anomalies (`/anomalies`) and viewing/updating configuration (`/config`).

```python
# Example from kubewise/api/server.py
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # ... initialize clients (MongoDB, K8s, HTTP) ...
    # ... create queues ...
    # ... initialize detector and fetcher ...
    # ... create background tasks (polling, detection, remediation) ...
    yield # Application is running
    # ... cancel tasks ...
    # ... close clients ...

def create_app() -> FastAPI:
    """Creates the FastAPI application instance."""
    app = FastAPI(lifespan=lifespan, ...)
    app.include_router(routers.router)
    # ... add metrics endpoint ...
    return app
```

#### `kubewise/cli.py`

This module provides a command-line interface using `Typer` for basic interactions with KubeWise.

Key aspects:
-   Uses `Typer` for building the CLI application.
-   Provides commands for displaying configuration (`config`), checking connections to Prometheus and Kubernetes (`check_connections`), and listing recent anomalies from the database (`list-anomalies`).
-   Uses `rich` for aesthetic command-line output (tables, colors).
-   Includes helper functions (`_get_db`) to establish database connections for CLI commands.
-   Uses `asyncio.run` within command functions to execute asynchronous logic, suitable for standalone CLI execution.

```python
# Example from kubewise/cli.py
app = typer.Typer(...)

@app.command()
def config():
    """Display the current KubeWise configuration."""
    # ... print settings using rich ...

@app.command(name="list-anomalies")
def list_anomalies_cmd(...):
    """List the most recent detected anomalies."""
    async def fetcher():
        # ... fetch anomalies from DB ...
        # ... display in a rich table ...
    asyncio.run(fetcher())
```

### 3.5 Models & Schema

#### `kubewise/models/schema.py`

This module defines the Pydantic models used throughout the application for data validation, serialization, and structured representation.

Key models:
-   `RemediationAction`: Represents a single action within a remediation plan, including its type (`ActionType` literal) and parameters. Includes a validator to check parameters based on the action type.
-   `RemediationPlan`: Represents a sequence of `RemediationAction`s generated by the AI or loaded statically, along with the reasoning.
-   `BaseKubeWiseModel`: A base class for other models, providing common Pydantic configuration (e.g., `populate_by_name`, `arbitrary_types_allowed` for `ObjectId`).
-   `MetricPoint`: Represents a single data point from Prometheus.
-   `KubernetesEvent`: Represents a relevant event from the Kubernetes API.
-   `AnomalyRecord`: Represents a detected anomaly, storing details about the anomaly, its scores, feature vector, and the state of its remediation (plan, status, result).
-   `ExecutedActionRecord`: Represents a log entry for a single executed remediation action, providing an audit trail.

```python
# Example from kubewise/models/schema.py
class RemediationAction(BaseModel):
    action_type: ActionType = Field(...)
    parameters: Dict[str, Union[str, int, bool]] = Field(...)

    @field_validator("parameters")
    @classmethod
    def check_parameters_for_action(cls, params: Dict[str, Any], values: Any) -> Dict[str, Any]:
        # ... validation logic ...

class AnomalyRecord(BaseKubeWiseModel):
    entity_id: str = Field(...)
    # ... other fields ...
    anomaly_score: float = Field(...)
    hst_score: float = Field(...)
    river_score: float = Field(...)
    feature_vector: Dict[str, float] = Field(...)
    remediation_plan: Optional[RemediationPlan] = Field(None)
    # ... remediation status fields ...
```

### 3.6 Configuration & Logging

#### `kubewise/config.py`

This module uses `pydantic-settings` to manage application configuration. Settings are loaded from environment variables or a `.env` file, providing type-safe access to parameters like MongoDB URI, Prometheus URL, Gemini API key, anomaly thresholds, and log level.

Key aspects:
-   Uses `pydantic-settings.BaseSettings` and `SettingsConfigDict` to define settings and their sources.
-   Uses Pydantic field types (`str`, `HttpUrl`, `SecretStr`, `Dict[str, float]`, `Literal`) for validation.
-   Includes a validator to ensure the log level is uppercase.
-   Defines default Prometheus queries and anomaly thresholds.
-   Creates a single `settings` instance to be imported and used throughout the application.

```python
# Example from kubewise/config.py
class Settings(BaseSettings):
    mongo_uri: str = Field(..., validation_alias="MONGO_URI")
    prom_url: HttpUrl = Field(..., validation_alias="PROM_URL")
    gemini_api_key: SecretStr = Field(..., validation_alias="GEMINI_API_KEY")
    anomaly_thresholds: Dict[str, float] = Field(default_factory=lambda: {"default": 0.8}, validation_alias="ANOMALY_THRESHOLDS")
    log_level: LogLevel = Field("INFO", validation_alias="LOG_LEVEL")
    # ... other settings ...

    model_config = SettingsConfigDict(env_file=".env", ...)

settings = Settings()
```

#### `kubewise/logging.py`

This module configures the application's logging using `loguru` and `rich`.

Key aspects:
-   Uses `loguru` for flexible and structured logging.
-   Integrates `rich.logging.RichHandler` for aesthetic and readable console output with colors and formatting.
-   Defines a standard log format.
-   Sets up file logging with rotation and retention policies, writing to a configurable directory (`LOG_DIR`).
-   Implements an `InterceptHandler` to capture standard library logging messages (e.g., from `uvicorn`, `httpx`, `kubernetes-asyncio`) and redirect them to Loguru, ensuring consistent logging output.
-   Configures the logging level based on the `settings.log_level`.

```python
# Example from kubewise/logging.py
from loguru import logger
from rich.logging import RichHandler

class InterceptHandler(logging.Handler):
    # ... redirects standard logs to loguru ...

def setup_logging() -> None:
    """Configure Loguru logger based on application settings."""
    logger.remove() # Remove default handler
    logger.add(RichHandler(...), level=settings.log_level.upper(), ...) # Console handler
    # ... add file handler ...
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True) # Intercept standard logging
```

## 4. Core Concepts & Design Patterns

### 4.1 Asynchronous Programming (`asyncio`)

KubeWise is built using Python's `asyncio` library to handle concurrent I/O operations efficiently. This is critical for interacting with external services like the Kubernetes API, Prometheus, and MongoDB without blocking the main event loop. Key components like the collectors, detector loop, and remediation engine are implemented as asynchronous tasks.

### 4.2 Dependency Injection

The FastAPI application (`kubewise/api`) utilizes dependency injection (`FastAPI.Depends`) to provide access to shared resources such as the MongoDB database client, Kubernetes API client, and application settings. This promotes modularity and testability.

### 4.3 Retry Mechanisms (`with_exponential_backoff`)

A common pattern observed across the codebase, particularly in the collectors and state persistence logic, is the use of the `@with_exponential_backoff` decorator. This decorator provides a standardized way to add robust retry logic with exponential backoff and jitter to asynchronous functions that might fail due to transient network issues or service unavailability.

```python
# Example of decorator usage
@with_exponential_backoff(max_retries=5, retry_on_exceptions=(httpx.RequestError, asyncio.TimeoutError))
async def query_prometheus(...):
    # ... function logic that might fail ...
```

### 4.4 State Persistence

The anomaly detection models and the event watcher's `resourceVersion` are stateful components that need to persist their state to survive application restarts. This is achieved by serializing the state (using `pickle`, with `zlib` compression for the potentially larger HST model) and storing it as binary data in a dedicated collection (`_kubewise_state`) in MongoDB.

### 4.5 Data Modeling with Pydantic

Pydantic is used extensively for defining data structures (`MetricPoint`, `KubernetesEvent`, `AnomalyRecord`, etc.) and validating data, especially when interacting with external APIs (Kubernetes, Prometheus) and the database. It ensures data integrity and provides clear data contracts between different parts of the system. The use of `model_validate` and `model_dump` (with `by_alias=True` for MongoDB compatibility) is prevalent.

### 4.6 Action Registry Pattern

The remediation engine uses a simple action registry pattern (`ACTION_REGISTRY` and `@register_action` decorator) to map action types defined in the `RemediationAction` model to specific execution functions. This allows for easy extension of the remediation capabilities by adding new action handler functions without modifying the core execution logic.

## 5. Technology Stack Integration

KubeWise integrates several key Python libraries:
-   **FastAPI:** For building the asynchronous REST API.
-   **`kubernetes-asyncio`:** For asynchronous interaction with the Kubernetes API.
-   **`httpx`:** For asynchronous HTTP requests, used by the Prometheus collector and potentially other parts needing HTTP communication.
-   **`river`:** Provides online machine learning algorithms for anomaly detection (Half-Space Trees, Hoeffding Trees, scalers).
-   **`pydantic-ai`:** Facilitates interaction with AI models (specifically Google Gemini) for structured output generation (remediation plans).
-   **`motor`:** An asynchronous driver for MongoDB, used for state persistence and storing anomaly/action records.
-   **`Typer`:** For building the command-line interface.
-   **`loguru`:** For flexible and structured logging.
-   **`rich`:** For aesthetic console output in the CLI and enhanced logging.
-   **`pydantic-settings`:** For loading application configuration from environment variables.

## 6. Conclusion

KubeWise is a well-structured application that effectively leverages asynchronous programming and modern Python libraries to build a robust Kubernetes anomaly detection and self-remediation system. The codebase demonstrates clear separation of concerns, utilizes design patterns like dependency injection and action registries, and incorporates essential features like retry mechanisms and state persistence for resilience. The use of Pydantic for data modeling and validation, combined with structured logging, contributes to the maintainability and observability of the system. The architecture is designed to be extensible, particularly in the areas of collectors, anomaly detection models, and remediation actions.
