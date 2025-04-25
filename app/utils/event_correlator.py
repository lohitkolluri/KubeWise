import uuid
from typing import Dict, Any, Optional
from kubernetes import client
from app.core import logger
from app.core.exceptions import KubernetesConnectionError

class K8sEventCorrelator:
    """
    Correlates application logs with Kubernetes events using request IDs.
    This allows tracing operations across system boundaries between the application
    and Kubernetes for better diagnostics and observability.
    """
    
    def __init__(self):
        """Initialize the Kubernetes event correlator."""
        try:
            self.core_v1_api = client.CoreV1Api()
            self.events_api = client.EventsV1Api()
            self.custom_api = client.CustomObjectsApi()
            self._use_events_v1 = self._check_events_v1_available()
        except Exception as e:
            logger.warning(
                "Failed to initialize K8s event correlator", 
                exception=str(e),
                component="event_correlator"
            )
            self.core_v1_api = None
            self.events_api = None
            self.custom_api = None
            self._use_events_v1 = False
    
    def _check_events_v1_available(self) -> bool:
        """Check if Events V1 API is available in the cluster."""
        try:
            self.events_api.get_api_resources()
            return True
        except Exception:
            logger.debug("Events V1 API not available, falling back to Core V1 Events", component="event_correlator")
            return False
    
    def generate_correlation_id(self) -> str:
        """Generate a unique correlation ID for tracking operations."""
        return f"kw-{uuid.uuid4().hex[:16]}"
    
    def record_event(
        self,
        name: str,
        namespace: str,
        resource_type: str,
        action: str, 
        correlation_id: str,
        reason: str,
        message: str,
        severity: str = "Normal",
        additional_data: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Record a Kubernetes event with correlation ID linking to application logs.
        
        Args:
            name: Name of the resource (deployment, pod, etc.)
            namespace: Kubernetes namespace
            resource_type: Type of resource (Pod, Deployment, etc.)
            action: Action being performed (e.g., "Restarted", "Scaled")
            correlation_id: Unique ID to correlate with application logs
            reason: Short, CamelCase reason for the event
            message: Detailed message explaining the event
            severity: "Normal" or "Warning"
            additional_data: Any additional data to include
            
        Returns:
            bool: True if event was recorded successfully, False otherwise
        """
        if not self.core_v1_api:
            logger.warning("Cannot record K8s event - event correlator not initialized", component="event_correlator")
            return False
            
        try:
            # Add correlation ID to message for easier searching
            full_message = f"{message} [Correlation ID: {correlation_id}]"
            
            # Create annotations with correlation data
            annotations = {
                "kubewise.io/correlation-id": correlation_id,
                "kubewise.io/action": action
            }
            
            # Add any additional data as annotations
            if additional_data:
                for key, value in additional_data.items():
                    annotations[f"kubewise.io/{key}"] = str(value)
            
            if self._use_events_v1:
                # Use Events V1 API (Kubernetes 1.19+)
                event = client.EventsV1Event(
                    metadata=client.V1ObjectMeta(
                        generate_name=f"kubewise-{action.lower()}-",
                        namespace=namespace,
                        annotations=annotations
                    ),
                    regarding=client.V1ObjectReference(
                        kind=resource_type,
                        namespace=namespace,
                        name=name
                    ),
                    reason=reason,
                    note=full_message,
                    type=severity,
                    reporting_instance="kubewise-controller",
                    reporting_controller="KubeWise"
                )
                self.events_api.create_namespaced_event(namespace=namespace, body=event)
            else:
                # Fallback to Core V1 Events
                event = client.V1Event(
                    metadata=client.V1ObjectMeta(
                        generate_name=f"kubewise-{action.lower()}-",
                        namespace=namespace,
                        annotations=annotations
                    ),
                    involved_object=client.V1ObjectReference(
                        kind=resource_type,
                        namespace=namespace,
                        name=name
                    ),
                    reason=reason,
                    message=full_message,
                    type=severity,
                    source=client.V1EventSource(component="kubewise-controller")
                )
                self.core_v1_api.create_namespaced_event(namespace=namespace, body=event)
                
            logger.info(
                f"Recorded K8s event for {resource_type}/{name}",
                correlation_id=correlation_id,
                action=action,
                component="event_correlator",
                namespace=namespace
            )
            return True
            
        except Exception as e:
            logger.error(
                f"Failed to record K8s event",
                exception=str(e),
                correlation_id=correlation_id,
                component="event_correlator",
                namespace=namespace
            )
            return False
    
    def find_correlated_events(self, correlation_id: str) -> list:
        """
        Find all Kubernetes events with a specific correlation ID.
        
        Args:
            correlation_id: The correlation ID to search for
            
        Returns:
            list: List of events with matching correlation ID
        """
        if not self.core_v1_api:
            raise KubernetesConnectionError("Event correlator not initialized")
            
        events = []
        try:
            # Get all events across all namespaces
            all_events = self.core_v1_api.list_event_for_all_namespaces()
            
            # Filter events with matching correlation ID
            for event in all_events.items:
                annotations = event.metadata.annotations or {}
                if annotations.get("kubewise.io/correlation-id") == correlation_id:
                    events.append({
                        "name": event.metadata.name,
                        "namespace": event.metadata.namespace,
                        "resource_type": event.involved_object.kind,
                        "resource_name": event.involved_object.name,
                        "reason": event.reason,
                        "message": event.message,
                        "timestamp": event.last_timestamp,
                        "count": event.count,
                        "type": event.type
                    })
        except Exception as e:
            logger.error(
                f"Failed to find correlated events",
                exception=str(e),
                correlation_id=correlation_id,
                component="event_correlator"
            )
        
        return events