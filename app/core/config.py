from typing import List, Optional, Literal
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- API Metadata ---
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "KubeWise: Kubernetes Autonomous Anomaly Detector"
    VERSION: str = "3.1.0"
    DESCRIPTION: str = "Autonomous Kubernetes Monitoring and Remediation System"

    # --- Core Mode ---
    KUBEWISE_MODE: Literal["learning", "assisted", "autonomous"] = "autonomous"

    # --- Logging ---
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Anomaly Detection ---
    ANOMALY_MODEL_PATH: str = "models/Anomaly Scaler.joblib"
    ANOMALY_SCORE_THRESHOLD: float = 1.00 
    ANOMALY_DETECTION_THRESHOLD: float = 0.8
    ANOMALY_CRITICAL_THRESHOLD: float = 40.0  # Increased based on observed scores (was 4.02)
    ANOMALY_METRIC_WINDOW_SECONDS: int = 300
    AUTOENCODER_MODEL_PATH: str = "models/Anomaly Autoencoder.keras"
    AUTOENCODER_SCALER_PATH: str = "models/Anomaly Scaler.joblib"
    AUTOENCODER_THRESHOLD: float = 1.00  

    FEATURE_METRICS: List[str] = [
        "cpu_usage_seconds_total",
        "memory_working_set_bytes",
        "network_receive_bytes_total",
        "network_transmit_bytes_total",
        "pod_status_phase",
        "container_restarts_rate",  # Changed from container_restarts_rate to rate
    ]

    # --- Prometheus ---
    PROMETHEUS_URL: str = "http://localhost:9090"
    PROMETHEUS_PORT: Optional[int] = None
    PROMETHEUS_SCRAPE_INTERVAL_SECONDS: int = 15
    PROMETHEUS_TIMEOUT: int = 30

    # --- Gemini ---
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_AUTO_QUERY_GENERATION: bool = False
    GEMINI_AUTO_VERIFICATION: bool = True
    GEMINI_AUTO_ANALYSIS: bool = True
    GEMINI_ANALYZE_CRITICAL_FIRST: bool = True
    GEMINI_MODEL: str = "gemini-2.0-flash"
    GEMINI_MAX_TOKENS: int = 1000
    GEMINI_TEMPERATURE: float = 0.2
    GEMINI_TOP_P: float = 0.9
    GEMINI_TOP_K: int = 40

    # --- Worker ---
    WORKER_SLEEP_INTERVAL_SECONDS: int = 15

    # --- Remediation ---
    DEFAULT_REMEDIATION_MODE: Literal["MANUAL", "AUTO"] = "AUTO"
    AUTO_REMEDIATION_ENABLED: bool = False
    VERIFICATION_DELAY_SECONDS: int = 300
    ADMIN_BYPASS: bool = False
    DRY_RUN: bool = False

    CRITICAL_FAILURE_THRESHOLD: float = 0.8
    AUTO_REMEDIATE_CRITICAL: bool = True
    AUTO_REMEDIATE_PREDICTED: bool = False
    CRITICAL_VERIFICATION_DELAY_SECONDS: int = 30
    PREDICTION_VERIFICATION_DELAY_SECONDS: int = 300
    PREDICTION_HORIZON_MINUTES: int = 60

    SAFE_CRITICAL_COMMANDS: List[str] = [
        "restart_pod",
        "restart_deployment",
        "scale_deployment",
        "cordon_node",
        "uncordon_node"
    ]
    SAFE_PROACTIVE_COMMANDS: List[str] = [
        "restart_pod",
        "scale_deployment",
        "adjust_resources"
    ]

    # --- Health ---
    HEALTH_CHECK_SERVICES: List[str] = ["gemini", "prometheus", "kubernetes"]

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


# Global settings instance
settings = Settings()
