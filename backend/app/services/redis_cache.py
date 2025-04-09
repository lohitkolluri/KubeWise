import json
from typing import Any, Dict, Optional, Union, List
import asyncio
from datetime import datetime

from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings


# Custom JSON encoder to handle datetime serialization
class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class RedisService:
    """Service for interacting with Redis for caching."""

    def __init__(self):
        """Initialize Redis client."""
        self.redis: Optional[Redis] = None
        self.connected = False

    async def connect(self) -> None:
        """
        Connect to Redis server.
        """
        try:
            # Parse REDIS_URL and make sure we're connecting to the right host
            redis_url = settings.REDIS_URL
            logger.info(f"Connecting to Redis at {redis_url}")

            self.redis = Redis.from_url(redis_url, decode_responses=True)
            # Test connection
            await self.redis.ping()
            self.connected = True
            logger.info("Connected to Redis")
        except RedisError as e:
            self.connected = False
            logger.error(f"Failed to connect to Redis: {e}")
            # Set up reconnection
            asyncio.create_task(self._reconnect())

    async def _reconnect(self, retry_delay: int = 5) -> None:
        """
        Attempt to reconnect to Redis on failure.

        Args:
            retry_delay: Seconds to wait between reconnection attempts
        """
        while not self.connected:
            logger.info(f"Attempting to reconnect to Redis in {retry_delay} seconds...")
            await asyncio.sleep(retry_delay)
            try:
                await self.connect()
            except Exception as e:
                logger.error(f"Reconnection to Redis failed: {e}")

    async def disconnect(self) -> None:
        """
        Disconnect from Redis server.
        """
        if self.redis:
            await self.redis.close()
            self.connected = False
            logger.info("Disconnected from Redis")

    async def set_value(self, key: str, value: Any, ttl: int = 300) -> bool:
        """
        Set a value in Redis with TTL.

        Args:
            key: Redis key
            value: Value to store (will be JSON serialized if not a string)
            ttl: Time to live in seconds

        Returns:
            True if successful, False otherwise
        """
        if not self.connected or not self.redis:
            logger.warning("Redis not connected, cannot set value")
            return False

        try:
            # Convert non-string values to JSON
            if not isinstance(value, str):
                value = json.dumps(value, cls=DateTimeEncoder)

            # Set value with TTL
            await self.redis.set(key, value, ex=ttl)
            return True
        except RedisError as e:
            logger.error(f"Failed to set value in Redis: {e}")
            return False

    async def get_value(self, key: str, default: Any = None) -> Any:
        """
        Get a value from Redis.

        Args:
            key: Redis key
            default: Default value if key does not exist

        Returns:
            The value if found, default otherwise
        """
        if not self.connected or not self.redis:
            logger.warning("Redis not connected, cannot get value")
            return default

        try:
            value = await self.redis.get(key)
            if value is None:
                return default

            # Try to parse as JSON, return as is if not valid JSON
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        except RedisError as e:
            logger.error(f"Failed to get value from Redis: {e}")
            return default

    async def delete_value(self, key: str) -> bool:
        """
        Delete a value from Redis.

        Args:
            key: Redis key

        Returns:
            True if successful, False otherwise
        """
        if not self.connected or not self.redis:
            logger.warning("Redis not connected, cannot delete value")
            return False

        try:
            await self.redis.delete(key)
            return True
        except RedisError as e:
            logger.error(f"Failed to delete value from Redis: {e}")
            return False

    async def set_kubernetes_object(
        self,
        obj_type: str,
        obj_name: str,
        namespace: Optional[str],
        data: Dict[str, Any],
        ttl: int = 300
    ) -> bool:
        """
        Cache a Kubernetes object.

        Args:
            obj_type: Object type (e.g., "pod", "node", "deployment")
            obj_name: Object name
            namespace: Object namespace (None for cluster-scoped objects)
            data: Object data
            ttl: Time to live in seconds

        Returns:
            True if successful, False otherwise
        """
        key = self._get_k8s_object_key(obj_type, obj_name, namespace)
        return await self.set_value(key, data, ttl)

    async def get_kubernetes_object(
        self,
        obj_type: str,
        obj_name: str,
        namespace: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Get a cached Kubernetes object.

        Args:
            obj_type: Object type (e.g., "pod", "node", "deployment")
            obj_name: Object name
            namespace: Object namespace (None for cluster-scoped objects)

        Returns:
            The object data if found, None otherwise
        """
        key = self._get_k8s_object_key(obj_type, obj_name, namespace)
        return await self.get_value(key)

    async def cache_kubernetes_collection(
        self,
        obj_type: str,
        items: List[Dict[str, Any]],
        ttl: int = 300
    ) -> bool:
        """
        Cache a collection of Kubernetes objects (e.g., all pods).

        Args:
            obj_type: Object type (e.g., "pods", "nodes", "deployments")
            items: List of object data
            ttl: Time to live in seconds

        Returns:
            True if successful, False otherwise
        """
        collection_key = f"k8s:{obj_type}:collection"

        # Also cache the collection list for quick access
        success = await self.set_value(collection_key, items, ttl)

        # Cache individual objects too
        if success:
            for item in items:
                name = item.get("metadata", {}).get("name")
                namespace = item.get("metadata", {}).get("namespace")

                if name:
                    # Convert plural to singular for individual items
                    singular_type = obj_type[:-1] if obj_type.endswith("s") else obj_type
                    await self.set_kubernetes_object(singular_type, name, namespace, item, ttl)

        return success

    async def get_kubernetes_collection(self, obj_type: str) -> List[Dict[str, Any]]:
        """
        Get a cached collection of Kubernetes objects.

        Args:
            obj_type: Object type (e.g., "pods", "nodes", "deployments")

        Returns:
            The list of objects if found, empty list otherwise
        """
        collection_key = f"k8s:{obj_type}:collection"
        result = await self.get_value(collection_key, [])
        return result if isinstance(result, list) else []

    def _get_k8s_object_key(self, obj_type: str, obj_name: str, namespace: Optional[str]) -> str:
        """
        Generate a Redis key for a Kubernetes object.

        Args:
            obj_type: Object type (e.g., "pod", "node", "deployment")
            obj_name: Object name
            namespace: Object namespace (None for cluster-scoped objects)

        Returns:
            Redis key
        """
        if namespace:
            return f"k8s:{obj_type}:{namespace}:{obj_name}"
        return f"k8s:{obj_type}:{obj_name}"


# Create a global instance for use across the application
redis_service = RedisService()
