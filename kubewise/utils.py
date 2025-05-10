# -*- coding: utf-8 -*-


def format_metric_value(metric_name: str, value: float) -> str:
    """Formats a raw metric value into a human-readable string with appropriate units.

    Determines units (e.g., MiB, Cores, %, B/s) based on common patterns
    in metric names.

    Args:
        metric_name: The name of the metric.
        value: The raw numeric value.

    Returns:
        A formatted string representation (e.g., '512.3 MiB', '1.5 Cores', '85.6%')
        or "N/A" if the value is None.
    """
    if value is None:
        return "N/A"

    name_lower = metric_name.lower()

    # Memory sizes
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

    # CPU usage or utilization
    if "cpu" in name_lower and (
        "usage" in name_lower or "seconds" in name_lower or "utilization" in name_lower
    ):
        if "pct" in name_lower or "utilization_pct" in name_lower:
            return f"{value:.1f}%"  # Percentage format
        # Core format (milliCores or Cores)
        if abs(value) < 1.0:
            return f"{value * 1000:.1f} mCores"
        else:
            return f"{value:.2f} Cores"

    # Network/Disk rates or sizes
    if ("network" in name_lower or "disk" in name_lower or "fs_" in name_lower) and (
        "rate" in name_lower or "bytes" in name_lower or "iops" in name_lower
    ):
        if "iops" in name_lower:
            return f"{value:.1f} IOPS"
        # Bytes/Second format
        if abs(value) < 1024:
            return f"{value:.1f} B/s"
        elif abs(value) < 1024**2:
            return f"{value / 1024:.1f} KiB/s"
        elif abs(value) < 1024**3:
            return f"{value / (1024**2):.1f} MiB/s"
        else:
            return f"{value / (1024**3):.1f} GiB/s"

    # Time durations (non-CPU)
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

    # Simple counts
    if "count" in name_lower or "restarts" in name_lower:
        return f"{int(value)}"

    # Default formatting for unrecognized metric types
    if isinstance(value, int):
        return str(value)
    elif isinstance(value, float):
        # Use scientific notation for very large or small magnitudes
        if abs(value) > 1000 or (abs(value) < 0.01 and value != 0):
            return f"{value:.2e}"
        else:
            return f"{value:.2f}"
    else:
        return str(value)
