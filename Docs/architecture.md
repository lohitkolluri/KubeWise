# KubeWise Architecture

This document outlines the architecture of the KubeWise system, detailing the flow of data from collection to remediation.

## Overview

KubeWise is designed as an autonomous system that monitors Kubernetes clusters, detects anomalies in real-time using online machine learning, and attempts automated remediation based on AI-generated plans.

## Data Flow Diagram

```mermaid
graph TD
    subgraph "Kubernetes Cluster"
        K8sAPI[Kubernetes API Server]
        Prometheus[Prometheus Instance]
        AppPods[Application Pods] -->|Metrics| Prometheus
        K8sAPI -- Events --> K8sWatcher(K8s Event Watcher)
    end

    subgraph "KubeWise System"
        direction LR
        subgraph "Collectors"
            PromWatcher(Prometheus Poller) -- Fetches Metrics --> MetricQueue[Metric Queue]
            K8sWatcher -- Streams Events --> EventQueue[Event Queue]
        end

        subgraph "Processing & Detection"
            Detector[Online Anomaly Detector <br/> (River HST + Scaler)]
            MetricQueue --> Detector
            EventQueue --> Detector
            Detector -- Stores Anomalies --> MongoDB[(MongoDB Atlas)]
            Detector -- Triggers Remediation --> RemediationQueue[Remediation Queue]
        end

        subgraph "Remediation"
            RemediationTrigger(Remediation Trigger) -- Reads Queue --> RemediationQueue
            Planner[AI Planner <br/> (Pydantic-AI + Gemini)]
            Engine[Remediation Engine <br/> (DSL Executor)]

            RemediationTrigger -- Gets Anomaly Context --> MongoDB
            RemediationTrigger -- Gets Resource State --> K8sAPIClient(K8s API Client)
            RemediationTrigger -- Sends Context --> Planner
            Planner -- Generates Plan --> RemediationTrigger
            RemediationTrigger -- Updates Plan --> MongoDB
            RemediationTrigger -- Sends Plan --> Engine
            Engine -- Executes Actions via --> K8sAPIClient
            Engine -- Logs Actions --> MongoDB
        end

        subgraph "API & CLI"
            FastAPI[FastAPI Server]
            TyperCLI[Typer CLI]
            FastAPI -- Reads/Updates Config --> Config(Settings)
            FastAPI -- Reads Anomalies --> MongoDB
            FastAPI -- Exposes --> Health[/health, /livez]
            FastAPI -- Exposes --> Metrics[/metrics]
            FastAPI -- Exposes --> AnomaliesAPI[/anomalies]
            FastAPI -- Exposes --> ConfigAPI[/config]
            TyperCLI -- Interacts via --> FastAPI # Or directly with components for some actions
            TyperCLI -- Reads --> Config
            TyperCLI -- Reads --> MongoDB
        end

        subgraph "Observability"
            PrometheusExporter[Prometheus Exporter]
            CircuitBreakers[Circuit Breakers]
            HealthChecks[Health Checks]
            GracefulShutdown[Graceful Shutdown]
            
            FastAPI --> PrometheusExporter
            CircuitBreakers --> K8sAPIClient
            CircuitBreakers --> MongoDB
            CircuitBreakers --> PromWatcher
            HealthChecks --> FastAPI
            GracefulShutdown --> FastAPI
        end

        Config -- Reads --> EnvVars[.env / Environment]
        MongoDB -- Stores --> AnomalyData[Anomaly Records]
        MongoDB -- Stores --> ActionLogs[Executed Action Logs]
        MongoDB -- Stores --> EventData[Raw Event Data (Optional)]

    end

    style MongoDB fill:#4DB33D,stroke:#333,stroke-width:2px,color:#fff
    style K8sAPI fill:#326CE5,stroke:#333,stroke-width:2px,color:#fff
    style Prometheus fill:#E6522C,stroke:#333,stroke-width:2px,color:#fff
    style FastAPI fill:#009688,stroke:#333,stroke-width:2px,color:#fff
    style Planner fill:#FFC107,stroke:#333,stroke-width:2px,color:#000
    style Detector fill:#9C27B0,stroke:#333,stroke-width:2px,color:#fff
    style CircuitBreakers fill:#FF5722,stroke:#333,stroke-width:2px,color:#fff
    style PrometheusExporter fill:#E91E63,stroke:#333,stroke-width:2px,color:#fff

```

*(To generate the SVG for `docs/images/architecture.svg` and the README, copy the Mermaid code above and use a tool like the Mermaid Live Editor ([https://mermaid.live](https://mermaid.live)) or a command-line tool (`mmdc`) to export it as an SVG file.)*

## Component Breakdown

1.  **Collectors:**
    *   **Prometheus Poller (`prometheus.py`):** Periodically queries a Prometheus instance (defined by `PROM_URL`) for configured metrics (e.g., CPU, memory usage). Parsed metrics (`MetricPoint`) are placed onto the `metric_queue`. Runs as an asynchronous background task.
    *   **K8s Event Watcher (`k8s_events.py`):** Establishes a long-lived watch stream with the Kubernetes API server. Filters for relevant event types (e.g., `Warning`) and reasons (`Failed`, `CrashLoopBackOff`). Parsed events (`KubernetesEvent`) are placed onto the `event_queue`. Runs as an asynchronous background task. *(Optionally, relevant events could also be persisted directly to MongoDB for later querying by the Planner).*

2.  **Processing & Detection:**
    *   **Anomaly Detector (`detector.py`):** Consumes metrics and events from their respective queues.
        *   **Feature Extraction:** Transforms incoming data points into feature vectors suitable for the anomaly detection model. This involves mapping metrics to features and potentially using event data to update stateful features (like recent warning counts) associated with specific Kubernetes resources (e.g., pods).
        *   **Detector Types:**
            *   **OnlineAnomalyDetector:** Base detector using a dual-model approach:
                *   `preprocessing.MinMaxScaler`: Scales features to a [0, 1] range, learned online.
                *   `anomaly.HalfSpaceTrees`: An online anomaly detection algorithm that scores incoming feature vectors.
                *   A secondary `AdaptiveRandomForestClassifier` from River for supervised classification.
            *   **SequentialAnomalyDetector:** Enhanced detector that builds on the base functionality:
                *   Adds temporal pattern recognition to identify problematic sequences of events.
                *   Implements trend detection to catch gradually worsening metrics.
                *   Maintains historical context for each entity to identify recurring patterns.
                *   Uses predefined critical sequences and metric patterns for known failure modes.
        *   **Scoring & Thresholding:** Compares the anomaly score against the configurable thresholds, which are differentiated by metric type (CPU, memory, network, etc.).
        *   **Persistence:** If an anomaly is detected (score > threshold), an `AnomalyRecord` containing the score, features, timestamp, threshold, and triggering data is created and persisted to the `anomalies` collection in MongoDB Atlas.
        *   **Triggering Remediation:** The persisted `AnomalyRecord` (including its database ID) is placed onto the `remediation_queue`.
    *   Runs as an asynchronous background task (`detection_loop` in `server.py`).

3.  **Remediation:**
    *   **Remediation Trigger (`server.py`):** Consumes `AnomalyRecord` objects from the `remediation_queue`. Orchestrates the planning and execution steps. Runs as an asynchronous background task.
    *   **Context Gathering (`planner.py`):** Before calling the AI, the trigger fetches relevant context:
        *   Recent related Kubernetes events (from MongoDB or live API query).
        *   Current status/metadata of the affected Kubernetes resource (via Kubernetes API client).
    *   **AI Planner (`planner.py`):**
        *   Uses `pydantic-ai`'s `Agent` configured with a Google Gemini model (`GEMINI_API_KEY`, `GEMINI_MODEL_ID` from settings).
        *   Constructs a detailed prompt containing the anomaly details, recent events, and resource metadata.
        *   Invokes the LLM to generate a structured `RemediationPlan` (defined in `schema.py`), which includes the LLM's reasoning and a list of `RemediationAction` objects.
        *   Updates the `AnomalyRecord` in MongoDB with the generated plan and sets the status to `generated`.
    *   **Remediation Engine (`engine.py`):**
        *   Receives the `RemediationPlan` from the trigger.
        *   Iterates through the `actions` in the plan.
        *   Uses a registry (`@register_action`) to find the appropriate Python function for each `action_type`.
        *   Executes the action function (e.g., `scale_deployment_action`, `delete_pod_action`) using the Kubernetes API client and the provided `parameters`.
        *   Logs the outcome (success/failure, details) of each executed action to the `executed_actions` collection in MongoDB.
        *   Updates the final `remediation_status` and `remediation_error` (if any) on the `AnomalyRecord` in MongoDB.

4.  **API & CLI:**
    *   **FastAPI Server (`server.py`, `routers.py`, `deps.py`):**
        *   Provides RESTful endpoints (`/health`, `/livez`, `/metrics`, `/anomalies`, `/config`).
        *   Manages application lifecycle (startup/shutdown) using `lifespan`.
        *   Initializes shared resources (DB client, K8s client) and background tasks.
        *   Uses dependency injection (`Depends`) to provide access to resources like the database.
    *   **Typer CLI (`cli.py`):**
        *   Offers command-line tools for interacting with the system (e.g., viewing config, listing anomalies, checking connections).
        *   Connects directly to MongoDB or uses helper functions where appropriate.

5.  **Configuration (`config.py`):**
    *   Uses `pydantic-settings` to load configuration from environment variables or a `.env` file.
    *   Provides type-safe access to settings like `MONGO_URI`, `PROM_URL`, `GEMINI_API_KEY`, `ANOMALY_THRESHOLD`, etc.

6.  **Persistence (MongoDB Atlas):**
    *   Stores `AnomalyRecord` objects.
    *   Stores `ExecutedActionRecord` objects.
    *   (Optionally) Could store processed `KubernetesEvent` objects.

## Key Design Principles

*   **Asynchronous:** Uses `asyncio` extensively for I/O-bound operations (API calls, DB interactions, event watching).
*   **Online Learning:** Employs River ML for incremental learning and anomaly detection without requiring batch retraining.
*   **Modularity:** Components are separated into distinct modules (collectors, detector, planner, engine, api).
*   **Configuration Driven:** Key parameters are managed via environment variables/`.env` file.
*   **AI-Powered Remediation:** Leverages an LLM via Pydantic-AI to generate structured, context-aware remediation plans.
*   **Declarative Actions:** Uses a simple DSL for remediation actions, making the engine extensible.
*   **Observability:** Provides standard `/health`, `/livez`, and `/metrics` endpoints. Uses Loguru for structured logging.
*   **Resilience:** Implements circuit breakers and retry mechanisms to handle external dependencies failures gracefully.
*   **Graceful Shutdown:** Ensures proper cleanup of resources during termination.

## Resilience Patterns

### Circuit Breaker Pattern

KubeWise implements the circuit breaker pattern for calls to external services, providing increased resilience when those services are experiencing issues:

1. **Components:**
   * Implemented in `kubewise/utils/retry.py` as the `with_circuit_breaker` decorator
   * Applied to Prometheus queries, Kubernetes API operations, and MongoDB database operations

2. **States:**
   * **Closed:** Normal operation - requests pass through to the service
   * **Open:** After `ERROR_THRESHOLD` consecutive failures, requests are immediately rejected without attempting to call the failing service
   * **Half-Open:** After `CIRCUIT_OPEN_TIMEOUT` seconds in the Open state, allows up to `HALF_OPEN_MAX_CALLS` test requests to verify service recovery

3. **Metrics:**
   * Circuit breaker status is exposed via Prometheus metrics (`kubewise_circuit_breaker_status`)
   * External service availability is tracked (`kubewise_external_service_up`)

4. **Benefits:**
   * Prevents cascading failures when a dependency is degraded
   * Provides automatic recovery when services become available again
   * Reduces system load during partial outages

### Retry with Exponential Backoff

For transient errors, KubeWise uses exponential backoff retry logic:

1. **Implementation:**
   * `with_exponential_backoff` decorator in `kubewise/utils/retry.py`
   * Configurable parameters for initial delay, max delay, backoff factor, and jitter

2. **Features:**
   * Preserves original exception context for better error reporting
   * Adds random jitter to prevent thundering herd problems
   * Detailed logging of retry attempts and outcomes

## Observability Features

### Metrics

KubeWise exposes comprehensive metrics via Prometheus for monitoring system health and performance:

1. **Application Metrics:**
   * `kubewise_anomalies_detected_total`: Anomaly counts by source, entity type, and namespace
   * `kubewise_false_positives_total`: False positive counts for anomaly detection
   * `kubewise_remediations_total`: Remediation action counts by type, entity, namespace, and status
   * `kubewise_remediation_duration_seconds`: Time required to execute remediation actions

2. **API Metrics:**
   * `kubewise_api_request_duration_seconds`: API request latency by endpoint, method, and status code

3. **External Service Metrics:**
   * `kubewise_external_request_duration_seconds`: External service call durations
   * `kubewise_external_request_errors_total`: External service error counts
   * `kubewise_circuit_breaker_status`: Circuit breaker status for each external service
   * `kubewise_external_service_up`: External service availability status

### Health Checks

The system provides multiple health check endpoints:

1. **Basic Health:** `GET /health` returns service status and uptime
2. **Liveness Probe:** `GET /livez` for Kubernetes liveness probes
3. **Enhanced Health:** `GET /health/extended` provides detailed component status including:
   * Database connectivity
   * Kubernetes API connectivity
   * Prometheus connectivity
   * Queue sizes and processing status
   * Memory usage statistics
   * Last successful operations for each component

### Graceful Shutdown

KubeWise implements a structured shutdown sequence to ensure proper resource cleanup:

1. **Shutdown Flow:**
   * Stop accepting new API requests
   * Complete in-flight requests (with timeout)
   * Drain the queues in order: metrics/events, remediation, execution
   * Stop collectors (Prometheus poller, K8s event watcher)
   * Stop processors (anomaly detector, remediation engine)
   * Close external clients (K8s, MongoDB, HTTP)

2. **Implementation:**
   * Uses FastAPI's `lifespan` context manager for application lifecycle
   * Implements comprehensive exception handling and timeout management
   * Cancels background tasks in correct dependency order
   * Properly closes database connections

This ensures clean shutdown during container termination, deployment updates, or regular process restarts.
