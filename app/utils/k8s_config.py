from kubernetes import client, config
from loguru import logger
import os
from typing import Optional, Dict, Any
from pydantic import BaseModel

class MonitoringConfig(BaseModel):
    prometheus_endpoint: str = "http://localhost:9090/api/v1/query"
    grafana_endpoint: str = "http://localhost:3000"
    prometheus_namespace: str = "monitoring"
    grafana_namespace: str = "monitoring"

class KubernetesConfig:
    def __init__(self, monitoring_config: Optional[MonitoringConfig] = None):
        try:
            # Load kubernetes configuration from default location
            config.load_kube_config()
            self.v1 = client.CoreV1Api()
            self.apps_v1 = client.AppsV1Api()
            self.monitoring_config = monitoring_config or MonitoringConfig()
            logger.info("Successfully initialized Kubernetes client")
        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            raise

    def get_metrics_endpoint(self) -> str:
        """
        Get the Prometheus metrics endpoint.
        """
        return self.monitoring_config.prometheus_endpoint

    def check_monitoring_status(self) -> Dict[str, bool]:
        """
        Check if monitoring tools are properly installed and running.
        """
        try:
            # Check prometheus pods
            prometheus_pods = self.v1.list_namespaced_pod(
                namespace=self.monitoring_config.prometheus_namespace,
                label_selector="app=prometheus"
            )

            # Check grafana pods
            grafana_pods = self.v1.list_namespaced_pod(
                namespace=self.monitoring_config.grafana_namespace,
                label_selector="app=grafana"
            )

            return {
                "prometheus_ready": any(pod.status.phase == "Running" for pod in prometheus_pods.items),
                "grafana_ready": any(pod.status.phase == "Running" for pod in grafana_pods.items)
            }
        except Exception as e:
            logger.error(f"Failed to check monitoring status: {e}")
            return {"prometheus_ready": False, "grafana_ready": False}
