"""
Circuit breaker implementation for KubeWise.

This module provides a circuit breaker pattern implementation to handle
external service failures gracefully and prevent cascading failures.
"""

import time
import functools
import asyncio
from enum import Enum
from typing import Callable, Any, TypeVar, Optional, Dict, List, Union, cast
from datetime import datetime, timedelta

from app.core import logger as app_logger
from app.core.exceptions import ServiceUnavailableError

# Type variable for function return type
T = TypeVar('T')

class CircuitState(Enum):
    """Possible states of the circuit breaker."""
    CLOSED = "closed"  # Normal operation, requests are allowed
    OPEN = "open"      # Failure threshold exceeded, requests are blocked
    HALF_OPEN = "half-open"  # Testing if service is back after timeout


class CircuitBreaker:
    """
    Circuit breaker implementation to handle external service failures.
    
    This class implements the circuit breaker pattern which prevents
    repeated calls to failing services and allows for graceful degradation.
    """
    
    # Class-level storage for circuit breakers by service name
    _instances: Dict[str, 'CircuitBreaker'] = {}
    
    @classmethod
    def get_instance(cls, service_name: str) -> 'CircuitBreaker':
        """
        Get or create a circuit breaker instance for the specified service.
        
        Args:
            service_name: Name of the service to create a circuit breaker for
            
        Returns:
            CircuitBreaker instance for the service
        """
        if service_name not in cls._instances:
            cls._instances[service_name] = CircuitBreaker(service_name)
        return cls._instances[service_name]
    
    @classmethod
    def get_status(cls) -> Dict[str, Dict[str, Any]]:
        """
        Get the status of all circuit breakers.
        
        Returns:
            Dictionary with status information for all circuit breakers
        """
        result = {}
        for name, instance in cls._instances.items():
            result[name] = {
                "state": instance.state.value,
                "failure_count": instance.failure_count,
                "last_failure_time": instance.last_failure_time.isoformat() if instance.last_failure_time else None,
                "last_success_time": instance.last_success_time.isoformat() if instance.last_success_time else None,
                "failure_threshold": instance.failure_threshold,
                "reset_timeout": instance.reset_timeout
            }
        return result
    
    def __init__(
        self, 
        service_name: str,
        failure_threshold: int = 5,
        reset_timeout: int = 60,
        half_open_max_calls: int = 1
    ):
        """
        Initialize a new circuit breaker.
        
        Args:
            service_name: Name of the service this circuit breaker protects
            failure_threshold: Number of failures before opening the circuit
            reset_timeout: Seconds to wait before trying again (half-open state)
            half_open_max_calls: Maximum number of test calls in half-open state
        """
        self.service_name = service_name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.half_open_max_calls = half_open_max_calls
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        self.half_open_calls = 0
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
    
    async def execute(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """
        Execute the function with circuit breaker protection.
        
        Args:
            func: The function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function
            
        Returns:
            Result of the function
            
        Raises:
            ServiceUnavailableError: If the circuit is open
        """
        await self._check_state()
        
        try:
            # Execute the function
            result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
            
            # Record success
            await self._on_success()
            return result
            
        except Exception as e:
            # Record failure
            await self._on_failure(e)
            raise
    
    async def _check_state(self) -> None:
        """
        Check if the request can proceed based on the current circuit state.
        
        Raises:
            ServiceUnavailableError: If the circuit is open
        """
        async with self._lock:
            now = datetime.now()
            
            if self.state == CircuitState.OPEN:
                # Check if reset timeout has elapsed
                if self.last_failure_time and now - self.last_failure_time > timedelta(seconds=self.reset_timeout):
                    # Transition to half-open state to test the service
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    app_logger.info(
                        f"Circuit breaker for {self.service_name} transitioning to HALF-OPEN state",
                        service=self.service_name,
                        circuit_state=self.state.value,
                        elapsed_seconds=(now - self.last_failure_time).total_seconds()
                    )
                else:
                    # Circuit is still open, reject the request
                    raise ServiceUnavailableError(
                        f"Service {self.service_name} is currently unavailable",
                        details={
                            "service": self.service_name,
                            "circuit_state": self.state.value,
                            "retry_after": self.reset_timeout - (now - cast(datetime, self.last_failure_time)).total_seconds() if self.last_failure_time else self.reset_timeout
                        }
                    )
            
            if self.state == CircuitState.HALF_OPEN:
                # Only allow limited test calls in half-open state
                if self.half_open_calls >= self.half_open_max_calls:
                    raise ServiceUnavailableError(
                        f"Service {self.service_name} is still in testing phase",
                        details={
                            "service": self.service_name,
                            "circuit_state": self.state.value,
                            "retry_after": self.reset_timeout
                        }
                    )
                self.half_open_calls += 1
    
    async def _on_success(self) -> None:
        """Record a successful call and update circuit state."""
        async with self._lock:
            now = datetime.now()
            self.last_success_time = now
            
            # If in half-open state and call succeeded, close the circuit
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                app_logger.info(
                    f"Circuit breaker for {self.service_name} closed after successful test",
                    service=self.service_name,
                    circuit_state=self.state.value
                )
            
            # Reset failure count in closed state on success
            if self.state == CircuitState.CLOSED:
                self.failure_count = 0
    
    async def _on_failure(self, exception: Exception) -> None:
        """
        Record a failed call and update circuit state.
        
        Args:
            exception: The exception that caused the failure
        """
        async with self._lock:
            now = datetime.now()
            self.last_failure_time = now
            self.failure_count += 1
            
            if self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
                # Open the circuit after reaching failure threshold
                self.state = CircuitState.OPEN
                app_logger.warning(
                    f"Circuit breaker for {self.service_name} opened due to repeated failures",
                    service=self.service_name,
                    circuit_state=self.state.value,
                    failure_count=self.failure_count,
                    last_exception=str(exception),
                    exception_type=exception.__class__.__name__
                )
            
            elif self.state == CircuitState.HALF_OPEN:
                # Test call failed, back to open state
                self.state = CircuitState.OPEN
                app_logger.warning(
                    f"Circuit breaker for {self.service_name} test failed, returning to OPEN state",
                    service=self.service_name,
                    circuit_state=self.state.value,
                    last_exception=str(exception),
                    exception_type=exception.__class__.__name__
                )


def circuit_breaker(service_name: str, failure_threshold: int = 5, reset_timeout: int = 60):
    """
    Decorator to apply circuit breaker pattern to a function.
    
    Args:
        service_name: Name of the service being protected
        failure_threshold: Number of failures before opening the circuit
        reset_timeout: Seconds to wait before trying again
    
    Returns:
        Decorated function with circuit breaker protection
    """
    def decorator(func):
        breaker = CircuitBreaker.get_instance(service_name)
        breaker.failure_threshold = failure_threshold
        breaker.reset_timeout = reset_timeout
        
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            return await breaker.execute(func, *args, **kwargs)
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            # For synchronous functions, run in an event loop
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(breaker.execute(func, *args, **kwargs))
        
        # Choose the appropriate wrapper based on whether the function is async or not
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator