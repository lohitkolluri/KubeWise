import math

# NOTE: This implementation is superseded by the version in utils/__init__.py.
# TODO: This file should be removed, and any unique functionality merged into utils/__init__.py
def format_metric_value(metric_name: str, value: float) -> str:
    """
    Formats a raw metric value into a human-readable string based on the metric name.

    Args:
        metric_name: The name of the metric (e.g., 'pod_memory_working_set_bytes').
        value: The raw numeric value of the metric.

    Returns:
        A formatted string representation of the value (e.g., '512.3 MiB', '1.5 cores', '85.6%').
    """
    if value is None:
        return "N/A"

    name_lower = metric_name.lower()

    # Memory (Bytes)
    if "bytes" in name_lower or "memory" in name_lower:
        if abs(value) < 1024:
            return f"{value:.1f} B"
        elif abs(value) < 1024**2:
            return f"{value / 1024:.1f} KiB"
        elif abs(value) < 1024**3:
            return f"{value / (1024**2):.1f} MiB"
        elif abs(value) < 1024**4:
            return f"{value / (1024**3):.1f} GiB"
        else:
            return f"{value / (1024**4):.1f} TiB"

    # CPU (Cores or Seconds Total -> Cores)
    if "cpu" in name_lower and ("usage" in name_lower or "seconds" in name_lower or "utilization" in name_lower):
         # Handle percentages
        if "pct" in name_lower or "utilization_pct" in name_lower:
             return f"{value:.1f}%"
        # Handle core values
        if abs(value) < 1.0:
            return f"{value * 1000:.1f} mCores"
        else:
            return f"{value:.2f} Cores"

    # Network/Disk Rates (Bytes/Second)
    if ("network" in name_lower or "disk" in name_lower or "fs_" in name_lower) and ("rate" in name_lower or "bytes" in name_lower or "iops" in name_lower):
        if "iops" in name_lower:
             return f"{value:.1f} IOPS"
        # Handle Bytes/Second
        if abs(value) < 1024:
            return f"{value:.1f} B/s"
        elif abs(value) < 1024**2:
            return f"{value / 1024:.1f} KiB/s"
        elif abs(value) < 1024**3:
            return f"{value / (1024**2):.1f} MiB/s"
        else:
            return f"{value / (1024**3):.1f} GiB/s"

    # Time (Seconds) - non-CPU metrics
    if "seconds" in name_lower and "cpu" not in name_lower:
        if abs(value) < 1:
            return f"{value * 1000:.1f} ms"
        elif abs(value) < 60:
            return f"{value:.2f} s"
        elif abs(value) < 3600:
            return f"{value / 60:.1f} min"
        else:
            return f"{value / 3600:.1f} h"

    # Percentages
    if "percent" in name_lower or "_pct" in name_lower:
        return f"{value:.1f}%"

    # Counts (e.g., restarts, events)
    if "count" in name_lower or "restarts" in name_lower:
        return f"{int(value)}"

    # Default formatting for unknown types
    if isinstance(value, int):
        return str(value)
    elif isinstance(value, float):
        if abs(value) > 1000 or abs(value) < 0.01 and value != 0:
             return f"{value:.2e}"  # Scientific notation for very large/small numbers
        else:
             return f"{value:.2f}"  # Standard decimal format
    else:
        return str(value)

# Example Usage (can be run directly for testing)
if __name__ == "__main__":
    test_cases = {
        "pod_memory_working_set_bytes": 550 * 1024**2, # 550 MiB
        "container_cpu_usage_seconds_total": 1.75, # 1.75 Cores
        "container_cpu_usage_seconds_total_rate": 0.25, # 250 mCores
        "node_network_receive_bytes_total": 1200 * 1024, # 1.2 MiB/s (assuming rate)
        "kube_pod_container_status_restarts_total": 5,
        "process_uptime_seconds": 7250, # ~2 hours
        "cpu_utilization_pct": 85.6,
        "event_oomkilled_count": 3.0, # Float from decay
        "disk_read_rate": 50 * 1024**2, # 50 MiB/s
        "some_unknown_metric": 12345.6789,
        "very_small_value": 0.0000123,
    }

    for name, val in test_cases.items():
        print(f"{name}: {val} -> {format_metric_value(name, val)}")

    print(f"None value -> {format_metric_value('any_metric', None)}")
