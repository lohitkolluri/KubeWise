"""
Utility for extracting and processing metrics from Prometheus.
Handles various formats and provides robust extraction methods.
"""
from typing import Any, Dict, List, Optional, Union, Tuple
from loguru import logger
import re


class MetricExtractor:
    """
    Extracts and processes metrics from various Prometheus response formats.
    Provides robust extraction and categorization of metrics.
    """

    @classmethod
    def extract_value(cls, metric: Dict[str, Any]) -> Optional[float]:
        """
        Extract a numeric value from a metric in a consistent way.
        Handles all Prometheus response formats.

        Args:
            metric: The metric dictionary from which to extract the value

        Returns:
            The numeric value as float, or None if no valid value could be extracted
        """
        # Debug print if needed
        # logger.debug(f"Extracting value from metric: {metric}")

        value = None

        # Try different extraction methods in order of reliability
        extraction_methods = [
            cls._extract_direct_value,
            cls._extract_from_values_array,
            cls._extract_from_raw_value,
            cls._extract_from_sample_value
        ]

        for method in extraction_methods:
            try:
                value = method(metric)
                if value is not None:
                    break
            except Exception as e:
                # Just move to the next method on failure
                pass

        if value is None:
            # Debug this case - what does the metric actually contain?
            logger.debug(f"Failed to extract value from metric: {metric.get('metric', 'unknown')}")
            if 'value' in metric:
                logger.debug(f"Value field present but extraction failed. Type: {type(metric['value'])}, Content: {metric['value']}")
            if 'values' in metric:
                logger.debug(f"Values array present but extraction failed. Type: {type(metric['values'])}")
                if isinstance(metric['values'], list) and metric['values']:
                    logger.debug(f"First value in array: {metric['values'][0]}")

        return value

    @staticmethod
    def _extract_direct_value(metric: Dict[str, Any]) -> Optional[float]:
        """Extract value directly from the 'value' field."""
        if "value" not in metric or metric["value"] is None:
            return None

        raw_value = metric["value"]

        # Handle different value formats
        if isinstance(raw_value, (int, float)):
            return float(raw_value)
        elif isinstance(raw_value, (list, tuple)) and len(raw_value) > 1:
            # Handle Prometheus vector format [timestamp, value]
            value_part = raw_value[1]
            if isinstance(value_part, (int, float, str)):
                return float(value_part)
        elif isinstance(raw_value, str):
            # Try to convert string to float
            return float(raw_value)

        return None

    @staticmethod
    def _extract_from_values_array(metric: Dict[str, Any]) -> Optional[float]:
        """Extract value from the 'values' array."""
        if "values" not in metric or not metric["values"] or not isinstance(metric["values"], list):
            return None

        values_array = metric["values"]

        # Try the most recent value first (usually last in array)
        if values_array:
            # Most recent value should be the last one
            last_value = values_array[-1]

            # Handle different value formats
            if isinstance(last_value, (int, float)):
                return float(last_value)
            elif isinstance(last_value, (list, tuple)) and len(last_value) > 1:
                value_part = last_value[1]
                if isinstance(value_part, (int, float, str)):
                    return float(value_part)
            elif isinstance(last_value, str):
                try:
                    return float(last_value)
                except ValueError:
                    pass

            # If last value didn't work, try each value in the array from newest to oldest
            for val in reversed(values_array):
                if isinstance(val, (int, float)):
                    return float(val)
                elif isinstance(val, (list, tuple)) and len(val) > 1:
                    try:
                        value_part = val[1]
                        if isinstance(value_part, (int, float, str)):
                            return float(value_part)
                    except (ValueError, TypeError, IndexError):
                        continue
                elif isinstance(val, str):
                    try:
                        return float(val)
                    except ValueError:
                        continue

        return None

    @staticmethod
    def _extract_from_raw_value(metric: Dict[str, Any]) -> Optional[float]:
        """Extract from raw_value field."""
        if "raw_value" in metric and metric["raw_value"] is not None:
            try:
                return float(metric["raw_value"])
            except (ValueError, TypeError):
                pass
        return None

    @staticmethod
    def _extract_from_sample_value(metric: Dict[str, Any]) -> Optional[float]:
        """Extract from sample_value field."""
        if "sample_value" in metric and metric["sample_value"] is not None:
            try:
                return float(metric["sample_value"])
            except (ValueError, TypeError):
                pass
        return None

    @classmethod
    def categorize_metric(cls, metric: Dict[str, Any]) -> Tuple[str, Optional[float]]:
        """
        Categorize a metric by type (CPU, memory, network, etc.) and extract its value.

        Args:
            metric: The metric dictionary

        Returns:
            Tuple of (category, value)
        """
        # Extract value first - so we know if we have a valid metric
        value = cls.extract_value(metric)
        if value is None:
            return "unknown", None

        # Get metric type and name
        metric_type = metric.get("metric_type", "").lower()
        metric_name = metric.get("metric", "").lower()

        # Try to categorize by metric_type first (most reliable)
        if metric_type:
            if "cpu" in metric_type:
                return "cpu", cls.normalize_cpu_value(value)
            elif "memory" in metric_type:
                return "memory", cls.normalize_memory_value(value)
            elif "network_receive" in metric_type:
                return "network_receive", value
            elif "network_transmit" in metric_type:
                return "network_transmit", value

        # Try to categorize by metric name as fallback
        if metric_name:
            if any(term in metric_name for term in ["cpu", "cores"]):
                return "cpu", cls.normalize_cpu_value(value)
            elif any(term in metric_name for term in ["memory", "mem", "bytes", "ram"]):
                return "memory", cls.normalize_memory_value(value)
            elif any(term in metric_name for term in ["network_receive", "rx", "receive"]):
                return "network_receive", value
            elif any(term in metric_name for term in ["network_transmit", "tx", "transmit"]):
                return "network_transmit", value

        # If we get here, we couldn't categorize
        logger.debug(f"Could not categorize metric: {metric_type or metric_name}")
        return "unknown", value

    @staticmethod
    def normalize_cpu_value(value: float) -> float:
        """
        Normalize CPU values to a consistent unit (cores).

        Args:
            value: Raw CPU value

        Returns:
            Normalized CPU value in cores
        """
        if value > 0:
            # If value is in millicores (e.g., 500m = 0.5 cores)
            if value > 100:
                return value / 1000.0
            # Otherwise assume cores already
            return value
        return 0.0

    @staticmethod
    def normalize_memory_value(value: float) -> float:
        """
        Normalize memory values to a consistent unit (bytes).

        Args:
            value: Raw memory value

        Returns:
            Normalized memory value in bytes
        """
        if value > 0:
            # If value is very small, it might be a percentage (0-1)
            if value < 1.0:
                # Assume percentage of 8GB (arbitrary reference point)
                return value * 8 * 1024 * 1024 * 1024
            # If value is small but not tiny, might be MB
            elif value < 1000:
                # Assume MB
                return value * 1024 * 1024
            # Otherwise assume bytes already
            return value
        return 0.0

    @staticmethod
    def format_cpu(cpu_value: float) -> str:
        """
        Format CPU usage in a human-readable way.

        Args:
            cpu_value: CPU usage value in cores

        Returns:
            Formatted string
        """
        if cpu_value == 0:
            return "0.0000 cores"
        elif cpu_value < 0.01:
            return f"{cpu_value*1000:.2f} millicores"
        elif cpu_value < 1:
            return f"{cpu_value:.4f} cores"
        else:
            return f"{cpu_value:.2f} cores"

    @staticmethod
    def format_bytes(bytes_value: float) -> str:
        """
        Format bytes in a human-readable way (B, KB, MB, GB).

        Args:
            bytes_value: Value in bytes

        Returns:
            Formatted string
        """
        if bytes_value == 0:
            return "0.00 B"
        elif bytes_value < 1024:
            return f"{bytes_value:.2f} B"
        elif bytes_value < 1024**2:
            return f"{bytes_value/1024:.2f} KB"
        elif bytes_value < 1024**3:
            return f"{bytes_value/1024**2:.2f} MB"
        else:
            return f"{bytes_value/1024**3:.2f} GB"

    @classmethod
    def process_metrics_collection(cls, all_metrics: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Process a collection of metrics to extract and categorize all values.
        Particularly useful for cluster overviews.

        Args:
            all_metrics: Dictionary of entity metrics from fetch_all_metrics

        Returns:
            Dictionary with processed metrics by namespace and category
        """
        result = {
            "entities": {
                "total": len(all_metrics),
                "by_type": {}
            },
            "namespaces": {},
            "network": {
                "receive": 0.0,
                "transmit": 0.0
            },
            "total_metrics": 0,
            "metrics_per_entity": 0.0
        }

        # Process each entity
        for entity_key, entity_data in all_metrics.items():
            # Count by entity type
            entity_type = entity_data.get("entity_type", "unknown")
            if entity_type not in result["entities"]["by_type"]:
                result["entities"]["by_type"][entity_type] = 0
            result["entities"]["by_type"][entity_type] += 1

            # Track namespace
            namespace = entity_data.get("namespace")
            if namespace:
                if namespace not in result["namespaces"]:
                    result["namespaces"][namespace] = {
                        "cpu": 0.0,
                        "memory": 0.0,
                        "entities": 0
                    }
                result["namespaces"][namespace]["entities"] += 1

            # Count and process metrics
            entity_metrics = entity_data.get("metrics", [])
            result["total_metrics"] += len(entity_metrics)

            # Process each metric
            for metric in entity_metrics:
                category, value = cls.categorize_metric(metric)

                if value is not None:
                    if category == "cpu" and namespace:
                        result["namespaces"][namespace]["cpu"] += value
                    elif category == "memory" and namespace:
                        result["namespaces"][namespace]["memory"] += value
                    elif category == "network_receive":
                        result["network"]["receive"] += value
                    elif category == "network_transmit":
                        result["network"]["transmit"] += value

        # Calculate metrics per entity
        if result["entities"]["total"] > 0:
            result["metrics_per_entity"] = result["total_metrics"] / result["entities"]["total"]

        return result
