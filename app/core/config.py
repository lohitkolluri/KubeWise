from pydantic_settings import BaseSettings
from typing import Optional, List


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables with defaults.

    Settings are organized by category:
    - API: API configuration and versioning
    - Logging: Log levels and configuration
    - Anomaly Detection: ML model settings and thresholds
    - Monitoring: Prometheus integration settings
    - Gemini: Google Gemini API configuration
    - Worker: Background processing configuration
    - Remediation: Automated remediation settings

    Environment variables are loaded from .env file and override these defaults when set.
    """

    # ========== API Configuration ==========
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "KubeWise: Kubernetes Autonomous Anomaly Detector"
    VERSION: str = "3.1.0"
    DESCRIPTION: str = "Autonomous Kubernetes Monitoring and Remediation System"

    # ========== Logging Configuration ==========
    LOG_LEVEL: str = "INFO"  # Options: DEBUG, INFO, WARNING, ERROR, CRITICAL

    # ========== Anomaly Detection Configuration ==========
    # Path to saved model file (relative to project root)
    ANOMALY_MODEL_PATH: str = "models/anomaly_detector_scaler.joblib"
    # Threshold below which scores are considered anomalies (negative numbers, lower is more anomalous)
    ANOMALY_SCORE_THRESHOLD: float = -0.5
    # Time window in seconds to consider for metrics history
    ANOMALY_METRIC_WINDOW_SECONDS: int = 300

    # ========== Monitoring Configuration ==========
    # Prometheus server URL for metrics collection
    PROMETHEUS_URL: str = "http://localhost:9090"
    # Port to expose application metrics for Prometheus scraping
    PROMETHEUS_PORT: int = 9090
    # Interval in seconds between metric collection cycles
    PROMETHEUS_SCRAPE_INTERVAL_SECONDS: int = 15  # Changed from 60 to 15
    PROMETHEUS_TIMEOUT: int = 30  # Timeout in seconds for Prometheus queries

    # ========== Gemini API Configuration ==========
    # Google Gemini API key for AI functions
    GEMINI_API_KEY: Optional[str] = None
    # Whether to use Gemini to generate PromQL queries automatically
    GEMINI_AUTO_QUERY_GENERATION: bool = True
    # Whether to use Gemini to verify remediation success
    GEMINI_AUTO_VERIFICATION: bool = True
    # Whether to use Gemini for automatic analysis and remediation suggestions
    GEMINI_AUTO_ANALYSIS: bool = True
    # Priority for Gemini analysis of critical failures
    GEMINI_ANALYZE_CRITICAL_FIRST: bool = True
    GEMINI_MODEL: str = "gemini-1.5-pro"
    GEMINI_MAX_TOKENS: int = 1000
    GEMINI_TEMPERATURE: float = 0.7

    # ========== Worker Configuration ==========
    # Interval in seconds between worker processing cycles
    WORKER_SLEEP_INTERVAL_SECONDS: int = 15  # Changed from 60 to 15

    # ========== Remediation Configuration ==========
    # Default remediation mode (MANUAL or AUTO)
    DEFAULT_REMEDIATION_MODE: str = "AUTO"
    # Delay in seconds before verifying remediation success
    VERIFICATION_DELAY_SECONDS: int = 300

    # ========== Failure Detection and Remediation Settings ==========
    CRITICAL_FAILURE_THRESHOLD: float = 0.8  # Threshold for failure metrics (e.g., error rates, crash loops)
    AUTO_REMEDIATE_CRITICAL: bool = True  # Whether to auto-remediate critical failures even in MANUAL mode
    AUTO_REMEDIATE_PREDICTED: bool = False  # Whether to auto-remediate predicted future failures in MANUAL mode
    CRITICAL_VERIFICATION_DELAY_SECONDS: int = 30  # Shorter verification delay for critical failures
    PREDICTION_VERIFICATION_DELAY_SECONDS: int = 300  # Verification delay for predicted failure remediations
    PREDICTION_HORIZON_MINUTES: int = 60
    SAFE_CRITICAL_COMMANDS: List[str] = [
        "restart_deployment",
        "scale_deployment",
        "delete_pod",
        "cordon_node",
        "uncordon_node",
        "restart_pod",
        "restart_statefulset",
        "restart_daemonset",
        "restart_pods_in_namespace",
        "scale_statefulset",
        "scale_daemonset",
        "adjust_resources",
        "drain_node",
        "get_pod",
        "get_deployment",
        "list_pods",
        "get_logs",
        "get_node",
        "list_dependencies",
        "monitor_pod",
        "update_deployment",
        "update_statefulset",
        "update_daemonset",
        "patch_deployment",
        "patch_statefulset",
        "patch_daemonset"
    ]  # Commands that are safe to run for critical failures
    SAFE_PROACTIVE_COMMANDS: List[str] = [
        "scale_deployment",
        "scale_statefulset",
        "monitor_pod",
        "adjust_resources",
        "drain_node"
    ]  # Commands that are safe to run for predicted failures (less invasive)

    # ========== Default PromQL Queries ==========
    # Used as fallback if Gemini query generation fails or is disabled
    DEFAULT_PROMQL_QUERIES: List[str] = [
        # CPU usage by pod
        'sum(rate(container_cpu_usage_seconds_total{namespace!="", pod!=""}[2m])) by (namespace, pod, node)',
        # Memory usage by pod
        'sum(container_memory_working_set_bytes{namespace!="", pod!=""}) by (namespace, pod, node)',
        # Unavailable deployment replicas
        'kube_deployment_status_replicas_unavailable > 0',
        # Failed pods
        'kube_pod_status_phase{phase="Failed"} == 1',
        # Pod restarts
        'kube_pod_container_status_restarts_total > 3',
        # Node readiness
        'kubelet_node_status_condition{condition="Ready", status="true"} == 0',
        # Pod phase status
        'kube_pod_status_phase{phase=~"Pending|Failed|Unknown"}',
        # Container status
        'kube_pod_container_status_waiting_reason{reason!=""}',
        'kube_pod_container_status_terminated_reason{reason!=""}',
        # Node conditions
        'kube_node_status_condition{condition=~"MemoryPressure|DiskPressure|PIDPressure|NetworkUnavailable", status="true"}',
        # Resource limits and requests
        'kube_pod_container_resource_limits{resource="cpu"}',
        'kube_pod_container_resource_limits{resource="memory"}',
        'kube_pod_container_resource_requests{resource="cpu"}',
        'kube_pod_container_resource_requests{resource="memory"}',
        # Network errors
        'sum(rate(container_network_receive_errors_total[2m])) by (namespace, pod)',
        'sum(rate(container_network_transmit_errors_total[2m])) by (namespace, pod)',
        # Disk I/O errors
        'sum(rate(container_fs_io_time_seconds_total[2m])) by (namespace, pod)',
        'sum(rate(container_fs_io_current[2m])) by (namespace, pod)',
        # OOM kills
        'sum(rate(container_oom_events_total[2m])) by (namespace, pod)',
        # Liveness/Readiness probe failures
        'sum(rate(kube_pod_container_status_last_terminated_reason{reason="Error"}[2m])) by (namespace, pod)',
        'sum(rate(kube_pod_container_status_last_terminated_reason{reason="CrashLoopBackOff"}[2m])) by (namespace, pod)',
        'sum(rate(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}[2m])) by (namespace, pod)',
        'sum(rate(kube_pod_container_status_waiting_reason{reason="ImagePullBackOff"}[2m])) by (namespace, pod)',
        # Container status
        'sum(kube_pod_container_status_ready{ready="false"}) by (namespace, pod)',
        # Node conditions
        'sum(kube_node_status_condition{status="True",condition=~"DiskPressure|MemoryPressure|NetworkUnavailable"}) by (node)',
        # Pod scheduling failures
        'sum(kube_pod_status_unschedulable) by (namespace, pod)',
        # Volume mount failures
        'sum(kube_pod_container_status_waiting_reason{reason="ContainerCreating"}) by (namespace, pod)',
        # Network connectivity
        'sum(rate(container_network_receive_bytes_total[2m])) by (namespace, pod)',
        'sum(rate(container_network_transmit_bytes_total[2m])) by (namespace, pod)',
        # API server latency
        'histogram_quantile(0.95, sum(rate(apiserver_request_duration_seconds_bucket[5m])) by (le, resource, verb))',
        # etcd latency
        'histogram_quantile(0.95, sum(rate(etcd_request_duration_seconds_bucket[5m])) by (le, operation))'
    ]

    # ========== Health Check Settings ==========
    HEALTH_CHECK_SERVICES: List[str] = [
        "gemini",
        "prometheus",
        "kubernetes"
    ]

    class Config:
        """Pydantic config for Settings."""
        env_file = ".env"
        case_sensitive = True


# Global settings instance
settings = Settings()
