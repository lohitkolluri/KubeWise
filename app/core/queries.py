"""
Centralized PromQL Query Definitions

This module defines and organizes all PromQL queries used across the application.
It ensures consistent usage, easier maintenance, and improved visibility into metrics logic.
"""

from typing import Dict, List, Optional
from app.core.config import settings

# ===============================
# Standard Prometheus Queries
# ===============================

STANDARD_QUERIES: Dict[str, str] = {
    # CPU usage per container
    "cpu_usage_seconds_total": "sum by (namespace, pod) (rate(container_cpu_usage_seconds_total[5m]))",

    "node_cpu_usage": "sum by (instance) (rate(node_cpu_seconds_total{mode!=\"idle\"}[5m]))",

    # Memory usage per container
    "memory_working_set_bytes": "sum by (namespace, pod) (container_memory_working_set_bytes)",

    # Network I/O per container
    "network_receive_bytes_total": "sum by (namespace, pod) (rate(container_network_receive_bytes_total[5m]))",
    "network_transmit_bytes_total": "sum by (namespace, pod) (rate(container_network_transmit_bytes_total[5m]))",

    # Pod and container status
    "pod_status_phase": "sum by (namespace, pod) (kube_pod_status_phase{phase=\"Running\"})",
    "container_restarts_rate": "sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[5m]))",

    # Node resource usage
    "node_cpu_usage": "sum by (instance) (rate(node_cpu_seconds_total{mode!=\"idle\"}[5m]))",
    "node_memory_usage": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",

    # Deployment availability
    "deployment_replicas_available": "sum by (namespace, deployment) (kube_deployment_status_replicas_available)",
    "deployment_replicas_unavailable": "sum by (namespace, deployment) (kube_deployment_status_replicas_unavailable)",

    # Disk usage (container level)
    "disk_usage_bytes": "sum by (namespace, pod) (container_fs_usage_bytes)",

    # Node-level filesystem metrics
    "filesystem_available": "node_filesystem_avail_bytes{mountpoint=\"/\", fstype!=\"rootfs\"}",
    "filesystem_size": "node_filesystem_size_bytes{mountpoint=\"/\", fstype!=\"rootfs\"}",
    "filesystem_usage_percent": "100 - ((node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100)",
}

DEFAULT_QUERY_LIST: List[str] = [
    STANDARD_QUERIES["cpu_usage_seconds_total"],
    STANDARD_QUERIES["memory_working_set_bytes"],
    STANDARD_QUERIES["network_receive_bytes_total"],
    STANDARD_QUERIES["network_transmit_bytes_total"],
    STANDARD_QUERIES["pod_status_phase"],
    STANDARD_QUERIES["container_restarts_rate"],
    STANDARD_QUERIES["node_cpu_usage"],
    STANDARD_QUERIES["node_memory_usage"],
    STANDARD_QUERIES["deployment_replicas_available"],
    STANDARD_QUERIES["disk_usage_bytes"],
    STANDARD_QUERIES["filesystem_available"],
    STANDARD_QUERIES["filesystem_size"],
    STANDARD_QUERIES["filesystem_usage_percent"],
]

# ===============================
# Resource Status Queries
# ===============================

RESOURCE_STATUS_QUERIES: Dict[str, str] = {
    "pod_running_status": "sum by (namespace, pod) (kube_pod_status_phase{phase=\"Running\"}) == 1",
    "deployment_replicas_available": STANDARD_QUERIES["deployment_replicas_available"],
    "deployment_replicas_unavailable": STANDARD_QUERIES["deployment_replicas_unavailable"],
    "node_ready_status": "kube_node_status_condition{condition=\"Ready\", status=\"true\"}",
}

# ===============================
# Resource Usage Queries
# ===============================

RESOURCE_USAGE_QUERIES: Dict[str, str] = {
    "cpu_usage_percent_node": (
        "sum by (instance) (rate(node_cpu_seconds_total{mode!=\"idle\"}[5m])) "
        "/ count by (instance) (node_cpu_seconds_total{mode=\"idle\"}) * 100"
    ),
    "disk_usage_percent_node": STANDARD_QUERIES["filesystem_usage_percent"],
}

# ===============================
# Failure Detection Queries
# ===============================

FAILURE_DETECTION_QUERIES: Dict[str, str] = {
    "crashloop_backoff_containers": (
        "sum by (namespace, pod) (kube_pod_container_status_waiting_reason{reason=\"CrashLoopBackOff\"}) > 0"
    ),
    "imagepull_backoff_containers": (
        "sum by (namespace, pod) (kube_pod_container_status_waiting_reason{reason=\"ImagePullBackOff\"}) > 0"
    ),
    "createcontainer_error": (
        "sum by (namespace, pod) (kube_pod_container_status_waiting_reason{reason=\"CreateContainerError\"}) > 0"
    ),
    "high_restart_count": (
        "sum by (namespace, pod) (kube_pod_container_status_restarts_total) > 5"
    ),
    "oom_killed_containers": (
        "sum by (namespace, pod) (kube_pod_container_status_last_terminated_reason{reason=\"OOMKilled\"}) > 0"
    ),
}

# ===============================
# Helper Functions
# ===============================

def get_query_for_metric(metric_type: str) -> Optional[str]:
    """Retrieve the PromQL query for a specific metric type."""
    return (
        STANDARD_QUERIES.get(metric_type)
        or RESOURCE_STATUS_QUERIES.get(metric_type)
        or RESOURCE_USAGE_QUERIES.get(metric_type)
        or FAILURE_DETECTION_QUERIES.get(metric_type)
    )

def get_default_query_list() -> List[str]:
    """Return the default PromQL queries to execute."""
    if hasattr(settings, "DEFAULT_PROMQL_QUERIES") and settings.DEFAULT_PROMQL_QUERIES:
        return list(settings.DEFAULT_PROMQL_QUERIES) + [FAILURE_DETECTION_QUERIES["oom_killed_containers"]]
    return DEFAULT_QUERY_LIST + [FAILURE_DETECTION_QUERIES["oom_killed_containers"]]

def get_fallback_query_list() -> List[str]:
    """Return a minimal, highly reliable set of fallback queries."""
    return [
        STANDARD_QUERIES["cpu_usage_seconds_total"],
        STANDARD_QUERIES["memory_working_set_bytes"],
        STANDARD_QUERIES["node_cpu_usage"],
        STANDARD_QUERIES["node_memory_usage"],
        STANDARD_QUERIES["pod_status_phase"],
        STANDARD_QUERIES["deployment_replicas_available"],
    ]

def get_metric_type_from_query(query: str) -> Optional[str]:
    """Attempt to infer the metric type from a PromQL query string."""
    query_lower = query.lower()
    for metric_type, q in STANDARD_QUERIES.items():
        if q.lower() in query_lower or metric_type.lower() in query_lower:
            return metric_type

    if "vector(1) as" in query_lower:
        try:
            return query.split("vector(1) as ")[1].split("_placeholder")[0].strip()
        except (IndexError, AttributeError):
            return None

    keyword_map = {
        "cpu": "cpu_usage_seconds_total",
        "memory": "memory_working_set_bytes",
        "receive": "network_receive_bytes_total",
        "transmit": "network_transmit_bytes_total",
        "phase": "pod_status_phase",
        "restart": "container_restarts_rate",
        "node cpu": "node_cpu_usage",
        "node memory": "node_memory_usage",
        "available": "deployment_replicas_available",
        "unavailable": "deployment_replicas_unavailable",
        "fs_usage": "disk_usage_bytes",
        "fs_reads": "disk_reads_bytes",
        "fs_writes": "disk_writes_bytes",
    }

    for keyword, metric in keyword_map.items():
        if keyword in query_lower:
            return metric

    return None

def create_placeholder_query(metric_type: str) -> str:
    """Return a real fallback query for the requested metric type."""
    fallback_map = {
        "cpu": "cpu_usage_seconds_total",
        "memory": "memory_working_set_bytes",
        "receive": "network_receive_bytes_total",
        "transmit": "network_transmit_bytes_total",
        "deployment": "deployment_replicas_available",
        "pod": "pod_status_phase",
        "container": "pod_status_phase",
        "disk": "disk_usage_bytes",
        "filesystem": "disk_usage_bytes",
        "storage": "disk_usage_bytes",
        "node": "node_cpu_usage",
    }

    for keyword, metric_key in fallback_map.items():
        if keyword in metric_type.lower():
            return STANDARD_QUERIES[metric_key]

    return STANDARD_QUERIES["cpu_usage_seconds_total"]
