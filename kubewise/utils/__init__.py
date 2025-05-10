"""Utility functions for KubeWise."""

from .retry import with_exponential_backoff


# Define format_metric_value function
def format_metric_value(metric_name: str, value: float) -> str:
    """Format the metric value for display based on metric type."""
    if not metric_name or not isinstance(value, (int, float)):
        return str(value) if value is not None else ""

    # Handle percentage-based metrics
    if (
        metric_name.endswith("_pct")
        or metric_name.endswith("_percent")
        or metric_name.endswith("_percentage")
        or "utilization" in metric_name
    ):
        return f"{value:.1f}%"

    # Handle time-based metrics
    if (
        metric_name.endswith("_seconds")
        or metric_name.endswith("_ms")
        or "latency" in metric_name
        or "duration" in metric_name
    ):
        if value < 1:
            return f"{value * 1000:.0f}ms"
        elif value < 60:
            return f"{value:.1f}s"
        elif value < 3600:
            return f"{value / 60:.1f}m"
        else:
            return f"{value / 3600:.1f}h"

    # Handle bytes-based metrics
    if (
        metric_name.endswith("_bytes")
        or "memory" in metric_name
        or "disk" in metric_name
        or "storage" in metric_name
    ):
        if value < 1024:
            return f"{value:.0f}B"
        elif value < 1024 * 1024:
            return f"{value / 1024:.1f}KB"
        elif value < 1024 * 1024 * 1024:
            return f"{value / (1024 * 1024):.1f}MB"
        else:
            return f"{value / (1024 * 1024 * 1024):.1f}GB"

    # Default format based on value range
    if value > 1000000:
        return f"{value / 1000000:.1f}M"
    elif value > 1000:
        return f"{value / 1000:.1f}K"
    elif isinstance(value, int) or value.is_integer():
        return f"{int(value)}"
    else:
        return f"{value:.2f}"


__all__ = ["with_exponential_backoff", "format_metric_value"]
