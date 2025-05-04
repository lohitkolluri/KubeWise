import functools # For lru_cache
from typing import AsyncGenerator, Optional

import httpx # Added for http_client dependency
import motor.motor_asyncio
from fastapi import Depends, Request # Added Depends
from kubernetes_asyncio import client
from loguru import logger

from kubewise.api.context import AppContext # Import AppContext from context.py
from kubewise.config import Settings, settings

# Add MongoDB connection pool configuration
MONGO_MAX_POOL_SIZE = 100
MONGO_MIN_POOL_SIZE = 10
MONGO_MAX_IDLE_TIME_MS = 30000  # 30 seconds

# --- Dependency Functions ---

# MongoDB client factory function
async def create_mongo_client(uri: str) -> motor.motor_asyncio.AsyncIOMotorClient:
    """Create a properly configured MongoDB client with connection pooling."""
    client = motor.motor_asyncio.AsyncIOMotorClient(
        uri,
        maxPoolSize=MONGO_MAX_POOL_SIZE, 
        minPoolSize=MONGO_MIN_POOL_SIZE,
        maxIdleTimeMS=MONGO_MAX_IDLE_TIME_MS,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
        waitQueueTimeoutMS=5000,
        retryWrites=True,
        w="majority"
    )
    return client

# Dependency to get the AppContext itself
async def get_app_context(request: Request) -> AppContext:
    """FastAPI dependency to retrieve the application context."""
    if not hasattr(request.app.state, "app_context"):
        logger.error("AppContext not found in application state. Lifespan might have failed.")
        raise RuntimeError("Application context not available.")
    return request.app.state.app_context


async def get_mongo_db(
    ctx: AppContext = Depends(get_app_context) # Depend on get_app_context
) -> motor.motor_asyncio.AsyncIOMotorDatabase:
    """
    FastAPI dependency to get the MongoDB database instance.

    Yields:
        An instance of AsyncIOMotorDatabase.

    Raises:
        RuntimeError: If the MongoDB database is not available in the context.
    """
    if ctx.db is None:
        logger.error("MongoDB database instance not found in AppContext.")
        raise RuntimeError("MongoDB database not available.")
    # No need for try/except yielding here, just return the instance
    return ctx.db


async def get_k8s_api_client(
    ctx: AppContext = Depends(get_app_context) # Depend on get_app_context
) -> client.ApiClient:
    """
    FastAPI dependency to get the Kubernetes API client instance.

    Yields:
        An instance of kubernetes_asyncio.client.ApiClient.

    Raises:
        RuntimeError: If the Kubernetes client is not available in the context.
    """
    if ctx.k8s_api_client is None:
        logger.error("Kubernetes API client instance not found in AppContext.")
        raise RuntimeError("Kubernetes API client not available.")
    return ctx.k8s_api_client


async def get_http_client(
    ctx: AppContext = Depends(get_app_context) # Depend on get_app_context
) -> httpx.AsyncClient:
    """
    FastAPI dependency to get the shared httpx.AsyncClient instance.

    Raises:
        RuntimeError: If the HTTP client is not available in the context.
    """
    if ctx.http_client is None:
        logger.error("HTTP client instance not found in AppContext.")
        raise RuntimeError("HTTP client not available.")
    return ctx.http_client


# Use lru_cache on get_settings as requested, although its current implementation
# already loads settings only once. This adds an explicit caching layer.
@functools.lru_cache()
def get_settings() -> Settings: # Use the class type for the hint
    """
    FastAPI dependency to get the application settings instance.
    (Simple case, just returns the global settings object).
    """
    return settings

# --- Optional: Dependency for accessing anomaly detector instance ---
# If the detector needs to be accessed in API routes, create a dependency for it.
# This requires the detector instance to be created and stored globally during startup,
# similar to the clients above.

# from kubewise.models.detector import OnlineAnomalyDetector
# anomaly_detector_instance: Optional[OnlineAnomalyDetector] = None

# async def get_anomaly_detector() -> OnlineAnomalyDetector:
#     if anomaly_detector_instance is None:
#         logger.error("Anomaly detector is not initialized.")
#         raise RuntimeError("Anomaly detector not available.")
#     return anomaly_detector_instance
