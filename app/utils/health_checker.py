from loguru import logger
from google import genai  # Updated import
import requests
from typing import Dict, Any
import asyncio
from app.core.config import settings

class HealthChecker:
    @staticmethod
    async def check_gemini(api_key: str) -> Dict[str, Any]:
        """Check Gemini API connection using the new google-genai SDK."""
        if not api_key:
            return {
                "status": "disabled",
                "message": "Gemini API is not configured (no API key)"
            }

        try:
            client = genai.Client(api_key=api_key)
            # Use the latest recommended model
            model_name = "gemini-1.5-pro"
            # Test with a simple prompt
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=["Test connection."]
            )
            if response and hasattr(response, 'text') and response.text:
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
        gemini_check = self.check_gemini(settings.GEMINI_API_KEY)
        prometheus_check = self.check_prometheus(settings.PROMETHEUS_URL)
        k8s_check = self.check_kubernetes()

        checks = await asyncio.gather(
            gemini_check, prometheus_check, k8s_check,
            return_exceptions=True
        )

        # Map results to named services
        results["gemini"] = checks[0] if not isinstance(checks[0], Exception) else {"status": "unhealthy", "message": str(checks[0])}
        results["prometheus"] = checks[1] if not isinstance(checks[1], Exception) else {"status": "unhealthy", "message": str(checks[1])}
        results["kubernetes"] = checks[2] if not isinstance(checks[2], Exception) else {"status": "unhealthy", "message": str(checks[2])}

        # Add overall status
        critical_services = ["prometheus", "kubernetes"]  # These services are critical
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
