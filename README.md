# KubeWise ☸️🧠

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Linter: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Imports: isort](https://img.shields.io/badge/%20imports-isort-%231674b1?style=flat&labelColor=ef8336)](https://pycqa.github.io/isort/)
[![Types: MyPy](https://img.shields.io/badge/types-MyPy%20(strict)-blue.svg)](https://mypy-lang.org/)
<!-- Add build/test status badges once CI/CD is set up -->
<!-- [![Build Status](...)](...) -->
<!-- [![Test Coverage](...)](...) -->

**Autonomous Kubernetes Anomaly Detection & Self-Remediation**

KubeWise monitors Kubernetes clusters using Prometheus metrics and Kubernetes API events, detects anomalies using online machine learning (River ML), and attempts automated remediation using AI-generated plans (Pydantic-AI + Gemini) executed via a DSL.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Technology Stack](#technology-stack)
- [Prerequisites](#prerequisites)
- [Getting Started](#getting-started)
  - [Configuration](#configuration)
  - [Development Environment with Docker Compose](#development-environment-with-docker-compose)
  - [Running Locally](#running-locally)
- [API Endpoints](#api-endpoints)
- [CLI Usage](#cli-usage)
- [Kubernetes Deployment](#kubernetes-deployment)
- [Development](#development)
  - [Testing](#testing)
  - [Linting & Formatting](#linting--formatting)
- [Contributing](#contributing)
- [License](#license)
- [Observability](#observability)
  - [Health Checks](#health-checks)
  - [Metrics](#metrics)
  - [Circuit Breaker Pattern](#circuit-breaker-pattern)
  - [Graceful Shutdown](#graceful-shutdown)
- [Deployment](#deployment)
  - [Docker Deployment](#docker-deployment)

---

## Features

*   **Real-time Monitoring:** Continuously collects metrics (CPU, memory, etc.) from Prometheus and streams critical events directly from the Kubernetes API.
*   **Online Anomaly Detection:** Employs River ML's Half-Space Trees (HST), an online algorithm ideal for streaming data. It detects anomalies in real-time using adaptive MinMax scaling without requiring periodic batch retraining. Scores indicate the degree of abnormality, compared against configurable thresholds. Includes heuristics to reduce potential false positives.
*   **Advanced Anomaly Detection:** Offers two detector implementations:
    *   **OnlineAnomalyDetector:** Employs River ML's Half-Space Trees (HST) and Adaptive Random Forest, ideal for streaming data. It detects anomalies in real-time using adaptive MinMax scaling without requiring periodic batch retraining.
    *   **SequentialAnomalyDetector:** Enhances detection by adding temporal context analysis, sequential pattern recognition, and trend detection to identify problematic patterns that develop over time.
*   **Standardized Metrics:** Fetched metrics and event data are processed and standardized into a consistent format (`NormalizedMetric`) for reliable detection and easier interpretation (see `kubewise/utils.py`).
*   **AI-Powered Remediation Planning:** Leverages Pydantic-AI integrated with a Google Gemini model. It analyzes anomaly details, recent cluster events, and current resource status to generate structured, context-aware `RemediationPlan` objects.
*   **Declarative Action DSL:** Executes generated remediation plans through a simple, extensible Domain-Specific Language (DSL) engine. This engine translates plan steps into concrete actions interacting with the Kubernetes API. Currently supports `scale_deployment` (adjust replicas) and `delete_pod`. Includes a configurable cooldown period between actions on the same resource.
*   **Persistence:** Stores detected anomalies, generated plans, and executed remediation actions with their outcomes in MongoDB Atlas for history and analysis.
*   **REST API:** Exposes a FastAPI interface for health checks (`/health`, `/livez`), internal Prometheus metrics (`/metrics`), listing detected anomalies (`/anomalies`), and viewing/updating core configuration (`/config`).
*   **CLI:** Provides a Typer-based command-line interface for essential operations like checking configuration, verifying external connections (Prometheus, K8s, MongoDB), and listing recent anomalies.
*   **Observability:** Utilizes structured logging via Loguru for clear operational insights and standard health/metrics endpoints for integration with monitoring systems.
*   **Kubernetes Native:** Includes standard Kubernetes manifests for Deployment, Service, ServiceAccount, and necessary RBAC ClusterRoles/Bindings.

---

## Architecture

KubeWise operates through several interconnected components:

![KubeWise Architecture Diagram](docs/images/architecture.svg)
*(Note: You need to generate `architecture.svg` from the Mermaid code in `docs/architecture.md`)*

1.  **Collectors (`kubewise/collector`):** Poll Prometheus for metrics at regular intervals and maintain a watch stream for Kubernetes API events. Data is standardized into `NormalizedMetric` objects.
2.  **Detector (`kubewise/models/detector.py`):** Processes the stream of `NormalizedMetric` objects. Offers two implementations:
    
    * **OnlineAnomalyDetector:** For each metric, applies online MinMax scaling and feeds the value into River ML models. If the resulting anomaly score exceeds the configured threshold (`ANOMALY_THRESHOLDS`), it's flagged.
    
    * **SequentialAnomalyDetector:** Extends the base detector with temporal pattern analysis, tracking event sequences and metric trends over time to identify problematic patterns that might not be detected by point-in-time analysis.
    
    Heuristics attempt to filter false positives. Detected anomalies are persisted to MongoDB and queued for remediation.
3.  **Remediation Trigger & Context Gathering (`kubewise/remediation/planner.py`):** Monitors the anomaly queue. When an anomaly is picked up, it gathers relevant context: the anomaly details, recent Kubernetes events related to the affected resource, and the current status (e.g., Pod details, Deployment status) from the K8s API.
4.  **AI Planner (`kubewise/remediation/planner.py`):** Packages the anomaly and gathered context into a prompt for the configured Gemini model via Pydantic-AI. The goal is to generate a structured `RemediationPlan` Pydantic object containing a sequence of actions.
5.  **Remediation Engine (`kubewise/remediation/engine.py`):** Receives the `RemediationPlan`. It iterates through the plan's actions, executing each one using a Domain-Specific Language (DSL) registry that maps action names (e.g., `scale_deployment`) to functions interacting with the Kubernetes API client (`kubernetes-asyncio`). A cooldown period (`REMEDIATION_COOLDOWN_SECONDS`) prevents rapid-fire actions on the same resource. Execution results (success/failure) are logged back to MongoDB.
6.  **API/CLI (`kubewise/api`, `kubewise/cli.py`):** Provide interfaces for user interaction, configuration management, and observability (health checks, metrics).

For a detailed explanation and component breakdown, see [docs/architecture.md](docs/architecture.md).

---

## Technology Stack

*   **Python:** 3.11+ (fully type-hinted with MyPy strict)
*   **Web Framework:** FastAPI
*   **Async HTTP:** HTTPX
*   **Kubernetes Client:** kubernetes-asyncio
*   **Anomaly Detection:** River ML (HalfSpaceTrees, MinMaxScaler)
*   **AI Planning:** Pydantic-AI, Google Gemini
*   **Database:** MongoDB Atlas (via Motor - async)
*   **CLI:** Typer
*   **Logging:** Loguru
*   **Metrics:** prometheus-client
*   **Linting/Formatting:** Ruff, Black

*(See `requirements.txt` for exact pinned versions)*

---

## Prerequisites

1.  **Python:** Version 3.11 or higher.
2.  **Kubernetes Cluster:** Access to a Kubernetes cluster where KubeWise will be deployed or can connect to.
3.  **MongoDB Atlas:** A running MongoDB Atlas cluster accessible from where KubeWise runs. Get the connection string (SRV URI).
4.  **Prometheus:** A running Prometheus instance (inside or outside the cluster) monitoring the target Kubernetes cluster, accessible from where KubeWise runs. Get the HTTP URL.
5.  **Google Gemini API Key:** An API key for Google AI Studio / Vertex AI with access to a Gemini model (e.g., `gemini-2.0-flash`).

---

## Getting Started

### Configuration

1.  **Copy the Example:**
    ```bash
    cp .env.example .env
    ```
2.  **Edit `.env`:**
    *   Replace `<username>`, `<password>`, `<atlas_cluster_url>`, and `<database_name>` in `MONGO_URI` with your actual MongoDB Atlas credentials and details.
    *   Update `PROM_URL` to point to your accessible Prometheus instance URL.
    *   Set `GEMINI_API_KEY` to your valid Google Gemini API key.
    *   (Optional) Adjust `LOG_LEVEL` (default: INFO).
    *   (Optional) Override default `ANOMALY_THRESHOLDS` (JSON string, e.g., `{"cpu": 0.9, "memory": 0.9, "default": 0.85}`) to tune detection sensitivity per metric type. Higher values mean less sensitivity.
    *   (Optional) Override `REMEDIATION_COOLDOWN_SECONDS` (default: 600) to change the minimum time between remediation attempts on the same resource.
    *   (Optional) Adjust HST model parameters like `HST_N_TREES`, `HST_HEIGHT`, `HST_WINDOW_SIZE` for finer control over the detection algorithm (requires understanding of River ML HST).
    *   (Optional) Configure MongoDB connection pooling via `MONGO_MAX_POOL_SIZE` (default: 100) and `MONGO_MIN_POOL_SIZE` (default: 10).
    *   (Optional) Configure circuit breaker parameters via `CIRCUIT_OPEN_TIMEOUT` (default: 60s), `ERROR_THRESHOLD` (default: 5), and `HALF_OPEN_MAX_CALLS` (default: 3).

### Development Environment with Docker Compose

KubeWise provides a Docker Compose configuration for easy local development with all required dependencies.

1. **Start the Development Environment:**
   ```bash
   docker-compose up -d
   ```

2. **Check Services:**
   ```bash
   docker-compose ps
   ```

3. **View Logs:**
   ```bash
   docker-compose logs -f kubewise
   ```

4. **Access Services:**
   * **KubeWise API:** http://localhost:8000/docs
   * **MongoDB Express:** http://localhost:8081
   * **Prometheus:** http://localhost:9090
   * **Grafana:** http://localhost:3000 (default credentials: admin/admin)

5. **Stop Services:**
   ```bash
   docker-compose down
   ```

### Running Locally

These instructions are for running the KubeWise server outside of Kubernetes, typically for development or testing, assuming it can connect to your external MongoDB, Prometheus, and Kubernetes cluster (via kubeconfig).

1.  **Set up Virtual Environment:**
    ```bash
    python -m venv .venv
    source .venv/bin/activate # On Windows use `.venv\Scripts\activate`
    ```

2.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Ensure `.env` is Populated:** Double-check your `.env` file has the correct values from the Configuration step.

4.  **Run the Application Server:**
    ```bash
    uvicorn kubewise.api.server:create_app --reload --host 0.0.0.0 --port 8000 --factory
    ```
    *   `--reload`: Enables hot-reloading for development (restart on code changes).
    *   `--host 0.0.0.0`: Makes the server accessible on your local network.
    *   `--port 8000`: The port the server listens on.
    *   `--factory`: Tells Uvicorn to use the `create_app` function.

5.  **Access the API:**
    *   **Health Check:** `http://localhost:8000/health`
    *   **API Docs (Swagger):** `http://localhost:8000/docs`
    *   **API Docs (ReDoc):** `http://localhost:8000/redoc`

The server will start, connect to MongoDB and Kubernetes (if configured correctly), and begin the background tasks for polling Prometheus, watching K8s events, detecting anomalies, and potentially triggering remediation. Check the logs for status information.

---

## API Endpoints

The following endpoints are available (see `/docs` for interactive documentation):

*   `GET /health`: Basic health check. Returns `{"status": "ok"}`.
*   `GET /livez`: Kubernetes liveness probe. Returns `{"status": "live"}`.
*   `GET /metrics`: Exposes internal application metrics in Prometheus format.
*   `GET /anomalies`: Lists recently detected anomalies (supports `skip` and `limit` query parameters).
*   `GET /config`: Shows the current configuration (secrets masked).
*   `PUT /config`: Updates specific configuration values dynamically (e.g., `anomaly_thresholds`). Requires appropriate request body (see API docs).

---

## CLI Usage

KubeWise provides a command-line interface for common tasks.

*   **Show Help:**
    ```bash
    python -m kubewise.cli --help
    ```
*   **View Configuration:**
    ```bash
    python -m kubewise.cli config
    ```
*   **Check Connections (Prometheus, K8s):**
    ```bash
    python -m kubewise.cli check-connections
    ```
*   **List Recent Anomalies:**
    ```bash
    python -m kubewise.cli list-anomalies --limit 50
    ```

---

## Kubernetes Deployment

Manifests are provided in the `/kubernetes` directory for deploying KubeWise to a cluster.

1.  **Namespace:** Choose a namespace (e.g., `kubewise`) or use `default`. Ensure it exists (`kubectl create namespace kubewise`).
2.  **Secrets:** Create a Kubernetes Secret in your chosen namespace containing your MongoDB URI and Gemini API Key.
    ```bash
    kubectl create secret generic kubewise-secrets \
      --from-literal=MONGO_URI='mongodb+srv://<user>:<password>@<atlas_cluster_url>/<db_name>?retryWrites=true&w=majority' \
      --from-literal=GEMINI_API_KEY='YOUR_API_KEY' \
      -n kubewise # Use your chosen namespace
    ```
    *Replace placeholders with your actual credentials.*
3.  **Configuration:** Review `kubernetes/deployment.yaml`.
    *   Ensure the `PROM_URL` environment variable points to your Prometheus service within the cluster (e.g., `http://prometheus-service.monitoring:9090`).
    *   Adjust resource requests/limits if needed.
    *   Verify the secret name (`kubewise-secrets`) matches the one you created.
    *   (Optional) If you want persistent logs via a file, uncomment the volume mount and volume sections related to `kubewise-log-storage` and ensure a corresponding PersistentVolumeClaim (PVC) exists *before* deploying. You might need to create a PVC manifest like `kubernetes/pvc-example.yaml` and apply it (`kubectl apply -f kubernetes/pvc-example.yaml -n kubewise`).
4.  **Apply Manifests:** Apply the RBAC rules, Deployment, and Service to your chosen namespace.
    ```bash
    # Apply RBAC (ClusterRole and ClusterRoleBinding are not namespaced)
    kubectl apply -f kubernetes/rbac.yaml

    # Apply Deployment and Service (adjust namespace if needed)
    kubectl apply -f kubernetes/deployment.yaml -n kubewise
    kubectl apply -f kubernetes/service.yaml -n kubewise
    ```
    *Modify the `-n <namespace>` flag if you used a different namespace.*

3.  **Verify Deployment:**
    ```bash
    kubectl get pods -l app=kubewise
    kubectl logs deployment/kubewise -f
    ```
    Check the logs to ensure the application starts correctly, connects to services, and background tasks are running.

---

## Development

### Testing

Basic unit and integration tests can be run using `pytest`. Ensure you have development dependencies installed (`pip install -r requirements-dev.txt` - *assuming such a file exists or dependencies are added to `requirements.txt`*).

```bash
# Activate virtual environment
source .venv/bin/activate

# Run tests
pytest -q tests/
```
*(Note: Test coverage is currently limited. Contributions are welcome!)*

### Linting & Formatting

This project uses Ruff for linting/formatting and MyPy for type checking. Configuration is in `pyproject.toml`.

*   **Check Formatting:**
    ```bash
    ruff format . --check
    ```
*   **Apply Formatting:**
    ```bash
    ruff format .
    ```
*   **Check Linting:**
    ```bash
    ruff check .
    ```
*   **Apply Linting Fixes (where possible):**
    ```bash
    ruff check . --fix
    ```
*   **Run Type Checking:**
    ```bash
    mypy src/
    ```

---

## Contributing

Contributions are welcome! Please follow these general steps:

1.  **Fork the repository.**
2.  **Create a new branch** for your feature or bug fix (`git checkout -b feature/my-new-feature` or `bugfix/issue-fix`).
3.  **Make your changes.** Ensure code is formatted (`ruff format .`), linted (`ruff check . --fix`), and type-checked (`mypy src/`).
4.  **Add tests** for your changes if applicable.
5.  **Commit your changes** with clear messages.
6.  **Push your branch** to your fork (`git push origin feature/my-new-feature`).
7.  **Open a Pull Request** against the main repository's `main` branch.

Please ensure your PR description clearly explains the changes and the problem they solve.

---

## License

This project is licensed under the **MIT License**.

```text
MIT License

Copyright (c) 2024 [Your Name or Organization - Update This]

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
*(Remember to update the copyright year and owner)*

---

## Observability

KubeWise includes comprehensive observability features to monitor its performance, health, and behavior in production environments.

### Health Checks

* **Basic Health Check:** `GET /health` - Returns service status and uptime
* **Liveness Probe:** `GET /livez` - Kubernetes-compatible liveness probe
* **Enhanced Health Check:** `GET /health/extended` - Detailed service health including component status and last successful operations

### Metrics

KubeWise exposes Prometheus metrics at the `/metrics` endpoint, including:

* **Application Metrics:** 
  * `kubewise_anomalies_detected_total` - Anomaly count by source, entity type, and namespace
  * `kubewise_false_positives_total` - False positive count by entity type and namespace
  * `kubewise_remediations_total` - Remediation count by action type, entity type, namespace, and status
  * `kubewise_remediation_duration_seconds` - Time to execute remediation actions

* **API Metrics:** 
  * `kubewise_api_request_duration_seconds` - API request durations by endpoint, method, and status code

* **External Service Metrics:**
  * `kubewise_external_request_duration_seconds` - External service request durations
  * `kubewise_external_request_errors_total` - External service request failures
  * `kubewise_circuit_breaker_status` - Circuit breaker status for external services (0=CLOSED, 1=HALF_OPEN, 2=OPEN)
  * `kubewise_external_service_up` - External service availability status (0=down, 1=up, 2=degraded)

These metrics can be collected by Prometheus and visualized in Grafana using the provided dashboards.

### Circuit Breaker Pattern

KubeWise implements the circuit breaker pattern for external service calls to prevent cascading failures:

* **Closed State:** Normal operation, requests flow through
* **Open State:** After `ERROR_THRESHOLD` consecutive failures, the circuit "trips" and immediately rejects requests for `CIRCUIT_OPEN_TIMEOUT` seconds
* **Half-Open State:** After the timeout, allows up to `HALF_OPEN_MAX_CALLS` test requests to verify service recovery

This pattern is applied to Prometheus queries, Kubernetes API calls, and MongoDB operations to ensure system resilience during partial outages.

### Graceful Shutdown

The application implements graceful shutdown to ensure proper cleanup during termination:

1. Stops accepting new API requests
2. Completes in-flight requests (with timeout)
3. Drains the queues in order: metrics/events, remediation, execution
4. Shuts down collectors first, then processors, then clients
5. Properly closes database connections and Kubernetes clients

This prevents data loss during container termination or deployment updates.

---

## Deployment

### Docker Deployment

1. **Build the image:**
   ```bash
   docker build -t kubewise:latest .
   ```

2. **Run the container:**
   ```bash
   docker run -p 8000:8000 --env-file .env kubewise:latest
   ```

### Kubernetes Deployment

Manifests are provided in the `/kubernetes` directory for deploying KubeWise to a cluster.

1.  **Namespace:** Choose a namespace (e.g., `kubewise`) or use `default`. Ensure it exists (`kubectl create namespace kubewise`).
2.  **Secrets:** Create a Kubernetes Secret in your chosen namespace containing your MongoDB URI and Gemini API Key.
    ```bash
    kubectl create secret generic kubewise-secrets \
      --from-literal=MONGO_URI='mongodb+srv://<user>:<password>@<atlas_cluster_url>/<db_name>?retryWrites=true&w=majority' \
      --from-literal=GEMINI_API_KEY='YOUR_API_KEY' \
      -n kubewise # Use your chosen namespace
    ```
    *Replace placeholders with your actual credentials.*
3.  **Configuration:** Review `kubernetes/deployment.yaml`.
    *   Ensure the `PROM_URL` environment variable points to your Prometheus service within the cluster (e.g., `http://prometheus-service.monitoring:9090`).
    *   Adjust resource requests/limits if needed.
    *   Verify the secret name (`kubewise-secrets`) matches the one you created.
    *   (Optional) If you want persistent logs via a file, uncomment the volume mount and volume sections related to `kubewise-log-storage` and ensure a corresponding PersistentVolumeClaim (PVC) exists *before* deploying. You might need to create a PVC manifest like `kubernetes/pvc-example.yaml` and apply it (`kubectl apply -f kubernetes/pvc-example.yaml -n kubewise`).
4.  **Apply Manifests:** Apply the RBAC rules, Deployment, and Service to your chosen namespace.
    ```bash
    # Apply RBAC (ClusterRole and ClusterRoleBinding are not namespaced)
    kubectl apply -f kubernetes/rbac.yaml

    # Apply Deployment and Service (adjust namespace if needed)
    kubectl apply -f kubernetes/deployment.yaml -n kubewise
    kubectl apply -f kubernetes/service.yaml -n kubewise
    ```
    *Modify the `-n <namespace>` flag if you used a different namespace.*

3.  **Verify Deployment:**
    ```bash
    kubectl get pods -l app=kubewise
    kubectl logs deployment/kubewise -f
    ```
    Check the logs to ensure the application starts correctly, connects to services, and background tasks are running.
