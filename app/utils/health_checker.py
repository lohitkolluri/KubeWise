from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
import google.generativeai as genai
import requests
from typing import Dict, Any
import asyncio
from app.core.config import settings

class HealthChecker:
    @staticmethod
    async def check_mongodb(uri: str, db_name: str) -> Dict[str, Any]:
        """Check MongoDB connection and basic operations."""
        try:
            client = AsyncIOMotorClient(uri)
            db = client[db_name]
            # Perform a simple operation to verify connection
            await db.command("ping")
            client.close()
            return {
                "status": "healthy",
                "message": "Successfully connected to MongoDB"
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "message": f"MongoDB connection failed: {str(e)}"
            }

    @staticmethod
    async def check_gemini(api_key: str) -> Dict[str, Any]:
        """Check Gemini API connection."""
        if not api_key:
            return {
                "status": "disabled",
                "message": "Gemini API is not configured (no API key)"
            }

        try:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-pro')
            # Test with a simple prompt
            response = model.generate_content("Test connection.")
            if response and response.text:
                return {
                    "status": "healthy",
                    "message": "Successfully connected to Gemini API"
                }
            return {
                "status": "unhealthy",
                "message": "Gemini API response validation failed"
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "message": f"Gemini API connection failed: {str(e)}"
            }

    @staticmethod
    async def check_prometheus(prometheus_url: str) -> Dict[str, Any]:
        """Check Prometheus server accessibility."""
        try:
            response = requests.get(f"{prometheus_url}/api/v1/status/config")
            if response.status_code == 200:
                return {
                    "status": "healthy",
                    "message": f"Prometheus server is accessible at {prometheus_url}"
                }
            return {
                "status": "unhealthy",
                "message": f"Prometheus server returned status code {response.status_code}"
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "message": f"Prometheus server check failed: {str(e)}"
            }

    @staticmethod
    async def check_kubernetes() -> Dict[str, Any]:
        """Check Kubernetes client configuration."""
        try:
            from kubernetes import client, config
            config.load_kube_config()
            v1 = client.CoreV1Api()
            nodes = v1.list_node()
            return {
                "status": "healthy",
                "message": f"Connected to Kubernetes cluster with {len(nodes.items)} nodes"
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "message": f"Kubernetes connection failed: {str(e)}"
            }

    async def check_all(self) -> Dict[str, Dict[str, Any]]:
        """Check health of all services using application settings."""
        results = {}

        # Run all checks concurrently
        mongo_check = self.check_mongodb(settings.MONGODB_ATLAS_URI, settings.MONGODB_DB_NAME)
        gemini_check = self.check_gemini(settings.GEMINI_API_KEY)
        prometheus_check = self.check_prometheus(settings.PROMETHEUS_URL)
        k8s_check = self.check_kubernetes()

        checks = await asyncio.gather(
            mongo_check, gemini_check, prometheus_check, k8s_check,
            return_exceptions=True
        )

        # Map results to named services
        results["mongo"] = checks[0] if not isinstance(checks[0], Exception) else {"status": "unhealthy", "message": str(checks[0])}
        results["gemini"] = checks[1] if not isinstance(checks[1], Exception) else {"status": "unhealthy", "message": str(checks[1])}
        results["prometheus"] = checks[2] if not isinstance(checks[2], Exception) else {"status": "unhealthy", "message": str(checks[2])}
        results["kubernetes"] = checks[3] if not isinstance(checks[3], Exception) else {"status": "unhealthy", "message": str(checks[3])}

        # Add overall status
        critical_services = ["mongo"] # Only MongoDB is critical for now
        critical_healthy = all(
            results[service]["status"] == "healthy"
            for service in critical_services
        )
        results["overall"] = {
            "status": "healthy" if critical_healthy else "unhealthy",
            "message": "All critical services healthy" if critical_healthy else "One or more critical services unhealthy"
        }

        # Log results
        for service, result in results.items():
            if service == "overall":
                continue
            if result["status"] == "healthy":
                logger.info(f"Health check - {service}: {result['message']}")
            elif result["status"] == "disabled":
                logger.warning(f"Health check - {service}: {result['message']}")
            else:
                logger.error(f"Health check - {service}: {result['message']}")

        return results

    @staticmethod
    async def check_all_services(
        mongodb_uri: str,
        mongodb_db_name: str,
        gemini_api_key: str,
        prometheus_port: int = None
    ) -> Dict[str, Any]:
        """
        Check health of all services with explicit parameters.

        Note: This is deprecated. Use check_all() instead.
        """
        logger.warning("Using deprecated check_all_services method. Use check_all() instead.")
        results = {}

        # Use settings.PROMETHEUS_PORT if not provided
        if prometheus_port is None:
            prometheus_port = settings.PROMETHEUS_PORT

        # Ensure prometheus_port is an int
        try:
            prometheus_port = int(prometheus_port)
        except (ValueError, TypeError):
            prometheus_port = 9090  # Default to 9090 if conversion fails

        # Run all checks concurrently
        checks = await asyncio.gather(
            HealthChecker.check_mongodb(mongodb_uri, mongodb_db_name),
            HealthChecker.check_gemini(gemini_api_key),
            HealthChecker.check_prometheus(f"http://localhost:{prometheus_port}"),
            return_exceptions=True
        )

        services = ["MongoDB", "Gemini", "Prometheus"]
        for service, check in zip(services, checks):
            if isinstance(check, Exception):
                results[service] = {
                    "status": "unhealthy",
                    "message": f"Check failed: {str(check)}"
                }
            else:
                results[service] = check

        # Log results
        for service, result in results.items():
            if result["status"] == "healthy":
                logger.info(f"{service}: {result['message']}")
            else:
                logger.error(f"{service}: {result['message']}")

        return results
