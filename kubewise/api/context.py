import asyncio
from dataclasses import dataclass, field
from typing import Optional, Set, Dict, Union

import httpx
import motor.motor_asyncio
from kubernetes_asyncio import client
from pydantic_ai import Agent # Changed PydanticAI back to Agent
from prometheus_client import Counter, Gauge, Histogram

# Forward references for type hints if needed, though direct imports might be okay here
# if the modules themselves don't import AppContext directly.
# Import the watcher class for type hinting
from kubewise.collector.k8s_events import KubernetesEventWatcher
from kubewise.models.detector import OnlineAnomalyDetector, SequentialAnomalyDetector
from kubewise.models import AnomalyRecord, KubernetesEvent, MetricPoint

# --- Constants ---
# Moved from server.py for better organization if needed elsewhere, or keep in server.py
# REMEDIATION_COOLDOWN_SECONDS = 300

# Application metrics
METRICS_NAMESPACE = "kubewise"

# Anomaly metrics
ANOMALY_COUNTER = Counter(
    f"{METRICS_NAMESPACE}_anomalies_detected_total",
    "Total number of anomalies detected",
    ["source", "entity_type", "namespace"]
)

FALSE_POSITIVE_COUNTER = Counter(
    f"{METRICS_NAMESPACE}_false_positives_total",
    "Count of detected anomalies classified as false positives",
    ["entity_type", "namespace"]
)

# Remediation metrics
REMEDIATION_COUNTER = Counter(
    f"{METRICS_NAMESPACE}_remediations_total",
    "Total number of remediation actions executed",
    ["action_type", "entity_type", "namespace", "status"]
)

REMEDIATION_DURATION = Histogram(
    f"{METRICS_NAMESPACE}_remediation_duration_seconds",
    "Time taken to execute remediation actions",
    ["action_type", "entity_type"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0]
)

# API metrics
API_REQUEST_DURATION = Histogram(
    f"{METRICS_NAMESPACE}_api_request_duration_seconds",
    "Duration of API requests",
    ["endpoint", "method", "status_code"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0]
)

# External service metrics
EXTERNAL_REQUEST_DURATION = Histogram(
    f"{METRICS_NAMESPACE}_external_request_duration_seconds",
    "Duration of requests to external services",
    ["service", "operation"],
    buckets=[0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0]
)

EXTERNAL_REQUEST_ERRORS = Counter(
    f"{METRICS_NAMESPACE}_external_request_errors_total",
    "Number of errors in requests to external services",
    ["service", "operation", "error_type"]
)

# Service health metrics
SERVICE_CIRCUIT_BREAKER_STATUS = Gauge(
    f"{METRICS_NAMESPACE}_circuit_breaker_status",
    "Circuit breaker status (0=CLOSED, 1=HALF_OPEN, 2=OPEN)",
    ["service", "circuit_key"]
)

SERVICE_UP = Gauge(
    f"{METRICS_NAMESPACE}_external_service_up",
    "Status of external services (0=down, 1=up, 2=degraded)",
    ["service"]
)

@dataclass
class AppContext:
    """Holds shared application resources like clients, queues, and detector."""
    mongo_client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
    db: Optional[motor.motor_asyncio.AsyncIOMotorDatabase] = None
    k8s_api_client: Optional[client.ApiClient] = None
    http_client: Optional[httpx.AsyncClient] = None
    anomaly_detector: Optional[Union[OnlineAnomalyDetector, SequentialAnomalyDetector]] = None
    gemini_agent: Optional[Agent] = None # Changed PydanticAI back to Agent
    metric_queue: asyncio.Queue[MetricPoint] = field(default_factory=lambda: asyncio.Queue(maxsize=1000))
    event_queue: asyncio.Queue[KubernetesEvent] = field(default_factory=lambda: asyncio.Queue(maxsize=500))
    remediation_queue: asyncio.Queue[AnomalyRecord] = field(default_factory=lambda: asyncio.Queue(maxsize=100))
    # Store tasks separately for ordered shutdown
    # Add the event watcher instance
    k8s_event_watcher: Optional[KubernetesEventWatcher] = None
    datasource_tasks: Set[asyncio.Task] = field(default_factory=set)
    processing_tasks: Set[asyncio.Task] = field(default_factory=set)
    aiohttp_session: Optional[object] = None
    
    # Application metrics tracking
    start_time: Optional[object] = None  # Will be datetime
    start_timestamp: float = field(default_factory=lambda: 0.0)
    
    # Statistics and counters - these aren't exposed as metrics but can be used internally
    detector_stats: Dict[str, int] = field(default_factory=lambda: {
        "processed_metrics": 0,
        "processed_events": 0,
        "anomalies_detected": 0,
        "false_positives": 0
    })
    
    remediation_stats: Dict[str, int] = field(default_factory=lambda: {
        "plans_generated": 0,
        "actions_executed": 0,
        "successful_actions": 0,
        "failed_actions": 0
    })
    
    def record_anomaly(self, source: str, entity_type: str, namespace: str = "unknown"):
        """Record an anomaly detection in Prometheus metrics."""
        ANOMALY_COUNTER.labels(source=source, entity_type=entity_type, namespace=namespace).inc()
        self.detector_stats["anomalies_detected"] += 1
    
    def record_false_positive(self, entity_type: str, namespace: str = "unknown"):
        """Record a false positive in Prometheus metrics."""
        FALSE_POSITIVE_COUNTER.labels(entity_type=entity_type, namespace=namespace).inc()
        self.detector_stats["false_positives"] += 1
    
    def record_remediation(self, action_type: str, entity_type: str, namespace: str, success: bool, duration: float):
        """Record a remediation action in Prometheus metrics."""
        status = "success" if success else "failure"
        REMEDIATION_COUNTER.labels(
            action_type=action_type,
            entity_type=entity_type,
            namespace=namespace,
            status=status
        ).inc()
        
        REMEDIATION_DURATION.labels(
            action_type=action_type,
            entity_type=entity_type
        ).observe(duration)
        
        if success:
            self.remediation_stats["successful_actions"] += 1
        else:
            self.remediation_stats["failed_actions"] += 1
        self.remediation_stats["actions_executed"] += 1
