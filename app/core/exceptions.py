"""
Exception classes for KubeWise application.

This module contains custom exceptions for different types of errors that can occur
in the KubeWise application. These exceptions are used to standardize error handling
and improve debuggability.
"""

from typing import Any, Dict, Optional, List


class KubeWiseException(Exception):
    """Base exception class for all KubeWise exceptions."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)
        
    def __str__(self) -> str:
        if self.details:
            return f"{self.message} - Details: {self.details}"
        return self.message
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for API responses"""
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "details": self.details
        }


class ValidationError(KubeWiseException):
    """Raised when input validation fails."""
    pass


class DatabaseError(KubeWiseException):
    """Raised when a database operation fails."""
    pass


class KubernetesConnectionError(KubeWiseException):
    """Raised when connection to the Kubernetes API fails."""
    pass


class PrometheusConnectionError(KubeWiseException):
    """Raised when connection to the Prometheus API fails."""
    pass


class ResourceNotFoundError(KubeWiseException):
    """Raised when a Kubernetes resource is not found."""
    
    def __init__(self, resource_type: str, name: str, namespace: Optional[str] = None):
        details = {
            "resource_type": resource_type,
            "name": name
        }
        if namespace:
            details["namespace"] = namespace
            message = f"Resource not found: {resource_type}/{name} in namespace {namespace}"
        else:
            message = f"Resource not found: {resource_type}/{name}"
        
        super().__init__(message, details)


class OperationFailedError(KubeWiseException):
    """Raised when a Kubernetes operation fails."""
    
    def __init__(self, operation: str, reason: str, details: Optional[Dict[str, Any]] = None):
        message = f"Operation '{operation}' failed: {reason}"
        details_dict = details or {}
        details_dict["operation"] = operation
        details_dict["reason"] = reason
        super().__init__(message, details_dict)


class ConfigurationError(KubeWiseException):
    """Raised when there is an error in the configuration."""
    pass


class AuthenticationError(KubeWiseException):
    """Raised when authentication fails."""
    pass


class AuthorizationError(KubeWiseException):
    """Raised when authorization fails."""
    pass


class ServiceUnavailableError(KubeWiseException):
    """Raised when a required service is unavailable."""
    pass


class AnomalyDetectionError(KubeWiseException):
    """Raised when anomaly detection fails."""
    pass


class RemediationError(KubeWiseException):
    """Raised when remediation fails."""
    pass


class MetricsError(KubeWiseException):
    """Raised when getting or processing metrics fails."""
    pass


class AIServiceError(KubeWiseException):
    """Raised when AI service integration fails."""
    pass


class GeminiConnectionError(AIServiceError):
    """Raised when connection to the Gemini AI API fails."""
    pass


class GeminiResponseError(AIServiceError):
    """Raised when the Gemini AI API returns an invalid or unexpected response."""
    pass


# HTTP error mapping to help provide appropriate status codes
ERROR_TO_HTTP_STATUS = {
    ValidationError: 400,
    AuthenticationError: 401,
    AuthorizationError: 403,
    ResourceNotFoundError: 404,
    ServiceUnavailableError: 503,
    DatabaseError: 500,
    KubernetesConnectionError: 502,
    PrometheusConnectionError: 502,
    OperationFailedError: 500,
    ConfigurationError: 500,
    AnomalyDetectionError: 500,
    RemediationError: 500,
    MetricsError: 500,
    AIServiceError: 502,
    GeminiConnectionError: 502,
    GeminiResponseError: 500,
    KubeWiseException: 500
}