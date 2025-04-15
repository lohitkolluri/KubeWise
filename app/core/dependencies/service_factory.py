"""
Service factory module for dependency injection.

This module provides factory functions that can be used with FastAPI's
dependency injection system to get service instances.
"""
from app.services.anomaly_event_service import AnomalyEventService
from app.services.gemini_service import GeminiService

# Global instances that will be set by main.py
_gemini_service_instance = None
_anomaly_event_service_instance = None

def set_gemini_service(gemini_service: GeminiService):
    """Set the global Gemini service instance."""
    global _gemini_service_instance
    _gemini_service_instance = gemini_service

def set_anomaly_event_service(event_service: AnomalyEventService):
    """Set the global anomaly event service instance."""
    global _anomaly_event_service_instance
    _anomaly_event_service_instance = event_service

def get_gemini_service():
    """
    Factory function to provide Gemini service instance for dependency injection.

    Returns:
        The global Gemini service instance.
    """
    return _gemini_service_instance

def get_anomaly_event_service():
    """
    Factory function to provide AnomalyEventService instance for dependency injection.

    Returns:
        The global AnomalyEventService instance.
    """
    global _anomaly_event_service_instance

    if _anomaly_event_service_instance is None:
        # Create a new instance if none exists
        _anomaly_event_service_instance = AnomalyEventService()

    return _anomaly_event_service_instance
