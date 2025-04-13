from pydantic_settings import BaseSettings
from typing import Optional, List


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables with defaults.

    Settings are organized by category:
    - Database: MongoDB connection settings
    - API: API configuration and versioning
    - Logging: Log levels and configuration
    - Anomaly Detection: ML model settings and thresholds
    - Monitoring: Prometheus integration settings
    - Gemini: Google Gemini API configuration
    - Worker: Background processing configuration
    - Remediation: Automated remediation settings

    Environment variables override these defaults when set.
    """

    # ========== Database Configuration ==========
    MONGODB_ATLAS_URI: str  # Required, no default
    MONGODB_DB_NAME: str = "kubewise_db"

    # ========== API Configuration ==========
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "KubeWise: Kubernetes Autonomous Anomaly Detector"

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
    PROMETHEUS_SCRAPE_INTERVAL_SECONDS: int = 60

    # ========== Gemini API Configuration ==========
    # Google Gemini API key for AI functions
    GEMINI_API_KEY: Optional[str] = None
    # Whether to use Gemini to generate PromQL queries automatically
    GEMINI_AUTO_QUERY_GENERATION: bool = True
    # Whether to use Gemini to verify remediation success
    GEMINI_AUTO_VERIFICATION: bool = True

    # ========== Worker Configuration ==========
    # Interval in seconds between worker processing cycles
    WORKER_SLEEP_INTERVAL_SECONDS: int = 60

    # ========== Remediation Configuration ==========
    # Default remediation mode (MANUAL or AUTO)
    DEFAULT_REMEDIATION_MODE: str = "MANUAL"
    # Delay in seconds before verifying remediation success
    VERIFICATION_DELAY_SECONDS: int = 300

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
        'kubelet_node_status_condition{condition="Ready", status="true"} == 0'
    ]

    class Config:
        """Pydantic config for Settings."""
        env_file = ".env"
        case_sensitive = True


# Global settings instance
settings = Settings()
