from time import time

from fastapi import Request
from loguru import logger

# Changed import from prometheus_client to prometheus_scraper
from app.utils.prometheus_scraper import prometheus_scraper


async def prometheus_middleware(request: Request, call_next):
    """
    Middleware for request logging without Prometheus metrics collection.
    This version only logs request details and no longer collects metrics.
    """
    start_time = time()

    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        logger.error(f"Request failed: {e}")
        status_code = 500
        raise
    finally:
        # Calculate request duration
        duration = time() - start_time
        endpoint = request.url.path
        method = request.method

        # Log request details
        logger.info(f"{method} {endpoint} - {status_code} - {duration:.3f}s")

    return response
