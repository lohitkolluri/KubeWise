# KubeWise Architecture

This document outlines the architecture of the KubeWise system, detailing the core components, data flow patterns, and fundamental design principles.

## Overview

KubeWise is designed as an autonomous system that monitors Kubernetes clusters, detects anomalies in real-time using online machine learning, and implements automated remediation based on AI-generated plans.

## Architectural Principles

KubeWise adheres to several core architectural principles:

1. **Event-Driven Design:** The system operates on event streams and message queues, enabling loose coupling between components and asynchronous processing.

2. **Online Learning:** Unlike traditional batch-oriented machine learning approaches, KubeWise uses online learning algorithms that continuously adapt to incoming data streams without requiring periodic retraining.

3. **Separation of Concerns:** Components are modularized with clear responsibilities - collection, detection, planning, and execution are distinct phases.

4. **Kubernetes-Native Design:** The architecture leverages Kubernetes patterns and API semantics, treating it as both a platform and a first-class integration point.

5. **Observability by Default:** All components expose metrics, health information, and detailed logs for comprehensive monitoring.

6. **Resilient Communication:** All external dependencies implement circuit breakers and exponential backoff patterns to handle transient failures gracefully.

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
            TyperCLI -- Interacts via --> FastAPI
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

## Component Architecture

### 1. Collectors Layer

The collectors layer is responsible for gathering observability data from various sources and transforming it into standardized formats:

* **Prometheus Poller:**
  * Implements a polling pattern to query Prometheus time-series data
  * Transforms metrics into normalized representations with consistent units and formats
  * Operates asynchronously with configurable intervals to manage load
  * Employs circuit breakers to handle Prometheus unavailability

* **Kubernetes Event Watcher:**
  * Establishes a long-lived watch connection to the Kubernetes API
  * Filters events by type, reason, and resource kinds
  * Normalizes event data into a consistent schema
  * Implements reconnection logic for watch stream failures

Both collectors feed their data into respective in-memory queues for further processing, effectively decoupling the collection process from the analysis process.

### 2. Detection Layer

The detection layer consumes normalized metrics and events from the collection layer queues:

* **Feature Engineering:**
  * Extracts relevant features from raw metrics and events
  * Combines metrics with event context for holistic analysis
  * Maps Kubernetes resource hierarchies to correctly associate metrics
  * Performs real-time feature scaling and normalization

* **Anomaly Detection Models:**
  * **OnlineAnomalyDetector:** 
    * Implements River ML's Half-Space Trees for anomaly score generation
    * Uses adaptive MinMax scaling for data normalization
    * Leverages AdaptiveRandomForestClassifier for supervised classification
    * Maintains separate models per metric type for specialized detection

  * **SequentialAnomalyDetector:**
    * Extends base detection with temporal sequence analysis
    * Recognizes patterns over time windows rather than point anomalies
    * Implements trend detection for gradually degrading conditions
    * Maintains entity-specific historical context for improved accuracy
  
* **Thresholding Framework:**
  * Applies metric-specific configurable thresholds
  * Implements dynamic thresholding for adaptive sensitivity
  * Uses temporal dampening to prevent flapping detections

When anomalies are detected, they are both persisted to MongoDB and enqueued for remediation consideration.

### 3. Remediation Layer

The remediation layer is responsible for analyzing detected anomalies and implementing appropriate corrective actions:

* **Context Gathering:**
  * Retrieves comprehensive context about the anomalous entity
  * Collects recent related events to understand the anomaly timeline
  * Queries Kubernetes API for current resource state
  * Builds a complete contextual picture for intelligent planning

* **AI Planning System:**
  * Leverages Gemini large language model through Pydantic-AI
  * Constructs detailed prompts with anomaly context and system state
  * Guides the LLM to generate structured remediation plans
  * Validates generated plans against schema definitions
  * Applies safety constraints to prevent destructive actions

* **Remediation Engine:**
  * Implements a Domain-Specific Language (DSL) for standardized actions
  * Maps action declarations to concrete Kubernetes operations
  * Uses an extensible registry pattern for adding new action types
  * Implements transaction-like semantics with partial rollback capabilities
  * Enforces cooldown periods to prevent action flooding

* **Action Types:**
  * **Resource Scaling:** Adjusting replica counts for deployments
  * **Pod Management:** Restarting or deleting problematic pods
  * **Resource Requests/Limits:** Adjusting resource configurations
  * **External System Notifications:** Alerting external systems about issues

### 4. Interface Layer

The interface layer provides consistent access points for interacting with the system:

* **FastAPI Server:**
  * Implements RESTful endpoints for system interaction
  * Provides comprehensive OpenAPI documentation
  * Manages application lifecycle and resource initialization
  * Implements structured error handling and validation
  * Uses dependency injection for access to shared resources

* **Typer CLI:**
  * Offers command-line tools for operational tasks
  * Provides intuitive interface for system interaction
  * Enables scriptable automation of routine operations

### 5. Persistence Layer

The persistence layer handles data storage and retrieval:

* **MongoDB Atlas:**
  * Stores structured records with flexible schema evolution
  * Enables high-performance querying with proper indexing
  * Supports asynchronous operations for non-blocking I/O
  * Implements connection pooling for efficient resource usage

* **Data Models:**
  * **AnomalyRecord:** Captures anomaly details, scores, context, and remediation status
  * **ExecutedActionRecord:** Logs remediation actions, parameters, and outcomes
  * **EventRecord:** Optionally stores relevant Kubernetes events

## Resilience Patterns

### Circuit Breaker Pattern

KubeWise implements the circuit breaker pattern for external service interactions:

1. **State Machine:**
   * **Closed:** Normal operation with requests passing through
   * **Open:** After failure threshold is reached, immediately rejects requests
   * **Half-Open:** After timeout period, allows limited test requests

2. **Implementation:**
   * Decorates critical external-facing functions
   * Tracks failure counts and success rates
   * Implements timeout-based state transitions
   * Exposes state as observable metrics

3. **Application:**
   * Applied to Prometheus queries, Kubernetes API operations, and MongoDB database access
   * Prevents overwhelming already struggling services
   * Enables faster recovery during partial outages
   * Provides automatic service recovery testing

### Retry with Exponential Backoff

For transient failures that may resolve quickly:

1. **Strategy:**
   * Initial retry delay with exponential growth
   * Maximum retry limit to prevent infinite attempts
   * Random jitter to prevent thundering herd problems

2. **Implementation:**
   * Decorator-based application to critical functions
   * Configurable parameters for timeout and retry counts
   * Preserves exception context for accurate error reporting

## Observability Architecture

### Metrics Framework

KubeWise exposes comprehensive metrics for operational visibility:

1. **Metric Categories:**
   * **Application Metrics:** Anomaly detection rates, remediation success
   * **API Metrics:** Request durations, error rates, endpoint usage
   * **External Service Metrics:** Dependency health, response times
   * **Resource Metrics:** Memory usage, queue sizes, processing rates

2. **Implementation:**
   * Prometheus client integration
   * Labeled metrics for detailed segmentation
   * Counters, gauges, histograms, and summaries as appropriate
   * Consistent naming convention for better dashboarding

### Health Check System

Multiple health check levels for different monitoring needs:

1. **Basic Health:** Quick liveness verification
2. **Detailed Health:** Component-level status reporting
3. **Dependency Health:** External system connectivity verification

### Graceful Shutdown Architecture

Structured shutdown process for clean termination:

1. **Signal Handling:**
   * Captures termination signals (SIGTERM, SIGINT)
   * Initiates orderly shutdown sequence

2. **Shutdown Sequence:**
   * API server stops accepting new requests
   * In-flight requests complete with timeout
   * Queues drain in dependency order
   * Background tasks cancel in correct sequence
   * External connections close properly

3. **Implementation:**
   * FastAPI lifespan context managers
   * Asyncio task management and cancellation
   * Timeout-based safety mechanisms

## Data Models

### Core Data Structures

1. **NormalizedMetric:**
   * Standard representation of metrics regardless of source
   * Contains entity references, metric type, value, and timestamp
   * Provides consistent interface for detector components

2. **AnomalyRecord:**
   * Captures detected anomalies with comprehensive context
   * Stores anomaly score, feature vector, threshold, and timestamp
   * Tracks remediation status, plan, and execution results
   * Links to related events and executed actions

3. **RemediationPlan:**
   * Structured Pydantic model for AI-generated plans
   * Contains reasoning, action sequence, and priority
   * Includes safety constraints and verification steps
   * Maps to concrete Kubernetes API operations

4. **ExecutedActionRecord:**
   * Logs all remediation actions taken
   * Records success/failure status and detailed results
   * Maintains audit trail for compliance and analysis
   * Links back to originating anomaly

### Schema Evolution Strategy

KubeWise implements a schema evolution approach that allows:

1. **Backward Compatibility:** New code can read old data
2. **Forward Compatibility:** Old code can read new data (ignoring new fields)
3. **Migration Support:** For substantial schema changes

This is achieved through:

* Pydantic models with optional fields
* MongoDB's flexible document structure
* Version fields on persisted records
* Default values for evolving schemas

## Operational Characteristics

### Asynchronous Processing

KubeWise leverages Python's asyncio throughout the codebase:

1. **Benefits:**
   * Non-blocking I/O for high concurrency
   * Efficient resource utilization
   * Simplified concurrency model
   * Natural fit for event-driven architecture

2. **Implementation:**
   * Async API clients (kubernetes-asyncio, motor)
   * AsyncIO queues for inter-component communication
   * FastAPI for asynchronous HTTP endpoints
   * Structured task management for background processes

### Resource Efficiency

1. **Memory Management:**
   * Streaming processing to avoid large in-memory datasets
   * Efficient data structures for frequent operations
   * Connection pooling for external services
   * Garbage collection-friendly object lifecycle

2. **CPU Optimization:**
   * River ML's efficient online algorithms
   * Batched database operations
   * Throttled polling frequencies for external services
   * Event-driven processing to minimize idle CPU

3. **I/O Efficiency:**
   * Connection pooling for MongoDB
   * Kubernetes watch API instead of polling
   * HTTP connection reuse through HTTPX client
   * Batched database operations where possible

### Scalability Considerations

1. **Vertical Scalability:**
   * Configurable worker threads and process counts
   * Memory tuning parameters for different workload sizes
   * Adjustable queue sizes and batch processing parameters

2. **Horizontal Scalability:**
   * Stateless API design enables multiple replicas
   * MongoDB Atlas for scalable persistence
   * Independent scaling of collection, detection, and remediation components

## Security Model

1. **Authentication & Authorization:**
   * Kubernetes RBAC for API access control
   * MongoDB connection string authentication
   * API key for Gemini model access
   * FastAPI endpoint authorization

2. **Secure Communications:**
   * TLS for all external communications
   * Kubernetes API TLS certificate validation
   * MongoDB TLS/SSL connection

3. **Least Privilege:**
   * Kubernetes service account with minimal permissions
   * Database user with required access only
   * Container runs as non-root user

4. **Secret Management:**
   * Environment variables for local configuration
   * Kubernetes Secrets for deployment
   * No hard-coded credentials
   * Masking of sensitive values in logs and API responses

## Deployment Model

KubeWise is designed for Kubernetes deployment with:

1. **Kubernetes Manifests:**
   * Deployment for core application
   * Service for API access
   * RBAC resources for permissions
   * ConfigMap/Secrets for configuration

2. **Docker Containers:**
   * Multi-stage builds for minimal image size
   * Non-root user for security
   * Health checks for proper orchestration
   * Resource requests and limits for scheduler efficiency

3. **Configuration:**
   * Environment variables with sensible defaults
   * Kubernetes ConfigMap for non-sensitive configuration
   * Kubernetes Secrets for sensitive values
   * Runtime configuration updates through API
