import asyncio
import functools
import random
import time
from typing import Any, Callable, Coroutine, Dict, Optional, TypeVar, cast

from loguru import logger

# Type Definitions
T = TypeVar('T')
F = TypeVar('F', bound=Callable[..., Coroutine[Any, Any, Any]])

# Default retry configuration
DEFAULT_MAX_RETRY_ATTEMPTS = 3
DEFAULT_INITIAL_RETRY_DELAY = 1.0  # seconds
DEFAULT_MAX_RETRY_DELAY = 30.0  # seconds
DEFAULT_RETRY_BACKOFF_FACTOR = 2.0
DEFAULT_JITTER_FACTOR = 0.1  # 10% jitter

# Circuit breaker configuration
CIRCUIT_OPEN_TIMEOUT = 60.0  # seconds to keep circuit open before trying half-open state
ERROR_THRESHOLD = 5  # number of errors before opening circuit
HALF_OPEN_MAX_CALLS = 3  # max calls to allow in half-open state

# Global circuit breaker state
circuit_breakers: Dict[str, Dict[str, Any]] = {}

# Circuit breaker status values for metrics
CB_STATUS_CLOSED = 0
CB_STATUS_HALF_OPEN = 1
CB_STATUS_OPEN = 2

def with_exponential_backoff(
    max_retries: Optional[int] = 3,
    initial_delay: float = DEFAULT_INITIAL_RETRY_DELAY,
    max_delay: float = DEFAULT_MAX_RETRY_DELAY,
    backoff_factor: float = DEFAULT_RETRY_BACKOFF_FACTOR,
    jitter_factor: float = DEFAULT_JITTER_FACTOR,
):
    """
    Decorator that applies exponential backoff retry logic to an async function.
    
    Args:
        max_retries: Maximum number of retry attempts (None for infinite retries)
        initial_delay: Initial backoff delay in seconds
        max_delay: Maximum backoff delay in seconds
        backoff_factor: Multiplier for each retry attempt
        jitter_factor: Random jitter percentage to add to delay
    
    Returns:
        Decorator function
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay
            attempt = 0
            
            func_name = func.__qualname__
            
            while True:
                attempt += 1
                
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if max_retries is not None and attempt >= max_retries:
                        logger.error(f"Function {func_name} failed after {attempt} attempts. Last error: {repr(e)}")
                        raise
                    
                    logger.warning(
                        f"Function {func_name} failed on attempt {attempt}"
                        f"{f'/{max_retries}' if max_retries else ''}: {repr(e)}"
                    )
                    
                    jitter = delay * jitter_factor
                    actual_delay = delay + random.uniform(-jitter, jitter)
                    actual_delay = max(0.1, actual_delay)  # Ensure minimum delay
                    
                    logger.info(f"Retrying {func_name} in {actual_delay:.2f}s (attempt {attempt + 1}"
                               f"{f'/{max_retries}' if max_retries else ''})")
                    
                    await asyncio.sleep(actual_delay)
                    
                    delay = min(delay * backoff_factor, max_delay)
        
        return cast(F, wrapper)
    
    return decorator

def with_circuit_breaker(
    service_name: str,
    error_threshold: int = ERROR_THRESHOLD,
    reset_timeout: float = CIRCUIT_OPEN_TIMEOUT,
):
    """
    Circuit breaker pattern implementation for async functions.
    
    Args:
        service_name: Identifier for the service to create circuit for
        error_threshold: Number of errors before opening circuit
        reset_timeout: Time in seconds to keep circuit open before half-open
        
    Returns:
        Decorator function
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            func_name = func.__qualname__
            circuit_key = f"{service_name}:{func_name}"
            
            # Initialize circuit state if not exists
            if circuit_key not in circuit_breakers:
                circuit_breakers[circuit_key] = {
                    "state": "CLOSED",  # CLOSED, OPEN, HALF_OPEN
                    "error_count": 0,
                    "last_error_time": 0,
                    "total_calls": 0,
                    "successful_calls": 0,
                    "half_open_calls": 0,
                }
            
            circuit = circuit_breakers[circuit_key]
            now = time.time()
            
            # Check circuit state
            if circuit["state"] == "OPEN":
                # Check if timeout expired to transition to HALF_OPEN
                if now - circuit["last_error_time"] > reset_timeout:
                    logger.info(f"Circuit {circuit_key} transitioning from OPEN to HALF_OPEN after timeout")
                    circuit["state"] = "HALF_OPEN"
                    circuit["half_open_calls"] = 0
                else:
                    # Circuit is still open
                    remaining_time = reset_timeout - (now - circuit["last_error_time"])
                    logger.warning(f"Circuit {circuit_key} is OPEN, fast-failing call to {func_name}. Will attempt recovery in {remaining_time:.1f}s")
                    raise RuntimeError(f"Circuit breaker open for {service_name}. Will retry after {remaining_time:.1f}s")
            
            if circuit["state"] == "HALF_OPEN" and circuit["half_open_calls"] >= HALF_OPEN_MAX_CALLS:
                logger.warning(f"Circuit {circuit_key} in HALF_OPEN has reached max test calls, fast-failing")
                raise RuntimeError(f"Circuit breaker in half-open state for {service_name} has reached test call limit")
            
            # Allow the call to proceed
            circuit["total_calls"] += 1
            if circuit["state"] == "HALF_OPEN":
                circuit["half_open_calls"] += 1
                logger.info(f"Testing circuit {circuit_key} with probe call {circuit['half_open_calls']}/{HALF_OPEN_MAX_CALLS}")
            
            try:
                result = await func(*args, **kwargs)
                
                # Call succeeded
                circuit["successful_calls"] += 1
                
                # If we were in HALF_OPEN and call succeeded, close the circuit
                if circuit["state"] == "HALF_OPEN":
                    logger.info(f"Circuit {circuit_key} test call succeeded, transitioning from HALF_OPEN to CLOSED")
                    circuit["state"] = "CLOSED"
                    circuit["error_count"] = 0
                    circuit["half_open_calls"] = 0
                    
                    # Update circuit breaker metrics if available
                    try:
                        from kubewise.api.context import SERVICE_CIRCUIT_BREAKER_STATUS
                        SERVICE_CIRCUIT_BREAKER_STATUS.labels(
                            service=service_name, 
                            circuit_key=circuit_key
                        ).set(CB_STATUS_CLOSED)
                    except (ImportError, NameError):
                        pass
                
                # Reset error count on success in CLOSED state
                if circuit["state"] == "CLOSED" and circuit["error_count"] > 0:
                    circuit["error_count"] = max(0, circuit["error_count"] - 1)  # Slowly decrease error count
                
                return result
                
            except Exception as e:
                # Call failed
                circuit["error_count"] += 1
                circuit["last_error_time"] = now
                
                # If in HALF_OPEN and call failed, reopen the circuit
                if circuit["state"] == "HALF_OPEN":
                    logger.warning(f"Circuit {circuit_key} test call failed: {repr(e)}, reopening circuit")
                    circuit["state"] = "OPEN"
                    circuit["half_open_calls"] = 0
                
                # If in CLOSED but error threshold reached, open the circuit
                elif circuit["state"] == "CLOSED" and circuit["error_count"] >= error_threshold:
                    logger.warning(f"Circuit {circuit_key} reached error threshold ({circuit['error_count']}), opening circuit. Last error: {repr(e)}")
                    circuit["state"] = "OPEN"
                    
                    # Update circuit breaker metrics if available
                    try:
                        from kubewise.api.context import SERVICE_CIRCUIT_BREAKER_STATUS, SERVICE_UP
                        SERVICE_CIRCUIT_BREAKER_STATUS.labels(
                            service=service_name, 
                            circuit_key=circuit_key
                        ).set(CB_STATUS_OPEN)
                        
                        # Also mark the service as down
                        SERVICE_UP.labels(service=service_name).set(0)
                    except (ImportError, NameError):
                        pass
                
                # Re-raise the original exception
                raise
        
        return cast(F, wrapper)
    
    return decorator
