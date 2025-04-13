from fastapi import Request
from time import time
from app.utils.prometheus_client import prometheus
from loguru import logger

async def prometheus_middleware(request: Request, call_next):
    """Middleware to track request metrics using Prometheus."""
    start_time = time()

    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        logger.error(f"Request failed: {e}")
        status_code = 500
        raise
    finally:
        # Record request duration
        duration = time() - start_time
        endpoint = request.url.path
        method = request.method

        # Record metrics
        prometheus.record_request(endpoint, method, status_code)
        prometheus.record_request_duration(endpoint, duration)

        # Log request details
        logger.info(f"{method} {endpoint} - {status_code} - {duration:.3f}s")

    return response
