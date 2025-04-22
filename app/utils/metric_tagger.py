"""
Metric Tagger module for KubeWise.
Provides functionality to tag and categorize metrics for better organization and analysis.
"""

from typing import Any, Dict, List

from loguru import logger


class MetricTagger:
    """
    A utility class that provides functionality to tag and categorize metrics.
    This helps with organizing metrics for better analysis and reporting.
    """

    COMMON_METRIC_TYPES = {
        "cpu": ["cpu_usage", "cpu_seconds", "cpu_percent"],
        "memory": ["memory_working_set", "memory_usage", "memory_bytes", "memory_percent"],
        "network": ["network_receive", "network_transmit", "network_bytes"],
        "disk": ["disk_usage", "disk_io", "fs_", "filesystem"],
        "pod_status": ["pod_status", "pod_phase", "pod_ready"],
        "container_restarts": ["container_restarts", "restart_count"]
    }

    @staticmethod
    def tag_metric(metric: Dict[str, Any]) -> Dict[str, Any]:
        """
        Tags a single metric with type information based on its name and query.

        Args:
            metric: A dictionary containing metric information

        Returns:
            The same metric dictionary with additional 'metric_type' field
        """
        if not metric:
            return metric

        # If already tagged, return as is
        if "metric_type" in metric and metric["metric_type"]:
            return metric

        metric_type = "unknown"

        # Extract metric name and query for analysis
        metric_name = metric.get("metric", "").lower()
        query = metric.get("query", "").lower()

        # Check if metric name or query contains known patterns
        for category, patterns in MetricTagger.COMMON_METRIC_TYPES.items():
            for pattern in patterns:
                if pattern in metric_name or pattern in query:
                    metric_type = pattern
                    break
            if metric_type != "unknown":
                break

        # Special case handling
        if "kube_pod_status_phase" in metric_name or "kube_pod_status_phase" in query:
            metric_type = "pod_status_phase"
        elif "pod" in metric.get("entity_type", "") and "cpu" in metric_name:
            metric_type = "cpu_usage_seconds_total"
        elif "pod" in metric.get("entity_type", "") and "memory" in metric_name:
            metric_type = "memory_working_set_bytes"

        # Add the type to the metric
        metric["metric_type"] = metric_type
        return metric

    @classmethod
    def tag_metrics_batch(cls, entities_dict: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Tags a batch of metrics for multiple entities.

        Args:
            entities_dict: Dictionary of entity data with metrics

        Returns:
            The same dictionary with tagged metrics
        """
        if not entities_dict:
            return {}

        for entity_key, entity_data in entities_dict.items():
            metrics = entity_data.get("metrics", [])
            for i, metric in enumerate(metrics):
                metrics[i] = cls.tag_metric(metric)

        return entities_dict

    @staticmethod
    def categorize_metrics(metrics_list: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """
        Organizes metrics into categories based on their type.

        Args:
            metrics_list: List of metrics to categorize

        Returns:
            Dictionary with metrics grouped by category
        """
        categories = {}

        for metric in metrics_list:
            # Ensure metric is tagged
            tagged_metric = MetricTagger.tag_metric(metric)
            metric_type = tagged_metric.get("metric_type", "unknown")

            # Map specific metric types to general categories
            category = "other"
            if any(t in metric_type for t in ["cpu"]):
                category = "cpu"
            elif any(t in metric_type for t in ["memory"]):
                category = "memory"
            elif any(t in metric_type for t in ["network"]):
                category = "network"
            elif any(t in metric_type for t in ["disk", "filesystem", "fs_"]):
                category = "disk"
            elif any(t in metric_type for t in ["pod_status", "phase"]):
                category = "state"
            elif any(t in metric_type for t in ["restart"]):
                category = "lifecycle"

            # Add to appropriate category
            if category not in categories:
                categories[category] = []
            categories[category].append(tagged_metric)

        return categories
