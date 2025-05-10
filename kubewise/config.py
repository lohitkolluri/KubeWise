# -*- coding: utf-8 -*-
import logging
from typing import Literal, Dict, List
from textwrap import dedent

from loguru import logger
from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_PROM_QUERIES = {
    # Resource Utilization Metrics
    "pod_cpu_utilization_pct": dedent("""
        sum by (namespace, pod) (
            rate(container_cpu_usage_seconds_total{container!="POD", container!=""}[5m])
        ) * 100
    """),
    "pod_memory_utilization_pct": dedent("""
        sum by (namespace, pod) (
            container_memory_usage_bytes{container!="POD", container!=""}
        ) / 1024 / 1024
    """),
    
    # Direct Failure Indicators
    "pod_oom_events": dedent("""
        sum by (namespace, pod) (
            container_oom_events_total{container!=""}
        ) > 0
    """),
    "pod_oomkilled_state": dedent("""
        sum by (namespace, pod) (
            kube_pod_container_status_terminated_reason{reason="OOMKilled"}
        ) > 0
    """),
    "pod_not_ready": dedent("""
        sum by (namespace, pod) (
            kube_pod_status_ready{condition="false"}
        ) > 0
    """),
    "pod_container_waiting": dedent("""
        sum by (namespace, pod, reason) (
            kube_pod_container_status_waiting_reason
        ) > 0
    """),
    
    # Deployment/StatefulSet Availability
    "deployment_replicas_unavailable": dedent("""
        sum(kube_deployment_status_replicas_unavailable)
    """),
    "statefulset_replicas_available_ratio": dedent("""
        kube_statefulset_status_replicas_ready / 
        clamp_min(kube_statefulset_replicas, 1)
    """),
    
    # Failure States
    "pod_crashloopbackoff": dedent("""
        sum by (namespace, pod) (
            kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}
        ) > 0
    """),
    "pod_imagepullbackoff": dedent("""
        sum by (namespace, pod) (
            kube_pod_container_status_waiting_reason{reason=~"ImagePullBackOff|ErrImagePull"}
        ) > 0
    """),
    "pod_failed": dedent("""
        sum by (namespace) (
            kube_pod_status_phase{phase=~"Failed|Unknown"}
        )
    """),
    
    # Pod Restarts
    "pod_container_restarts": dedent("""
        increase(kube_pod_container_status_restarts_total[5m])
    """),
    
    # Network / Connectivity Issues
    "pod_tcp_retransmits_per_second": dedent("""
        rate(node_network_receive_errors_total[5m]) + rate(node_network_transmit_errors_total[5m])
    """),
    "pod_dns_request_failures": dedent("""
        rate(coredns_dns_request_count_total{rcode=~"SERVFAIL|NXDOMAIN"}[5m])
    """),
    
    # Node Conditions
    "node_not_ready": dedent("""
        kube_node_status_condition{condition="Ready",status="false"}
    """),
    "node_memory_pressure": dedent("""
        kube_node_status_condition{condition="MemoryPressure",status="true"}
    """),
    "node_disk_pressure": dedent("""
        kube_node_status_condition{condition="DiskPressure",status="true"}
    """),
    "node_pid_pressure": dedent("""
        kube_node_status_condition{condition="PIDPressure",status="true"}
    """),
    
    # Node Resource Utilization
    "node_load_high": dedent("""
        node_load5 / on(instance) group_left() count by(instance)(node_cpu_seconds_total{mode="idle"}) > 2
    """),
    "node_disk_usage_pct": dedent("""
        (1 - (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})) * 100
    """),
}


class Settings(BaseSettings):
    """
    Application configuration settings loaded from environment variables or .env file.
    """

    mongo_uri: str = Field(..., validation_alias="MONGO_URI")
    mongo_db_name: str = Field(..., validation_alias="MONGO_DB_NAME")
    prom_url: HttpUrl = Field("http://localhost:9090", validation_alias="PROM_URL")
    gemini_api_key: SecretStr = Field(..., validation_alias="GEMINI_API_KEY")
    anomaly_thresholds: Dict[str, float] = Field(
        default_factory=lambda: {"default": 0.85, "memory": 0.95},
        validation_alias="ANOMALY_THRESHOLDS",
        description="Score thresholds (0.0-1.0) for anomaly detection, keyed by query name or 'default'.",
    )
    log_level: LogLevel = Field("INFO", validation_alias="LOG_LEVEL")
    gemini_model_id: str = Field("gemini-2.0-flash", validation_alias="GEMINI_MODEL_ID")
    prom_queries: Dict[str, str] = Field(
        default_factory=lambda: DEFAULT_PROM_QUERIES.copy(),
        validation_alias="PROM_QUERIES",
        description="Prometheus queries used for anomaly detection.",
    )
    prom_queries_poll_interval: float = Field(
        60.0, validation_alias="PROM_QUERIES_POLL_INTERVAL"
    )

    # Anomaly Detector Model Settings
    hst_n_trees: int = Field(
        25,
        validation_alias="HST_N_TREES",
        description="Number of trees in the Half-Space Trees model.",
    )
    hst_height: int = Field(
        15,
        validation_alias="HST_HEIGHT",
        description="Height of trees in the Half-Space Trees model.",
    )
    hst_window_size: int = Field(
        500,
        validation_alias="HST_WINDOW_SIZE",
        description="Window size for the Half-Space Trees model.",
    )
    river_trees: int = Field(
        50,
        validation_alias="RIVER_TREES",
        description="Number of trees in the River Adaptive Random Forest model.",
    )
    detector_save_interval: int = Field(
        300,
        validation_alias="DETECTOR_SAVE_INTERVAL",
        description="Interval (seconds) to save anomaly detector state.",
    )

    # IsolationForest Settings
    iso_n_trees: int = Field(
        100,
        validation_alias="ISO_N_TREES",
        description="Number of trees in the IsolationForest model.",
    )
    iso_subspace_size: int = Field(
        10,
        validation_alias="ISO_SUBSPACE_SIZE",
        description="Feature subset size for IsolationForest splits.",
    )
    iso_seed: int = Field(
        12345,
        validation_alias="ISO_SEED",
        description="Random seed for IsolationForest.",
    )
    model_persist_interval: int = Field(
        300,
        validation_alias="MODEL_PERSIST_INTERVAL",
        description="Interval (seconds) between model persistence operations.",
    )

    # Remediation Settings
    remediation_cooldown_seconds: int = Field(
        300,
        validation_alias="REMEDIATION_COOLDOWN_SECONDS",
        description="Minimum time (seconds) between remediation actions for the same entity.",
    )
    remediation_disabled: bool = Field(
        False,
        validation_alias="REMEDIATION_DISABLED",
        description="Disable automatic remediation actions if True.",
    )
    max_parallel_remediation_actions: int = Field(
        3,
        validation_alias="MAX_PARALLEL_REMEDIATION_ACTIONS",
        description="Maximum concurrent remediation actions.",
    )
    blacklisted_namespaces: List[str] = Field(
        default_factory=lambda: ["monitoring", "kube-system", "kube-public"],
        validation_alias="BLACKLISTED_NAMESPACES",
        description="Namespaces excluded from remediation actions.",
    )

    # Diagnosis-Driven Workflow Settings
    enable_diagnosis_workflow: bool = Field(
        True,
        validation_alias="ENABLE_DIAGNOSIS_WORKFLOW",
        description="Use diagnosis workflow for remediation if True.",
    )
    diagnosis_timeout_seconds: int = Field(
        600,
        validation_alias="DIAGNOSIS_TIMEOUT_SECONDS",
        description="Maximum time (seconds) for diagnosis workflow completion.",
    )
    diagnosis_confidence_threshold: float = Field(
        0.7,
        validation_alias="DIAGNOSIS_CONFIDENCE_THRESHOLD",
        description="Minimum confidence required to proceed with remediation after diagnosis.",
        ge=0.0,
        le=1.0,
    )
    diagnosis_require_validation: bool = Field(
        True,
        validation_alias="DIAGNOSIS_REQUIRE_VALIDATION",
        description="Require diagnosis validation before remediation if True.",
    )
    diagnostic_max_findings: int = Field(
        10,
        validation_alias="DIAGNOSTIC_MAX_FINDINGS",
        description="Maximum findings included in a diagnosis report.",
    )
    diagnostic_tool_timeout_seconds: int = Field(
        30,
        validation_alias="DIAGNOSTIC_TOOL_TIMEOUT_SECONDS",
        description="Maximum time (seconds) for a single diagnostic tool execution.",
    )

    # AI Model Settings
    ai_model_inference: str = Field(
        "",  # Will default to the model specified in gemini_model_id
        validation_alias="AI_MODEL_INFERENCE",
        description="Specific model to use for inference tasks (if empty, uses gemini_model_id)",
    )
    ai_temperature: float = Field(
        0.2,
        validation_alias="AI_TEMPERATURE",
        description="Temperature setting for AI model generation (0.0-1.0)",
        ge=0.0,
        le=1.0,
    )
    ai_max_tokens: int = Field(
        4096,
        validation_alias="AI_MAX_TOKENS",
        description="Maximum tokens to generate in AI responses",
        gt=0,
    )

    # Email Settings
    smtp_server: str = Field(
        None,
        validation_alias="SMTP_SERVER",
        description="SMTP server address for sending emails."
    )
    smtp_port: int = Field(
        None,
        validation_alias="SMTP_PORT",
        description="SMTP server port for sending emails."
    )
    smtp_sender_email: str = Field(
        None,
        validation_alias="SMTP_SENDER_EMAIL",
        description="Sender email address for KubeWise notifications."
    )
    smtp_password: SecretStr = Field(
        None,
        validation_alias="SMTP_PASSWORD",
        description="Password for the sender email address (if required)."
    )

    # False Positive Heuristic Settings
    fp_hst_score_threshold: float = Field(
        0.90,
        validation_alias="FP_HST_SCORE_THRESHOLD",
        description="HST score threshold for potential false positive identification.",
        ge=0.0,
        le=1.0,
    )
    fp_river_score_threshold: float = Field(
        0.10,
        validation_alias="FP_RIVER_SCORE_THRESHOLD",
        description="River score threshold for potential false positive identification.",
        ge=0.0,
        le=1.0,
    )
    fp_system_mem_mib_threshold: float = Field(
        500.0,
        validation_alias="FP_SYSTEM_MEM_MIB_THRESHOLD",
        description="Memory usage (MiB) threshold for system namespace false positive heuristic.",
        gt=0.0,
    )
    fp_score_difference_threshold: float = Field(
        0.75,
        validation_alias="FP_SCORE_DIFFERENCE_THRESHOLD",
        description="Score difference (HST - River) threshold for potential false positive identification.",
        ge=0.0,
        le=1.0,
    )
    system_namespaces: List[str] = Field(
        default_factory=lambda: [
            "kube-system",
            "kube-public",
            "monitoring",
            "default",
            "kubewise",
        ],
        validation_alias="KUBEWISE_SYSTEM_NAMESPACES",
        description="Namespaces considered 'system' for heuristic adjustments.",
    )

    # Event Processing Settings
    event_max_age_seconds: int = Field(
        300,
        validation_alias="EVENT_MAX_AGE_SECONDS",
        description="Maximum age (seconds) for events considered in anomaly detection.",
        gt=0,
    )

    # Resource Health Check Settings
    resource_check_interval: int = Field(
        300,
        validation_alias="RESOURCE_CHECK_INTERVAL",
        description="Frequency (seconds) of resource health checks.",
        gt=0,
    )
    resource_health_ttl: int = Field(
        900,
        validation_alias="RESOURCE_HEALTH_TTL",
        description="TTL (seconds) for cached resource health information.",
        gt=0,
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level_case_insensitive(cls, value: str) -> str:
        """Ensure log level is uppercase before validation."""
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("anomaly_thresholds")
    @classmethod
    def validate_anomaly_thresholds(
        cls, thresholds: Dict[str, float]
    ) -> Dict[str, float]:
        """Validate anomaly threshold values are between 0.0 and 1.0."""
        for key, value in thresholds.items():
            if not isinstance(value, (int, float)) or not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"Anomaly threshold for '{key}' must be between 0.0 and 1.0, got {value}"
                )
        if "default" not in thresholds:
            thresholds["default"] = 0.8  # Provide a sensible default if missing
            logger.warning("No 'default' anomaly threshold provided, using 0.8.")
        return thresholds

    @property
    def log_level_int(self) -> int:
        """Return the integer logging level."""
        level = logging.getLevelName(self.log_level)
        # Ensure a valid integer level is returned, default to INFO if invalid
        return (
            level
            if isinstance(level, int) and level != logging.NOTSET
            else logging.INFO
        )


# Singleton instance of settings for application-wide use.
settings = Settings()
