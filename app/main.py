import asyncio
import uuid
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from loguru import logger

from app.api import (
    anomaly_router,
    health_router,
    metrics_router,
    remediation_router,
    setup_router,
)
from app.core.config import settings
from app.core.dependencies.service_factory import set_gemini_service
from app.core.exceptions import KubeWiseException
from app.core.logger import setup_logger, LogContext
from app.core import logger as app_logger
from app.core.middleware import setup_middleware
from app.services.anomaly_event_service import AnomalyEventService
from app.services.gemini_service import GeminiService
from app.services.mode_service import mode_service
from app.utils.cli_aesthetics import (
    print_ascii_banner,
    print_service_dashboard,
    print_shutdown_message,
    print_version_info,
    start_spinner,
)
from app.utils.health_checker import HealthChecker
from app.worker import AutonomousWorker


# Initial CLI display
print_ascii_banner()
print_version_info(version="3.1.0", mode=mode_service.get_mode())

# Setup logging with our enhanced structured logger
setup_logger()

# FastAPI application instance
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="""
    # KubeWise: AI-Powered Kubernetes Anomaly Detection and Autonomous Remediation System

    ## Overview
    KubeWise leverages machine learning and the Gemini AI API to detect anomalies in Kubernetes
    clusters and automatically suggest or apply remediation steps.

    ## Key Features
    - **Real-time Kubernetes Monitoring**: Collects metrics from Prometheus
    - **ML-powered Anomaly Detection**: Detects abnormal behavior in your cluster
    - **AI-assisted Root Cause Analysis**: Uses Gemini API for anomaly explanation
    - **Smart Remediation**: Suggests or automatically applies fixes
    - **Observability**: Dashboard for system monitoring

    ## API Groups
    - Health, Metrics, Anomalies, Remediation, Setup
    """,
    version="3.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "Root", "description": "Basic application information"},
        {"name": "Health", "description": "System health status checks for all components"},
        {"name": "Metrics", "description": "Retrieve metrics and manage detection models"},
        {"name": "Anomalies", "description": "Analyze and manage detected anomalies"},
        {"name": "Remediation", "description": "Apply or suggest remediation actions"},
        {"name": "Setup", "description": "Configure system mode and environment settings"},
    ],
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
)

# Configure all middleware using our enhanced middleware setup
setup_middleware(app)

# Application routers
app.include_router(health_router.router, prefix=settings.API_V1_STR)
app.include_router(metrics_router.router, prefix=settings.API_V1_STR)
app.include_router(remediation_router.router, prefix=settings.API_V1_STR)
app.include_router(setup_router.router, prefix=settings.API_V1_STR)
app.include_router(anomaly_router.router, prefix=settings.API_V1_STR)

# Globals for worker state
worker_instance = None
worker_task = None


@app.on_event("startup")
async def startup_event():
    """Handle application startup"""
    global worker_task, worker_instance
    
    # Generate a unique application instance ID
    app_instance_id = str(uuid.uuid4())
    
    # Set application-wide logging context
    with LogContext(
        request_id=app_instance_id,
        operation="startup",
        component="application"
    ):
        services_status = {
            "Prometheus": {"status": "initializing", "message": "Checking connection..."},
            "Gemini API": {"status": "initializing", "message": "Initializing..."},
            "Worker": {"status": "initializing", "message": "Not started"},
        }

        try:
            app_logger.info("Starting application initialization...")

            gemini_service = None
            if settings.GEMINI_API_KEY:
                app_logger.info("Initializing Gemini service")
                gemini_service = GeminiService(
                    api_key=settings.GEMINI_API_KEY,
                    model_type=settings.GEMINI_MODEL,
                )
                set_gemini_service(gemini_service)
                app_logger.info(f"Gemini service initialized with model {settings.GEMINI_MODEL}")

            anomaly_service = AnomalyEventService()
            spinner = start_spinner("Performing dependency checks...")

            health_status = await HealthChecker().check_all()
            spinner.stop(True, "Dependency checks completed")

            for key, label in [("prometheus", "Prometheus"), ("gemini", "Gemini API")]:
                if key in health_status:
                    status = health_status[key]
                    services_status[label]["status"] = (
                        "healthy" if status["status"] == "healthy" else "warning"
                    )
                    services_status[label]["message"] = status["message"]
                    if status["status"] != "healthy":
                        app_logger.warning(
                            f"{label} not healthy: {status['message']}",
                            service=key,
                            status=status["status"]
                        )

            worker_spinner = start_spinner("Starting autonomous worker...")
            worker_instance = AutonomousWorker(
                anomaly_event_svc=anomaly_service,
                gemini_svc=gemini_service,
            )
            worker_task = asyncio.create_task(worker_instance.run())

            services_status["Worker"] = {"status": "healthy", "message": "Running"}
            worker_spinner.stop(True, "Autonomous worker started")

            print_service_dashboard(services_status)
            app_logger.success("KubeWise startup completed. System operational.")

        except Exception as e:
            app_logger.critical("Startup failed", exception=e)
            raise RuntimeError("KubeWise startup failed") from e


@app.on_event("shutdown")
async def shutdown_event():
    """Handle graceful shutdown of background tasks"""
    global worker_task, worker_instance

    # Set shutdown context for logging
    with LogContext(
        operation="shutdown",
        component="application"
    ):
        print_shutdown_message()
        app_logger.info("Initiating shutdown...")

        try:
            spinner = start_spinner("Stopping background worker...")

            if worker_task and not worker_task.done():
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
                spinner.stop(True, "Worker task cancelled")
                app_logger.info("Worker task cancelled successfully")
            else:
                spinner.stop(True, "No active worker")
                app_logger.info("No active worker to shut down")

            # Gracefully close any service connections
            if worker_instance:
                try:
                    # Gracefully close Gemini service connections
                    if hasattr(worker_instance, 'gemini_service') and worker_instance.gemini_service:
                        await worker_instance.gemini_service.close()
                        app_logger.info("Gemini service connections closed")
                except Exception as e:
                    app_logger.error(f"Error closing service connections: {e}")

            app_logger.success("Shutdown complete.")
        except Exception as e:
            app_logger.error("Error during shutdown", exception=e)


@app.get("/", tags=["Root"])
async def root():
    """Root endpoint for service metadata."""
    return {
        "name": settings.PROJECT_NAME,
        "version": "3.1.0",
        "description": "AI-Powered Kubernetes Anomaly Detection and Remediation",
        "remediation_mode": mode_service.get_mode(),
    }
