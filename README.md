# 🧠 KubeWise

<div align="center">

![Version](https://img.shields.io/badge/version-3.0.0-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Python](https://img.shields.io/badge/python-3.9+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.95+-green.svg)
![Kubernetes](https://img.shields.io/badge/kubernetes-1.22+-blue.svg)
![Gemini](https://img.shields.io/badge/Gemini%20API-integrated-purple.svg)

**AI-Powered Kubernetes Anomaly Detection and Autonomous Remediation System**

</div>

<p align="center">
  <a href="#-project-overview">Overview</a> •
  <a href="#-key-features">Features</a> •
  <a href="#-architecture--workflow">Architecture</a> •
  <a href="#-installation--setup">Installation</a> •
  <a href="#-usage--operation">Usage</a> •
  <a href="#-api-reference">API</a> •
  <a href="#-technology-stack">Tech Stack</a> •
  <a href="#-development--contribution">Development</a> •
  <a href="#-troubleshooting">Troubleshooting</a> •
  <a href="#-license--contact">License</a>
</p>

---

## 📋 Project Overview

The K8s Autonomous Anomaly Detector is a comprehensive system designed to monitor Kubernetes clusters in real-time, detect anomalies using machine learning, and autonomously remediate issues with the help of AI-powered analysis.

This project bridges the gap between traditional rule-based monitoring and modern AI-driven operations by combining:

- **Machine Learning** for anomaly detection using Isolation Forest algorithm
- **Google Gemini AI** for insightful root cause analysis and intelligent remediation suggestion
- **Autonomous remediation** capabilities with safety-validated Kubernetes operations
- **Continuous verification** to ensure issues are properly resolved

### Target Environment

- Kubernetes clusters (v1.22+)
- Prometheus monitoring setup
- MongoDB for data persistence

### Core Technologies

- FastAPI for the API layer
- Scikit-learn for ML-based anomaly detection
- Google Gemini API for AI analysis and remediation
- Official Kubernetes Python client for cluster interactions
- Prometheus for metrics collection
- MongoDB for event storage and persistence

---

## 🌟 Key Features

### Monitoring & Detection

- **Real-time Metric Collection**: Continuously monitors Kubernetes clusters via Prometheus metrics
- **Dynamic PromQL Query Generation**: AI-driven discovery and optimization of PromQL queries based on available metrics
- **ML-Powered Anomaly Detection**: Uses Isolation Forest algorithm to identify anomalous patterns in metrics
- **Feature Engineering Pipeline**: Transforms raw metrics into ML-compatible feature vectors

### AI Analysis & Remediation

- **Root Cause Analysis**: Google Gemini AI analyzes anomaly context to provide insights on probable causes
- **Intelligent Remediation**: AI suggests appropriate remediation commands based on historical success patterns
- **Autonomous Mode**: Optionally executes safe, validated remediation commands without human intervention
- **Manual Mode**: Allows human review of AI-suggested remediation before execution

### Safety & Verification

- **Command Validation**: All remediation actions are validated for safety before execution
- **Phased Operations**: Critical operations like scaling can be performed incrementally
- **Post-Remediation Verification**: AI-assisted verification confirms successful resolution
- **Dependency Checking**: Validates impact on related resources before operations

### Extensible API & Interfaces

- **RESTful API**: Comprehensive API for system management and monitoring
- **Swagger Documentation**: Interactive API documentation with OpenAPI/Swagger
- **System Health Monitoring**: Endpoints for system status and health checks

### Anomaly Detection Capabilities

The system can identify various types of Kubernetes anomalies:

- **Resource Exhaustion**: CPU/memory spikes, disk space issues, resource contention
- **Application Health Issues**: Container crashes, failed deployments, restart loops
- **Control Plane Problems**: API server latency, etcd health issues, scheduler problems
- **Node Health Issues**: Node pressure, network connectivity, hardware issues
- **Workload Instability**: Deployment replica mismatches, HPA scaling issues, pod evictions

### Remediation Capabilities

The system can execute multiple remediation strategies:

- **Workload Operations**: Restart deployments/pods, scale resources up or down
- **Resource Management**: Adjust CPU/memory requests and limits
- **Node Management**: Cordon/uncordon nodes, drain workloads safely
- **Custom Commands**: Execute user-defined safe operations

---

## 🏗 Architecture & Workflow

### System Architecture

The system follows a modular architecture with clear separation of concerns:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           K8s Autonomous Anomaly Detector                       │
│                                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐       │
│  │   FastAPI   │    │   Anomaly   │    │   Gemini    │    │     K8s     │       │
│  │   Server    │◄───┤  Detector   │◄───┤   Service   │◄───┤   Executor  │       │
│  │             │    │             │    │             │    │             │       │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘    └──────┬──────┘       │
│         │                  │                  │                   │              │
│         ▼                  ▼                  ▼                   ▼              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐       │
│  │ API Router: │    │ Autonomous  │    │   MongoDB   │    │  Prometheus │       │
│  │ - Health    │    │   Worker    │    │  Database   │    │   Client    │       │
│  │ - Metrics   │    │   Process   │    │             │    │             │       │
│  │ - Anomaly   │    │             │    │             │    │             │       │
│  │ - Remediate │    │             │    │             │    │             │       │
│  └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘       │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
           │                  │                  │                   │
           ▼                  ▼                  ▼                   ▼
  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
  │    HTTP     │    │  Kubernetes │    │   MongoDB   │    │  Prometheus │
  │   Clients   │    │   Cluster   │    │    Atlas    │    │    Server   │
  └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
```

### Component Descriptions

#### Core Services

1. **FastAPI Server (`app/main.py`)**
   - Web framework providing the API layer
   - Handles HTTP requests and responses
   - Manages dependency injection and middleware

2. **Anomaly Detector (`app/models/anomaly_detector.py`)**
   - ML-based anomaly detection using Isolation Forest
   - Feature engineering from raw metrics
   - Model training, persistence, and evaluation

3. **Gemini Service (`app/services/gemini_service.py`)**
   - Integration with Google's Gemini API
   - Structured output with function calling
   - Root cause analysis and remediation suggestion

4. **K8s Executor (`app/utils/k8s_executor.py`)**
   - Safe execution of Kubernetes operations
   - Command validation and parsing
   - Resource dependency checking

#### Supporting Components

5. **Autonomous Worker (`app/worker.py`)**
   - Background process for continuous monitoring
   - Anomaly detection pipeline orchestration
   - Remediation decision and execution

6. **Event Service (`app/services/anomaly_event_service.py`)**
   - Manages anomaly events lifecycle
   - Stores and retrieves events from MongoDB
   - Tracks remediation attempts

7. **Prometheus Scraper (`app/utils/prometheus_scraper.py`)**
   - Fetches metrics from Prometheus
   - Query validation and execution
   - Metric transformation

### Data Flow & Workflow

#### Monitoring & Detection Workflow

1. **Metric Collection**:
   ```
   AutonomousWorker
     ↓
   PrometheusClient.fetch_all_metrics()
     ↓
   Raw metrics from Prometheus
   ```

2. **Feature Engineering & Anomaly Detection**:
   ```
   Raw metrics
     ↓
   AnomalyDetector._engineer_features_aggregate()
     ↓
   Feature vectors
     ↓
   AnomalyDetector.predict()
     ↓
   Anomaly prediction (boolean) + anomaly score
   ```

3. **Event Creation**:
   ```
   Anomaly detected
     ↓
   AnomalyEventService.create_event()
     ↓
   MongoDB: anomaly_events collection
   ```

#### Remediation Workflow

4. **AI Analysis**:
   ```
   Anomaly event
     ↓
   GeminiService.batch_process_anomaly()
     ↓
   AI analysis & suggested remediation
   ```

5. **Remediation Execution** (AUTO mode):
   ```
   Validated remediation commands
     ↓
   K8sExecutor.execute_validated_command()
     ↓
   Kubernetes API: Apply changes
     ↓
   AnomalyEventService.add_remediation_attempt()
   ```

6. **Verification**:
   ```
   After remediation timeout
     ↓
   AutonomousWorker._verify_remediation()
     ↓
   GeminiService.verify_remediation_success()
     ↓
   Update event status: RESOLVED or REMEDIATION_FAILED
   ```

### Code Example: Anomaly Detection Process

```python
# From app/worker.py
async def _predict_and_handle_anomaly(self, entity_key: str, entity_data: Dict[str, Any], metric_window: List[List[Dict[str, Any]]]):
    """Predicts anomaly and handles if detected."""
    # Get latest metrics for prediction
    latest_metrics_snapshot = metric_window[-1] if metric_window else []

    # Predict anomaly using ML model
    is_anomaly, score = await anomaly_detector.predict(latest_metrics_snapshot)

    if is_anomaly:
        # Create anomaly context for AI analysis
        anomaly_context = {
            "entity_id": entity_data["entity_id"],
            "entity_type": entity_data["entity_type"],
            "namespace": entity_data["namespace"],
            "metrics_snapshot": latest_metrics_snapshot,
            "anomaly_score": score,
            "timestamp": datetime.utcnow().isoformat()
        }

        # Process with Google Gemini AI
        batch_result = await self.gemini_service.batch_process_anomaly(anomaly_context)

        # Create event and handle remediation
        new_event = await self.anomaly_event_service.create_event(event_data)
        if new_event:
            await self._decide_remediation(new_event, remediation_suggestions_raw)
```

---

## 📦 Installation & Setup

### Prerequisites

- Python 3.9+
- Kubernetes cluster (for production deployments)
- Access to Google Gemini API (API key)
- MongoDB instance or MongoDB Atlas account
- Prometheus monitoring setup for your Kubernetes cluster

### Local Development Setup

#### 1. Clone the Repository

```bash
git clone https://github.com/your-org/k8s-anomaly-detector.git
cd k8s-anomaly-detector
```

#### 2. Create a Python Virtual Environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

#### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

#### 4. Set Up Environment Variables

Create a `.env` file in the project root:

```dotenv
# Database Configuration
MONGODB_ATLAS_URI=mongodb://localhost:27017
MONGODB_DB_NAME=k8s_monitoring

# External Services
GEMINI_API_KEY=your-gemini-api-key
PROMETHEUS_URL=http://localhost:9090

# Application Settings
LOG_LEVEL=INFO
WORKER_SLEEP_INTERVAL_SECONDS=30
PROMETHEUS_SCRAPE_INTERVAL_SECONDS=15
ANOMALY_SCORE_THRESHOLD=-0.2
ANOMALY_METRIC_WINDOW_SECONDS=600

# Feature Flags
GEMINI_AUTO_VERIFICATION=true
GEMINI_AUTO_ANALYSIS=true
REMEDIATION_MODE=MANUAL  # or AUTO
```

#### 5. Start Local Infrastructure with Docker Compose

Create a `docker-compose.yml` file:

```yaml
version: '3'
services:
  mongodb:
    image: mongo:latest
    ports:
      - "27017:27017"
    volumes:
      - mongo_data:/data/db

  prometheus:
    image: prom/prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml

volumes:
  mongo_data:
```

Start the infrastructure services:

```bash
docker-compose up -d
```

#### 6. Run the Application

```bash
# Development mode with auto-reload
uvicorn app.main:app --reload

# Production mode
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Production Deployment

#### Kubernetes Deployment with Helm

1. **Configure values.yaml**

Create a `values.yaml` file:

```yaml
replicaCount: 1

image:
  repository: your-registry/k8s-anomaly-detector
  tag: 3.0.0
  pullPolicy: Always

mongodb:
  uri: mongodb+srv://your-username:your-password@your-cluster.mongodb.net
  dbName: k8s_monitoring

gemini:
  apiKey: your-gemini-api-key

prometheus:
  url: http://prometheus-server.monitoring.svc

remediation:
  mode: "MANUAL"  # or "AUTO"

resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 200m
    memory: 256Mi
```

2. **Deploy using Helm**

```bash
helm install k8s-anomaly-detector ./helm/k8s-anomaly-detector -f values.yaml \
  --namespace monitoring \
  --create-namespace
```

#### Building the Docker Image

```bash
docker build -t your-registry/k8s-anomaly-detector:3.0.0 .
docker push your-registry/k8s-anomaly-detector:3.0.0
```

### Configuration Options

#### Core Configuration Options

| Parameter | Description | Default | Required |
|-----------|-------------|---------|----------|
| `MONGODB_ATLAS_URI` | MongoDB connection string | - | Yes |
| `MONGODB_DB_NAME` | MongoDB database name | `k8s_monitoring` | No |
| `GEMINI_API_KEY` | Google Gemini API key | - | Yes |
| `PROMETHEUS_URL` | Prometheus server URL | `http://localhost:9090` | No |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) | `INFO` | No |
| `WORKER_SLEEP_INTERVAL_SECONDS` | Time between worker processing cycles | `30` | No |
| `PROMETHEUS_SCRAPE_INTERVAL_SECONDS` | Metrics scraping interval | `15` | No |
| `ANOMALY_SCORE_THRESHOLD` | Threshold for anomaly detection | `-0.2` | No |
| `ANOMALY_METRIC_WINDOW_SECONDS` | Time window for metrics history | `600` | No |
| `ANOMALY_MODEL_PATH` | Path to pre-trained model file | `models/anomaly_detector_scaler.joblib` | No |
| `REMEDIATION_MODE` | Operation mode (AUTO or MANUAL) | `MANUAL` | No |

#### Feature Flag Options

| Parameter | Description | Default |
|-----------|-------------|---------|
| `GEMINI_AUTO_ANALYSIS` | Enable automatic anomaly analysis with Gemini | `true` |
| `GEMINI_AUTO_VERIFICATION` | Enable automatic verification of remediation | `true` |
| `DEFAULT_PROMQL_QUERIES` | List of default PromQL queries if none detected | *[see documentation]* |

---

## 🚀 Usage & Operation

### Starting the System

#### Local Development

```bash
# Start with default configuration
uvicorn app.main:app --reload

# Start with specific log level
LOG_LEVEL=DEBUG uvicorn app.main:app --reload

# Start with specific port
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

#### Production Mode

```bash
# Start with production settings
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Web Interface & API Documentation

The system provides interactive API documentation:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Operation Modes

#### MANUAL Mode

In MANUAL mode, the system detects anomalies and suggests remediation, but requires explicit API calls to execute commands.

```bash
# Set MANUAL mode
curl -X POST "http://localhost:8000/api/v1/setup/mode" \
     -H "Content-Type: application/json" \
     -d '{"mode": "MANUAL"}'
```

When an anomaly is detected:
1. The system creates an anomaly event in MongoDB
2. Gemini AI analyzes the anomaly and suggests remediation
3. You can view events and suggested commands via the API
4. You must explicitly call the remediate endpoint to execute commands

#### AUTO Mode

In AUTO mode, the system automatically executes validated remediation commands.

```bash
# Set AUTO mode
curl -X POST "http://localhost:8000/api/v1/setup/mode" \
     -H "Content-Type: application/json" \
     -d '{"mode": "AUTO"}'
```

When an anomaly is detected:
1. The system creates an anomaly event in MongoDB
2. Gemini AI analyzes the anomaly and suggests remediation
3. The system validates and executes safe remediation commands
4. After a verification delay, the system checks if remediation was successful

### Monitoring System Activity

#### View Current Metrics

```bash
curl -X GET "http://localhost:8000/api/v1/metrics"
```

Example response:
```json
{
  "metrics": {
    "deployment/nginx-deployment": {
      "entity_id": "nginx-deployment",
      "entity_type": "deployment",
      "namespace": "default",
      "metrics": [
        {
          "query": "sum(rate(container_cpu_usage_seconds_total{container!=\"POD\",pod=~\"nginx-deployment-.*\"}[5m])) by (pod)",
          "values": [[1618245000, "0.21"], [1618245015, "0.65"], ...],
          "labels": {"pod": "nginx-deployment-7b9b4b6fb7-abcd1"}
        },
        ...
      ]
    },
    ...
  }
}
```

#### List Anomaly Events

```bash
curl -X GET "http://localhost:8000/api/v1/remediation/events"
```

Example response:
```json
{
  "events": [
    {
      "anomaly_id": "f82b4a7e-1c2d-4e5f-6g7h-8i9j0k1l2m3n",
      "status": "RemediationSuggested",
      "detection_timestamp": "2023-04-13T14:25:30.123456",
      "entity_id": "nginx-deployment",
      "entity_type": "deployment",
      "namespace": "default",
      "anomaly_score": -0.35,
      "suggested_remediation_commands": [
        "restart_deployment name=nginx-deployment ns=default",
        "scale_deployment name=nginx-deployment ns=default replicas=3"
      ],
      "ai_analysis": {
        "root_cause": "Memory pressure on deployment pods",
        "severity": "MEDIUM",
        "description": "The deployment is experiencing memory pressure..."
      }
    },
    ...
  ]
}
```

#### Manually Execute Remediation

```bash
curl -X POST "http://localhost:8000/api/v1/remediation/events/f82b4a7e-1c2d-4e5f-6g7h-8i9j0k1l2m3n/remediate" \
     -H "Content-Type: application/json" \
     -d '{"command": "restart_deployment name=nginx-deployment ns=default"}'
```

Example response:
```json
{
  "status": "success",
  "message": "Remediation command executed successfully",
  "event_id": "f82b4a7e-1c2d-4e5f-6g7h-8i9j0k1l2m3n",
  "command": "restart_deployment name=nginx-deployment ns=default",
  "result": {
    "status": "success",
    "timestamp": "2023-04-13T14:30:45.123456"
  }
}
```

#### Manual Anomaly Analysis

```bash
curl -X POST "http://localhost:8000/api/v1/anomalies/analyze/f82b4a7e-1c2d-4e5f-6g7h-8i9j0k1l2m3n"
```

#### Check System Health

```bash
curl -X GET "http://localhost:8000/api/v1/health"
```

Example response:
```json
{
  "status": "healthy",
  "version": "3.0.0",
  "services": {
    "API": {
      "status": "healthy",
      "message": "API is operational"
    },
    "MongoDB": {
      "status": "healthy",
      "message": "Connected to MongoDB Atlas"
    },
    "Prometheus": {
      "status": "healthy",
      "message": "Connected to Prometheus at http://localhost:9090"
    },
    "Kubernetes": {
      "status": "healthy",
      "message": "Connected to Kubernetes cluster with 3 nodes"
    },
    "Gemini API": {
      "status": "healthy",
      "message": "Gemini API client initialized successfully"
    },
    "Worker": {
      "status": "healthy",
      "message": "Worker process running normally"
    }
  },
  "timestamp": "2023-04-13T15:00:00.123456"
}
```

### Understanding System Logs

The application uses structured logging with Loguru. Log formats include:

```
2023-04-13 15:00:00.123 | INFO     | app.worker:run_cycle:307 - Starting worker cycle... Mode: MANUAL
2023-04-13 15:00:01.456 | WARNING  | app.worker:_predict_and_handle_anomaly:97 - Anomaly DETECTED for entity deployment/nginx-deployment with score -0.35
2023-04-13 15:00:02.789 | INFO     | app.services.gemini_service:batch_process_anomaly:747 - Received both analysis and remediation in a single Gemini API call
```

---

## 📘 API Reference

### Health & Status Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/health` | GET | Check system health and component status |
| `/api/v1/setup/mode` | GET | Get current remediation mode (AUTO/MANUAL) |
| `/api/v1/setup/mode` | POST | Set remediation mode |
| `/api/v1/setup/config` | GET | Get current system configuration |

### Metrics & Monitoring

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/metrics` | GET | Get current metrics from Prometheus |
| `/api/v1/metrics/queries` | GET | Get active PromQL queries |
| `/api/v1/metrics/model/info` | GET | Get anomaly model information |
| `/api/v1/metrics/model/train` | POST | Manually trigger model training |

### Anomaly Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/remediation/events` | GET | List anomaly events with optional filtering |
| `/api/v1/remediation/events/{anomaly_id}` | GET | Get detailed anomaly information |
| `/api/v1/anomalies/analyze/{anomaly_id}` | POST | Manually analyze an anomaly |

### Remediation Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/remediation/commands` | GET | List available remediation commands |
| `/api/v1/remediation/events/{anomaly_id}/remediate` | POST | Execute remediation command |

---

## 💻 Technology Stack

### Core Technologies

| Technology | Purpose | Benefits |
|------------|---------|----------|
| **Python 3.9+** | Primary programming language | Async support, strong ML ecosystem |
| **FastAPI** | API framework | High performance, async/await support, auto-docs |
| **Kubernetes Python Client** | K8s API interaction | Official client with comprehensive API coverage |
| **Scikit-learn** | Machine learning | Robust anomaly detection with Isolation Forest |
| **Google Gemini API** | AI for analysis and remediation | Advanced reasoning capabilities for complex analysis |
| **MongoDB** | Database for events & metrics | Flexible schema design for metric data |
| **Prometheus** | Metrics collection | Industry standard for K8s monitoring |

### Key Libraries

| Library | Purpose |
|---------|---------|
| **Pydantic** | Data validation and settings management |
| **Loguru** | Structured logging with improved developer experience |
| **Motor** | Async MongoDB driver |
| **Joblib** | Model serialization/deserialization |
| **Numpy** | Numerical operations for ML |
| **Prometheus Client** | Prometheus integration |
| **Httpx** | Async HTTP client |
| **python-dotenv** | Environment variable management |

### Design Patterns

- **Dependency Injection**: For services and components
- **Repository Pattern**: For data access abstraction
- **Service Layer**: Business logic separation
- **Event-Driven**: For anomaly detection and remediation flow
- **Worker Process**: Background task processing

---

## 🧩 Development & Contribution

### Project Structure

```
k8s-anomaly-detector/
├── app/                      # Application code
│   ├── api/                  # API routers
│   │   ├── anomaly_router.py # Anomaly-related endpoints
│   │   ├── health_router.py  # Health check endpoints
│   │   ├── metrics_router.py # Metrics-related endpoints
│   │   ├── remediation_router.py # Remediation endpoints
│   │   └── setup_router.py   # System configuration endpoints
│   ├── core/                 # Core functionality
│   │   ├── config.py         # Application configuration
│   │   ├── database.py       # Database connection
│   │   ├── logger.py         # Logger configuration
│   │   └── middleware.py     # FastAPI middleware
│   ├── models/               # Data models
│   │   ├── anomaly_detector.py # ML model for anomaly detection
│   │   ├── anomaly_event.py    # Anomaly event data model
│   │   └── common.py         # Common/shared models
│   ├── services/             # Business logic services
│   │   ├── anomaly_event_service.py # Anomaly event management
│   │   ├── gemini_service.py # Google Gemini AI integration
│   │   └── mode_service.py   # Remediation mode management
│   ├── utils/                # Utility functions
│   │   ├── cli_aesthetics.py # CLI output formatting
│   │   ├── health_checker.py # Component health checks
│   │   ├── k8s_config.py     # Kubernetes configuration
│   │   ├── k8s_executor.py   # Kubernetes operation executor
│   │   ├── prometheus_client.py # Prometheus API client
│   │   └── prometheus_scraper.py # Metric scraping logic
│   ├── main.py               # FastAPI application entry point
│   └── worker.py             # Autonomous worker process
├── models/                   # ML model files
│   └── anomaly_detector_scaler.joblib # Pre-trained model
├── prometheus/               # Prometheus configuration
│   └── prometheus.yml        # Prometheus config
├── .env                      # Environment variables (not in repo)
├── docker-compose.yml        # Local development environment
├── Dockerfile                # Container definition
├── requirements.txt          # Python dependencies
└── README.md                 # Project documentation
```

### Development Workflow

1. **Set Up Development Environment**
   ```bash
   # Clone repository
   git clone https://github.com/your-org/k8s-anomaly-detector.git

   # Create virtual environment
   python -m venv venv
   source venv/bin/activate

   # Install dependencies
   pip install -r requirements.txt
   ```

2. **Run Tests**
   ```bash
   # Run unit tests
   pytest

   # Run with coverage
   pytest --cov=app
   ```

3. **Code Style & Linting**
   ```bash
   # Check code style
   flake8 app

   # Format code
   black app

   # Check typing
   mypy app
   ```

### Adding New Features

#### 1. Adding a New Remediation Command

1. Update the `_define_safe_operations` method in `app/utils/k8s_executor.py`:
   ```python
   def _define_safe_operations(self):
       """Define safe operations that can be executed"""
       self.safe_operations = {
           # Existing operations...

           # New operation
           "new_command_name": {
               "api": "apps_v1", "method": "api_method_name",
               "required": ["param1", "param2"], "optional": ["param3"],
               "validation": lambda p: "param1" in p and "param2" in p,
               "patch_body": lambda p: {"spec": {"yourField": p["param1"]}}
           },
       }
   ```

2. Add the command handler if needed (for complex commands):
   ```python
   async def _handle_new_command(self, param1: str, param2: str, param3: Optional[str] = None) -> Dict[str, Any]:
       """Handle new command execution."""
       if not self.apps_v1:
           return {"error": "Kubernetes client not available"}

       try:
           # Implementation
           pass
       except Exception as e:
           logger.error(f"Error executing new command: {e}")
           return {"error": f"Error: {str(e)}"}
   ```

#### 2. Extending the Anomaly Detection Model

1. Update feature engineering in `app/models/anomaly_detector.py`:
   ```python
   def _engineer_features_aggregate(self, data_snapshots: List[List[Dict[str, Any]]]) -> np.ndarray:
       # Existing code...

       # Add new metric handling
       elif 'new_metric_name' in query:
           new_metric_stats[0] = np.mean(metric_values)
           new_metric_stats[1] = np.max(metric_values)

       # Include new feature in vector
       entity_feature_vector.extend(new_metric_stats)  # Add new features
   ```

2. Update model training if needed.

### Contribution Guidelines

1. **Fork and Branch**
   - Fork the repository
   - Create a feature branch: `git checkout -b feature/my-feature`

2. **Code Standards**
   - Follow PEP 8 style guidelines
   - Use type hints for function parameters and return values
   - Document code using docstrings (Google style)
   - Keep functions focused and single-purpose

3. **Testing**
   - Write unit tests for new functionality
   - Ensure all existing tests pass
   - Aim for at least 80% code coverage

4. **Pull Requests**
   - Submit a pull request with detailed description
   - Link to relevant issues
   - Ensure CI/CD pipeline passes
   - Request code review from maintainers

---

## 🛠 Troubleshooting

### Common Issues

#### Connection to Prometheus Failed

```
ERROR:     Failed to connect to Prometheus: ConnectError
```

**Solution**:
- Verify your Prometheus URL in the configuration
- Check network connectivity and firewall rules
- Ensure Prometheus is running with proper permissions

#### Gemini API Key Issues

```
WARNING:    Gemini service disabled: GEMINI_API_KEY not set
```

**Solution**:
- Verify your Gemini API key is correctly set in the environment
- Check for API key quotas or usage limitations
- Ensure your Gemini API key has the necessary permissions

#### Model Loading Failed

```
CRITICAL:   Failed to load model/scaler from models/anomaly_detector_scaler.joblib
```

**Solution**:
- Ensure the model file exists at the specified path
- Verify file permissions allow reading
- Check if the model format is compatible with your scikit-learn version
- If needed, train a new model

#### Worker Process Errors

```
ERROR:     Error during worker cycle: OperationTimeout
```

**Solution**:
- Check MongoDB connection parameters
- Verify Prometheus is responding within timeouts
- Increase worker interval if needed

### Debug Mode

Enable debug logging for more verbose output:

```
LOG_LEVEL=DEBUG uvicorn app.main:app --reload
```

### Logs

The application uses structured logging with Loguru. Check logs at:
- Console output when running in development mode
- Log files (if configured in the environment)
- Container logs when running in Kubernetes

### Diagnostic Endpoints

The system provides diagnostic endpoints for troubleshooting:

- `/api/v1/health`: Check system health and component status
- `/api/v1/metrics/queries`: View current PromQL queries

---

## 📄 License & Contact

### License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

### Contact Information

- **Project Maintainer**: [Your Name](mailto:your.email@example.com)
- **Issue Tracker**: [GitHub Issues](https://github.com/your-org/k8s-anomaly-detector/issues)
- **Documentation**: [Project Wiki](https://github.com/your-org/k8s-anomaly-detector/wiki)

---

<div align="center">
  <p>Made with ❤️ for Kubernetes observability and autonomous operations</p>
</div>
