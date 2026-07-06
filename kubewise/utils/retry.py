# -*- coding: utf-8 -*-
import asyncio
import functools
import random
import time
from typing import Any, Callable, Coroutine, Dict, Optional, TypeVar, cast

from loguru import logger

from kubewise.config import settings # Import settings

# Type Definitions for generic decorators
T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])

DEFAULT_MAX_RETRY_ATTEMPTS = 3
DEFAULT_INITIAL_RETRY_DELAY = 1.0  # seconds
DEFAULT_MAX_RETRY_DELAY = 30.0  # seconds
DEFAULT_RETRY_BACKOFF_FACTOR = 2.0
DEFAULT_JITTER_FACTOR = 0.1  # 10% jitter

CIRCUIT_OPEN_TIMEOUT = 60.0  # seconds
ERROR_THRESHOLD = 5
HALF_OPEN_MAX_CALLS = 3

# Global storage for circuit breaker states, keyed by unique service/function identifiers.
circuit_breakers: Dict[str, Dict[str, Any]] = {}

# Constants representing circuit breaker states for metrics reporting.
CB_STATUS_CLOSED = 0
CB_STATUS_HALF_OPEN = 1
CB_STATUS_OPEN = 2


def with_exponential_backoff(
    max_retries_override: Optional[int] = None, # Renamed to avoid conflict
    initial_delay_override: Optional[float] = None, # Renamed to avoid conflict
    max_delay: float = DEFAULT_MAX_RETRY_DELAY,
    backoff_factor: float = DEFAULT_RETRY_BACKOFF_FACTOR,
    jitter_factor: float = DEFAULT_JITTER_FACTOR,
):
    """
    Decorator applying exponential backoff with jitter to an async function.

    Retries the function upon encountering exceptions, increasing the delay
    between attempts exponentially.

    Args:
        max_retries_override: Maximum retry attempts (None for infinite).
        initial_delay_override: Initial delay in seconds.
        max_delay: Maximum delay in seconds.
        backoff_factor: Multiplier for delay increase.
        jitter_factor: Percentage of delay to use for random jitter.

    Returns:
        The decorated async function.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Use override if provided, else use settings, else use decorator default
            current_max_retries = max_retries_override if max_retries_override is not None else getattr(settings, 'ai_max_retries', DEFAULT_MAX_RETRY_ATTEMPTS)
            current_initial_delay = initial_delay_override if initial_delay_override is not None else getattr(settings, 'ai_retry_delay', DEFAULT_INITIAL_RETRY_DELAY)
            
            delay = current_initial_delay
            attempt = 0
            func_name = func.__qualname__

            while True:
                attempt += 1
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if current_max_retries is not None and attempt >= current_max_retries:
                        logger.error(
                            f"Function {func_name} failed after {attempt} attempts. Last error: {repr(e)}"
                        )
                        raise

                    logger.warning(
                        f"Function {func_name} failed on attempt {attempt}"
                        f"{f'/{current_max_retries}' if current_max_retries else ''}: {repr(e)}"
                    )

                    jitter = delay * jitter_factor
                    actual_delay = max(
                        0.1, delay + random.uniform(-jitter, jitter)
                    )  # Ensure minimum delay

                    logger.info(
                        f"Retrying {func_name} in {actual_delay:.2f}s (attempt {attempt + 1}"
                        f"{f'/{current_max_retries}' if current_max_retries else ''})"
                    )

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
    Decorator implementing the circuit breaker pattern for an async function.

    Prevents repeated calls to a failing function, allowing it time to recover.
    Transitions between CLOSED, OPEN, and HALF_OPEN states based on failures.

    Args:
        service_name: Unique identifier for the service or function group.
        error_threshold: Number of consecutive errors before opening the circuit.
        reset_timeout: Time (seconds) the circuit stays OPEN before attempting recovery (HALF_OPEN).

    Returns:
        The decorated async function.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            func_name = func.__qualname__
            circuit_key = f"{service_name}:{func_name}"

            # Initialize circuit state if it doesn't exist for this key.
            if circuit_key not in circuit_breakers:
                circuit_breakers[circuit_key] = {
                    "state": "CLOSED",
                    "error_count": 0,
                    "last_error_time": 0,
                    "total_calls": 0,
                    "successful_calls": 0,
                    "half_open_calls": 0,
                }

            circuit = circuit_breakers[circuit_key]
            now = time.time()

            if circuit["state"] == "OPEN":
                if now - circuit["last_error_time"] > reset_timeout:
                    logger.info(
                        f"Circuit {circuit_key} transitioning from OPEN to HALF_OPEN after timeout"
                    )
                    circuit["state"] = "HALF_OPEN"
                    circuit["half_open_calls"] = 0
                    # Attempt to update metrics for state transition
                    try:
                        from kubewise.api.context import SERVICE_CIRCUIT_BREAKER_STATUS

                        SERVICE_CIRCUIT_BREAKER_STATUS.labels(
                            service=service_name, circuit_key=circuit_key
                        ).set(CB_STATUS_HALF_OPEN)
                    except (ImportError, NameError, AttributeError):
                        pass  # Metrics context might not be available
                else:
                    # Circuit remains OPEN, fail fast.
                    remaining_time = reset_timeout - (now - circuit["last_error_time"])
                    logger.warning(
                        f"Circuit {circuit_key} is OPEN, fast-failing call to {func_name}. Will attempt recovery in {remaining_time:.1f}s"
                    )
                    raise RuntimeError(
                        f"Circuit breaker open for {service_name}. Recovery attempt in {remaining_time:.1f}s"
                    )

            if (
                circuit["state"] == "HALF_OPEN"
                and circuit["half_open_calls"] >= HALF_OPEN_MAX_CALLS
            ):
                logger.warning(
                    f"Circuit {circuit_key} in HALF_OPEN has reached max test calls ({HALF_OPEN_MAX_CALLS}), fast-failing"
                )
                raise RuntimeError(
                    f"Circuit breaker in half-open state for {service_name}, max test calls reached."
                )

            circuit["total_calls"] += 1
            if circuit["state"] == "HALF_OPEN":
                circuit["half_open_calls"] += 1
                logger.info(
                    f"Testing circuit {circuit_key} with probe call {circuit['half_open_calls']}/{HALF_OPEN_MAX_CALLS}"
                )

            try:
                result = await func(*args, **kwargs)

                # Call Succeeded
                circuit["successful_calls"] += 1
                if circuit["state"] == "HALF_OPEN":
                    logger.info(
                        f"Circuit {circuit_key} test call succeeded, transitioning from HALF_OPEN to CLOSED"
                    )
                    circuit["state"] = "CLOSED"
                    circuit["error_count"] = 0
                    circuit["half_open_calls"] = 0
                    # Attempt to update metrics for state transition
                    try:
                        from kubewise.api.context import (
                            SERVICE_CIRCUIT_BREAKER_STATUS,
                            SERVICE_UP,
                        )

                        SERVICE_CIRCUIT_BREAKER_STATUS.labels(
                            service=service_name, circuit_key=circuit_key
                        ).set(CB_STATUS_CLOSED)
                        SERVICE_UP.labels(service=service_name).set(
                            1
                        )  # Mark service as up
                    except (ImportError, NameError, AttributeError):
                        pass  # Metrics context might not be available

                elif circuit["state"] == "CLOSED" and circuit["error_count"] > 0:
                    # Gradually reset error count on success in CLOSED state.
                    circuit["error_count"] = max(0, circuit["error_count"] - 1)

                return result

            except Exception as e:
                # Call Failed
                circuit["error_count"] += 1
                circuit["last_error_time"] = now

                if circuit["state"] == "HALF_OPEN":
                    logger.warning(
                        f"Circuit {circuit_key} test call failed: {repr(e)}, reopening circuit"
                    )
                    circuit["state"] = "OPEN"
                    circuit["half_open_calls"] = 0
                    # Attempt to update metrics for state transition
                    try:
                        from kubewise.api.context import (
                            SERVICE_CIRCUIT_BREAKER_STATUS,
                            SERVICE_UP,
                        )

                        SERVICE_CIRCUIT_BREAKER_STATUS.labels(
                            service=service_name, circuit_key=circuit_key
                        ).set(CB_STATUS_OPEN)
                        SERVICE_UP.labels(service=service_name).set(
                            0
                        )  # Mark service as down
                    except (ImportError, NameError, AttributeError):
                        pass  # Metrics context might not be available

                elif (
                    circuit["state"] == "CLOSED"
                    and circuit["error_count"] >= error_threshold
                ):
                    logger.warning(
                        f"Circuit {circuit_key} reached error threshold ({circuit['error_count']}), opening circuit. Last error: {repr(e)}"
                    )
                    circuit["state"] = "OPEN"
                    # Attempt to update metrics for state transition
                    try:
                        from kubewise.api.context import (
                            SERVICE_CIRCUIT_BREAKER_STATUS,
                            SERVICE_UP,
                        )

                        SERVICE_CIRCUIT_BREAKER_STATUS.labels(
                            service=service_name, circuit_key=circuit_key
                        ).set(CB_STATUS_OPEN)
                        SERVICE_UP.labels(service=service_name).set(
                            0
                        )  # Mark service as down
                    except (ImportError, NameError, AttributeError):
                        pass  # Metrics context might not be available

                raise  # Re-raise the original exception

        return cast(F, wrapper)

    return decorator
