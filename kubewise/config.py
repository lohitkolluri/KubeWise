# -*- coding: utf-8 -*-
import logging
from typing import Literal, Dict, List

from loguru import logger
from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Define valid log levels
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Default Prometheus Queries for resource monitoring and failure detection
DEFAULT_PROM_QUERIES = {
    # Resource Utilization Metrics (normalized to percentages)
    "pod_cpu_utilization_pct": '''
        sum by (container_label_io_kubernetes_pod_namespace, container_label_io_kubernetes_pod_name) (
            rate(container_cpu_usage_seconds_total{container_label_io_kubernetes_pod_name!=""}[5m])
        ) / 
        scalar(
            count(node_cpu_seconds_total{mode="idle"})
        ) * 100
    ''',
    "pod_memory_utilization_pct": '''
        sum by (container_label_io_kubernetes_pod_namespace, container_label_io_kubernetes_pod_name) (
            container_memory_working_set_bytes{container_label_io_kubernetes_pod_name!=""}
        ) / 
        scalar(
            sum(node_memory_MemTotal_bytes)
        ) * 100
    ''',

    # Direct Failure Indicators
    "pod_oom_events": '''
        sum by (container_label_io_kubernetes_pod_namespace, container_label_io_kubernetes_pod_name) (
            container_oom_events_total{container_label_io_kubernetes_pod_name!=""} > 0
        )
    ''',

    # Pod OOM State (current)
    "pod_oomkilled_state": '''
        sum by (namespace, pod) (
            kube_pod_container_status_terminated_reason{reason="OOMKilled"} == 1
        ) or on() vector(0)
    ''',

    # Pod Ready Status - detect unready pods
    "pod_not_ready": '''
        sum by (namespace, pod) (
            kube_pod_status_ready{condition="false"} == 1
        ) or on() vector(0)
    ''',

    # Pod Containers in Waiting state with reasons
    "pod_container_waiting": '''
        sum by (namespace, pod, reason) (
            kube_pod_container_status_waiting_reason
        ) > 0
    ''',

    # Deployment/StatefulSet availability
    "deployment_replicas_available_ratio": '''
        kube_deployment_status_replicas_available / 
        clamp_min(kube_deployment_spec_replicas, 1)
    ''',
    "statefulset_replicas_available_ratio": '''
        kube_statefulset_status_replicas_ready / 
        clamp_min(kube_statefulset_replicas, 1)
    ''',

    # Failure states
    "pod_crashloopbackoff": '''
        sum by (namespace, pod) (
            kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}
        ) or on() vector(0)
    ''',
    "pod_imagepullbackoff": '''
        sum by (namespace, pod) (
            kube_pod_container_status_waiting_reason{reason="ImagePullBackOff"} or
            kube_pod_container_status_waiting_reason{reason="ErrImagePull"}
        ) > 0
    ''',

    # Pod terminations and restarts
    "pod_container_restarts": '''
        sum by (namespace, pod) (
            delta(kube_pod_container_status_restarts_total[5m])
        ) > 2
    ''',

    # Failed pods
    "pod_failed": '''
        sum by (namespace, pod) (
            kube_pod_status_phase{phase="Failed"} or
            kube_pod_status_phase{phase="Unknown"}
        ) > 0
    ''',

    # Network / Connectivity Issues
    "pod_tcp_retransmits_per_second": '''
        sum by (instance) (
            rate(node_netstat_Tcp_RetransSegs[5m])
        )
    ''',
    
    # DNS failures
    "pod_dns_request_failures": '''
        sum(rate(coredns_dns_response_rcode_count{rcode=~"SERVFAIL|REFUSED"}[5m])) or on() vector(0)
    ''',

    # Node conditions related to resource exhaustion
    "node_not_ready": '''
        sum(kube_node_status_condition{condition="Ready", status="false"}) or on() vector(0)
    ''',
    "node_memory_pressure": '''
        sum(kube_node_status_condition{condition="MemoryPressure", status="true"}) or on() vector(0)
    ''',
    "node_disk_pressure": '''
        sum(kube_node_status_condition{condition="DiskPressure", status="true"}) or on() vector(0)
    ''',
    "node_pid_pressure": '''
        sum(kube_node_status_condition{condition="PIDPressure", status="true"}) or on() vector(0)
    ''',
    
    # Node resource utilization
    "node_load_high": '''
        node_load5 / on (instance) group_left() count by (instance) (node_cpu_seconds_total{mode="idle"}) > 2
    ''',
    "node_disk_usage_pct": '''
        (1 - node_filesystem_avail_bytes{fstype=~"ext4|xfs"} / node_filesystem_size_bytes{fstype=~"ext4|xfs"}) * 100
    ''',
}


class Settings(BaseSettings):
    """
    Application configuration settings loaded from environment variables or .env file.

    Attributes:
        mongo_uri: MongoDB Atlas connection string.
        prom_url: URL for the Prometheus server.
        gemini_api_key: API key for Google Gemini.
        mongo_db_name: Name of the MongoDB database to use.
        anomaly_thresholds: Dictionary mapping query names (or 'default') to score thresholds (0.0 to 1.0).
        log_level: Logging level for the application.
        gemini_model_id: Identifier for the Gemini model to use.
        prom_queries: Dictionary of Prometheus queries to execute.
        prom_queries_poll_interval: Interval in seconds between Prometheus query polls.
    """

    # Environment Variable Sources
    mongo_uri: str = Field(..., validation_alias="MONGO_URI")
    mongo_db_name: str = Field(..., validation_alias="MONGO_DB_NAME")
    prom_url: HttpUrl = Field("http://localhost:9090", validation_alias="PROM_URL")
    gemini_api_key: SecretStr = Field(..., validation_alias="GEMINI_API_KEY")
    anomaly_thresholds: Dict[str, float] = Field(
        default_factory=lambda: {"default": 0.85, "memory": 0.95},
        validation_alias="ANOMALY_THRESHOLDS"
    )
    log_level: LogLevel = Field("INFO", validation_alias="LOG_LEVEL")
    gemini_model_id: str = Field("gemini-1.5-flash-001", validation_alias="GEMINI_MODEL_ID")
    prom_queries: Dict[str, str] = Field(
        default_factory=lambda: DEFAULT_PROM_QUERIES.copy(),
        validation_alias="PROM_QUERIES"
    )
    prom_queries_poll_interval: float = Field(60.0, validation_alias="PROM_QUERIES_POLL_INTERVAL")

    # Anomaly Detector Model Settings
    hst_n_trees: int = Field(25, validation_alias="HST_N_TREES", description="Number of trees in the Half-Space Trees model.")
    hst_height: int = Field(15, validation_alias="HST_HEIGHT", description="Height of trees in the Half-Space Trees model.")
    hst_window_size: int = Field(500, validation_alias="HST_WINDOW_SIZE", description="Window size for the Half-Space Trees model.")
    river_trees: int = Field(50, validation_alias="RIVER_TREES", description="Number of trees in the River Adaptive Random Forest model.")
    detector_save_interval: int = Field(300, validation_alias="DETECTOR_SAVE_INTERVAL", description="Interval in seconds to save anomaly detector state.")

    # Remediation Settings
    remediation_cooldown_seconds: int = Field(
        10,
        validation_alias="REMEDIATION_COOLDOWN_SECONDS",
        description="Minimum time in seconds between remediation actions for the same entity."
    )

    max_parallel_remediation_actions: int = Field(
        5,
        validation_alias="MAX_PARALLEL_REMEDIATION_ACTIONS",
        description="Maximum number of remediation actions to execute in parallel."
    )

    # False Positive Heuristic Settings
    fp_hst_score_threshold: float = Field(
        0.90,
        validation_alias="FP_HST_SCORE_THRESHOLD",
        description="HST score above which (combined with low River score) an anomaly might be a false positive.",
        ge=0.0, le=1.0
    )
    fp_river_score_threshold: float = Field(
        0.10,
        validation_alias="FP_RIVER_SCORE_THRESHOLD",
        description="River score below which (combined with high HST score) an anomaly might be a false positive.",
        ge=0.0, le=1.0
    )
    fp_system_mem_mib_threshold: float = Field(
        500.0,
        validation_alias="FP_SYSTEM_MEM_MIB_THRESHOLD",
        description="Memory usage (in MiB) below which a memory anomaly in system namespaces is considered a likely false positive.",
        gt=0.0
    )
    fp_score_difference_threshold: float = Field(
        0.75,
        validation_alias="FP_SCORE_DIFFERENCE_THRESHOLD",
        description="Score difference (HST - River) above which (combined with high HST score) an anomaly might be a false positive.",
        ge=0.0, le=1.0
    )
    system_namespaces: List[str] = Field(
        default_factory=lambda: ["kube-system", "kube-public", "monitoring", "default", "kubewise"],
        validation_alias="KUBEWISE_SYSTEM_NAMESPACES",
        description="List of Kubernetes namespaces considered system namespaces for heuristic adjustments."
    )
    
    # Event Processing Settings
    event_max_age_seconds: int = Field(
        300,
        validation_alias="EVENT_MAX_AGE_SECONDS",
        description="Maximum age in seconds for events to be considered for anomaly detection. "
                    "Older events are likely already resolved and will be ignored.",
        gt=0
    )

    # Resource Health Check Settings
    resource_check_interval: int = Field(
        300,
        validation_alias="RESOURCE_CHECK_INTERVAL",
        description="How often to check the health status of resources (in seconds)",
        gt=0
    )

    resource_health_ttl: int = Field(
        900,
        validation_alias="RESOURCE_HEALTH_TTL",
        description="Time-to-live for resource health information (in seconds)",
        gt=0
    )

    # Pydantic Settings Configuration
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level_case_insensitive(cls, value: str) -> str:
        """Ensure log level is uppercase for validation."""
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("anomaly_thresholds")
    @classmethod
    def validate_anomaly_thresholds(cls, thresholds: Dict[str, float]) -> Dict[str, float]:
        """Validate that all anomaly threshold values are between 0.0 and 1.0."""
        for key, value in thresholds.items():
            if not isinstance(value, (int, float)) or not (0.0 <= value <= 1.0):
                raise ValueError(f"Anomaly threshold for '{key}' must be between 0.0 and 1.0, got {value}")
        if "default" not in thresholds:
             thresholds["default"] = 0.8
             logger.warning("No 'default' anomaly threshold provided, using 0.8.")
        return thresholds

    @property
    def log_level_int(self) -> int:
        """Return the integer value corresponding to the log level string."""
        level = logging.getLevelName(self.log_level)
        return level if isinstance(level, int) and level != 0 else logging.INFO


# Create a single instance of the settings to be imported across the application
settings = Settings()

# Example usage (for testing or direct script execution):
if __name__ == "__main__":
    print("--- KubeWise Configuration ---")
    print(f"Log Level: {settings.log_level} (Numeric: {settings.log_level_int})")
    print(f"Prometheus URL: {settings.prom_url!s}")
    print(f"Mongo URI: {settings.mongo_uri!s}")
    print(f"Mongo DB Name: {settings.mongo_db_name}")
    print(f"Anomaly Thresholds: {settings.anomaly_thresholds}")
    print(f"Gemini Model ID: {settings.gemini_model_id}")
    api_key_masked = f"{settings.gemini_api_key.get_secret_value()[:4]}..." if settings.gemini_api_key else "Not Set"
    print(f"Gemini API Key: {api_key_masked}")
    print(f"Prometheus Poll Interval: {settings.prom_queries_poll_interval}s")
    print(f"Total Prometheus Queries: {len(settings.prom_queries)}")
    print(f"HST Trees: {settings.hst_n_trees}, Height: {settings.hst_height}, Window: {settings.hst_window_size}")
    print(f"River Trees: {settings.river_trees}")
    print(f"Detector Save Interval: {settings.detector_save_interval}s")
    print(f"Remediation Cooldown: {settings.remediation_cooldown_seconds}s")
    print(f"FP HST Score Threshold: {settings.fp_hst_score_threshold}")
    print(f"FP River Score Threshold: {settings.fp_river_score_threshold}")
    print(f"FP System Mem MiB Threshold: {settings.fp_system_mem_mib_threshold}")
    print(f"System Namespaces: {settings.system_namespaces}")
    print("-----------------------------")
