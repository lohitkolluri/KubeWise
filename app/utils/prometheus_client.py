from prometheus_client import Counter, Gauge, Histogram, start_http_server
from loguru import logger
from typing import Dict, Any
import time

class PrometheusClient:
    """
    Client for exposing application metrics via Prometheus.

    This class creates and manages Prometheus metrics for the application,
    exposing them via an HTTP server for scraping by Prometheus.
    """

    def __init__(self, port: int = 9091):
        """
        Initialize Prometheus metrics and start the metrics server.

        Args:
            port: Port number for the Prometheus HTTP server
        """
        self.port = port
        self.metrics = {}
        self.server = None
        self.start_server()

        # Define metrics
        self.api_requests = Counter(
            'api_requests_total',
            'Total number of API requests',
            ['endpoint', 'method', 'status']
        )

        self.node_metrics = Gauge(
            'node_metrics',
            'Current node metrics',
            ['node', 'metric_type']
        )

        self.request_duration = Histogram(
            'request_duration_seconds',
            'Request duration in seconds',
            ['endpoint']
        )

        self.anomaly_score = Gauge(
            'anomaly_score',
            'Anomaly detection score',
            ['node']
        )

    def start_server(self):
        """Start the Prometheus metrics server on configured port."""
        try:
            self.server = start_http_server(self.port)
            logger.info(f"Prometheus metrics server started on port {self.port}")
        except Exception as e:
            logger.error(f"Error starting Prometheus server: {str(e)}")
            raise

    def record_request(self, endpoint: str, method: str, status: int):
        """
        Record an API request metric.

        Args:
            endpoint: API endpoint path
            method: HTTP method (GET, POST, etc.)
            status: HTTP status code
        """
        self.api_requests.labels(endpoint=endpoint, method=method, status=status).inc()

    def update_node_metrics(self, node: str, metrics: dict):
        """
        Update metrics for a specific node.

        Args:
            node: Node identifier
            metrics: Dictionary of metrics to update
        """
        try:
            if node not in self.metrics:
                self.metrics[node] = {}

            for metric_name, value in metrics.items():
                if metric_name not in self.metrics[node]:
                    self.metrics[node][metric_name] = Gauge(
                        f'node_{metric_name}',
                        f'Node {metric_name} metric',
                        ['node']
                    )
                self.metrics[node][metric_name].labels(node=node).set(value)
        except Exception as e:
            logger.error(f"Error updating node metrics: {str(e)}")
            raise

    def record_request_duration(self, endpoint: str, duration: float):
        """
        Record request duration in seconds.

        Args:
            endpoint: API endpoint path
            duration: Request duration in seconds
        """
        self.request_duration.labels(endpoint=endpoint).observe(duration)

    def update_anomaly_score(self, node: str, score: float):
        """
        Update anomaly detection score for a node.

        Args:
            node: Node identifier
            score: Anomaly detection score
        """
        self.anomaly_score.labels(node=node).set(score)

    async def get_metrics(self):
        """
        Get all current metrics.

        Returns:
            Dictionary containing all current metric values grouped by node
        """
        try:
            metrics_data = {}
            for node, node_metrics in self.metrics.items():
                metrics_data[node] = {
                    metric_name: metric.labels(node=node)._value.get()
                    for metric_name, metric in node_metrics.items()
                }
            return metrics_data
        except Exception as e:
            logger.error(f"Error getting metrics: {str(e)}")
            raise

# Create a global Prometheus client instance
prometheus = PrometheusClient()
