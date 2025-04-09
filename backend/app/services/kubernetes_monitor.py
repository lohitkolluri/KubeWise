import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import json

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from loguru import logger

from app.core.config import settings
from app.services.redis_cache import redis_service


# Custom JSON encoder to handle datetime serialization
class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class KubernetesMonitor:
    """Service for monitoring Kubernetes cluster state."""

    def __init__(self):
        """Initialize Kubernetes client."""
        self.core_v1_api = None
        self.apps_v1_api = None
        self.custom_objects_api = None
        self.initialized = False
        self.monitor_task = None
        self.monitoring_active = False
        self.last_collection_time = None

    def initialize(self):
        """Initialize the Kubernetes client with kubeconfig."""
        try:
            import os
            logger.info(f"Loading kubeconfig from: {settings.KUBECONFIG_PATH}")

            # Check if file exists
            if not os.path.exists(settings.KUBECONFIG_PATH):
                logger.warning(f"Kubeconfig file does not exist at {settings.KUBECONFIG_PATH}")
                self.initialized = False
                logger.info("Running in development mode without Kubernetes connectivity")
                return

            config.load_kube_config(config_file=settings.KUBECONFIG_PATH)
            self.core_v1_api = client.CoreV1Api()
            self.apps_v1_api = client.AppsV1Api()
            self.custom_objects_api = client.CustomObjectsApi()
            self.initialized = True
            logger.info("Kubernetes client initialized successfully")
        except Exception as e:
            self.initialized = False
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            logger.info("Running in development mode without Kubernetes connectivity")

    def get_cluster_name(self) -> str:
        """
        Get the name of the current Kubernetes cluster from kubeconfig.

        Returns:
            The name of the current cluster, or 'unknown' if not available
        """
        try:
            contexts, active_context = config.list_kube_config_contexts()
            if active_context:
                return active_context['context'].get('cluster', 'unknown')
            return 'unknown'
        except Exception as e:
            logger.error(f"Failed to get cluster name: {e}")
            return 'unknown'

    async def start_monitoring(self):
        """Start the background monitoring task."""
        if not self.initialized:
            logger.warning("Kubernetes client not initialized, initializing now...")
            self.initialize()

        if self.monitoring_active:
            logger.warning("Monitoring already active")
            return

        self.monitoring_active = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Kubernetes monitoring started")

    async def stop_monitoring(self):
        """Stop the background monitoring task."""
        if not self.monitoring_active:
            logger.warning("Monitoring not active")
            return

        self.monitoring_active = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None
        logger.info("Kubernetes monitoring stopped")

    async def _monitor_loop(self):
        """Main monitoring loop that runs periodically."""
        while self.monitoring_active:
            try:
                # Get current cluster state
                await self.collect_cluster_state()
                self.last_collection_time = datetime.utcnow()

                # Wait for the next interval
                await asyncio.sleep(settings.MONITOR_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                logger.info("Monitoring loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(5)  # Short delay before retry on error

    async def collect_cluster_state(self) -> Dict[str, Any]:
        """
        Collect the current state of the Kubernetes cluster.

        Returns:
            Dict containing the current cluster state
        """
        if not self.initialized:
            logger.warning("Kubernetes client not initialized, initializing now...")
            self.initialize()

        logger.info("Collecting cluster state...")

        try:
            # Get nodes
            nodes = await self._get_nodes()

            # Get pods
            pods = await self._get_pods()

            # Get deployments
            deployments = await self._get_deployments()

            # Get services
            services = await self._get_services()

            # Get events
            events = await self._get_events()

            # Get metrics if available
            metrics = await self._get_metrics()

            # Assemble cluster state
            cluster_state = {
                "timestamp": datetime.utcnow().isoformat(),
                "nodes": nodes,
                "pods": pods,
                "deployments": deployments,
                "services": services,
                "events": events,
                "metrics": metrics
            }

            # Cache in Redis
            await redis_service.set_value("k8s:cluster_state", cluster_state, ttl=settings.MONITOR_INTERVAL_SECONDS * 2)

            # Also cache individual collections
            await redis_service.cache_kubernetes_collection("nodes", nodes)
            await redis_service.cache_kubernetes_collection("pods", pods)
            await redis_service.cache_kubernetes_collection("deployments", deployments)
            await redis_service.cache_kubernetes_collection("services", services)
            await redis_service.cache_kubernetes_collection("events", events)

            logger.info("Cluster state collected and cached")
            return cluster_state

        except Exception as e:
            logger.error(f"Error collecting cluster state: {e}")
            return {}

    async def _get_nodes(self) -> List[Dict[str, Any]]:
        """
        Get all nodes in the cluster.

        Returns:
            List of node data
        """
        try:
            nodes = await asyncio.to_thread(self.core_v1_api.list_node)
            return self._k8s_object_to_dict(nodes.items)
        except ApiException as e:
            logger.error(f"Error fetching nodes: {e}")
            return []

    async def _get_pods(self) -> List[Dict[str, Any]]:
        """
        Get all pods in the monitored namespaces.

        Returns:
            List of pod data
        """
        try:
            if "*" in settings.MONITORED_NAMESPACES:
                # Get pods from all namespaces
                pods = await asyncio.to_thread(self.core_v1_api.list_pod_for_all_namespaces)
                return self._k8s_object_to_dict(pods.items)
            else:
                # Get pods from specific namespaces
                all_pods = []
                for namespace in settings.MONITORED_NAMESPACES:
                    pods = await asyncio.to_thread(self.core_v1_api.list_namespaced_pod, namespace)
                    all_pods.extend(pods.items)
                return self._k8s_object_to_dict(all_pods)
        except ApiException as e:
            logger.error(f"Error fetching pods: {e}")
            return []

    async def _get_deployments(self) -> List[Dict[str, Any]]:
        """
        Get all deployments in the monitored namespaces.

        Returns:
            List of deployment data
        """
        try:
            if "*" in settings.MONITORED_NAMESPACES:
                # Get deployments from all namespaces
                deployments = await asyncio.to_thread(self.apps_v1_api.list_deployment_for_all_namespaces)
                return self._k8s_object_to_dict(deployments.items)
            else:
                # Get deployments from specific namespaces
                all_deployments = []
                for namespace in settings.MONITORED_NAMESPACES:
                    deployments = await asyncio.to_thread(self.apps_v1_api.list_namespaced_deployment, namespace)
                    all_deployments.extend(deployments.items)
                return self._k8s_object_to_dict(all_deployments)
        except ApiException as e:
            logger.error(f"Error fetching deployments: {e}")
            return []

    async def _get_services(self) -> List[Dict[str, Any]]:
        """
        Get all services in the monitored namespaces.

        Returns:
            List of service data
        """
        try:
            if "*" in settings.MONITORED_NAMESPACES:
                # Get services from all namespaces
                services = await asyncio.to_thread(self.core_v1_api.list_service_for_all_namespaces)

                # Get endpoints for each service
                endpoints = await asyncio.to_thread(self.core_v1_api.list_endpoints_for_all_namespaces)

                # Add endpoints info to services
                services_with_endpoints = []
                service_dict = {f"{s.metadata.namespace}/{s.metadata.name}": s for s in services.items}
                endpoints_dict = {f"{e.metadata.namespace}/{e.metadata.name}": e for e in endpoints.items}

                for key, service in service_dict.items():
                    service_data = service.to_dict()
                    if key in endpoints_dict:
                        service_data["endpoints"] = endpoints_dict[key].to_dict()
                    services_with_endpoints.append(service_data)

                return services_with_endpoints
            else:
                # Get services from specific namespaces
                all_services = []
                for namespace in settings.MONITORED_NAMESPACES:
                    services = await asyncio.to_thread(self.core_v1_api.list_namespaced_service, namespace)
                    endpoints = await asyncio.to_thread(self.core_v1_api.list_namespaced_endpoints, namespace)

                    # Create maps for easy lookup
                    service_dict = {s.metadata.name: s for s in services.items}
                    endpoints_dict = {e.metadata.name: e for e in endpoints.items}

                    for name, service in service_dict.items():
                        service_data = service.to_dict()
                        if name in endpoints_dict:
                            service_data["endpoints"] = endpoints_dict[name].to_dict()
                        all_services.append(service_data)

                return all_services
        except ApiException as e:
            logger.error(f"Error fetching services: {e}")
            return []

    async def _get_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get recent events in the monitored namespaces.

        Args:
            limit: Maximum number of events to return

        Returns:
            List of event data
        """
        try:
            if "*" in settings.MONITORED_NAMESPACES:
                # Get events from all namespaces
                events = await asyncio.to_thread(self.core_v1_api.list_event_for_all_namespaces, limit=limit)
                return self._k8s_object_to_dict(events.items)
            else:
                # Get events from specific namespaces
                all_events = []
                for namespace in settings.MONITORED_NAMESPACES:
                    events = await asyncio.to_thread(self.core_v1_api.list_namespaced_event, namespace, limit=limit)
                    all_events.extend(events.items)
                return self._k8s_object_to_dict(all_events[:limit])
        except ApiException as e:
            logger.error(f"Error fetching events: {e}")
            return []

    async def _get_metrics(self) -> Dict[str, Any]:
        """
        Try to get metrics from the metrics API if available.

        Returns:
            Dict with node and pod metrics, or empty dict if metrics API is not available
        """
        try:
            # Check for node metrics
            node_metrics = await asyncio.to_thread(
                self.custom_objects_api.list_cluster_custom_object,
                "metrics.k8s.io",
                "v1beta1",
                "nodes"
            )

            # Check for pod metrics
            pod_metrics = await asyncio.to_thread(
                self.custom_objects_api.list_cluster_custom_object,
                "metrics.k8s.io",
                "v1beta1",
                "pods"
            )

            return {
                "nodes": node_metrics.get("items", []),
                "pods": pod_metrics.get("items", [])
            }
        except ApiException as e:
            if e.status == 404:
                logger.warning("Metrics API not available in the cluster")
            else:
                logger.error(f"Error fetching metrics: {e}")
            return {}

    def _k8s_object_to_dict(self, k8s_objects: List[Any]) -> List[Dict[str, Any]]:
        """
        Convert Kubernetes objects to dictionaries.

        Args:
            k8s_objects: List of Kubernetes objects

        Returns:
            List of dictionaries
        """
        return [obj.to_dict() for obj in k8s_objects]

    async def get_cluster_summary(self) -> Tuple[int, int, int, int, int, int, int]:
        """
        Get a summary of the cluster state (counts of resources).

        Returns:
            Tuple of (nodes_total, nodes_ready, pods_total, pods_running,
                    deployments_total, deployments_ready, services_total)
        """
        try:
            # Try to get from Redis first
            nodes = await redis_service.get_kubernetes_collection("nodes")
            pods = await redis_service.get_kubernetes_collection("pods")
            deployments = await redis_service.get_kubernetes_collection("deployments")
            services = await redis_service.get_kubernetes_collection("services")

            # If not in Redis, collect fresh data
            if not nodes or not pods or not deployments or not services:
                await self.collect_cluster_state()
                nodes = await redis_service.get_kubernetes_collection("nodes")
                pods = await redis_service.get_kubernetes_collection("pods")
                deployments = await redis_service.get_kubernetes_collection("deployments")
                services = await redis_service.get_kubernetes_collection("services")

            # Count ready nodes
            nodes_ready = 0
            for node in nodes:
                for condition in node.get("status", {}).get("conditions", []):
                    if condition.get("type") == "Ready" and condition.get("status") == "True":
                        nodes_ready += 1
                        break

            # Count running pods
            pods_running = sum(1 for pod in pods if pod.get("status", {}).get("phase") == "Running")

            # Count ready deployments
            deployments_ready = 0
            for deployment in deployments:
                if deployment.get("status", {}).get("readyReplicas", 0) == deployment.get("status", {}).get("replicas", 0):
                    deployments_ready += 1

            return (
                len(nodes),
                nodes_ready,
                len(pods),
                pods_running,
                len(deployments),
                deployments_ready,
                len(services)
            )
        except Exception as e:
            logger.error(f"Error getting cluster summary: {e}")
            return (0, 0, 0, 0, 0, 0, 0)


# Create a global instance for use across the application
kubernetes_monitor = KubernetesMonitor()
