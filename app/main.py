from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import asyncio
from app.core.config import settings
from app.core.logger import setup_logger
from app.core.middleware import prometheus_middleware
from app.utils.health_checker import HealthChecker
from app.utils.prometheus_client import prometheus
from app.utils.cli_aesthetics import (
    print_ascii_banner, print_version_info,
    start_spinner, print_service_dashboard, print_shutdown_message
)
from app.api import health_router, metrics_router, remediation_router, setup_router, anomaly_router
from app.worker import AutonomousWorker
from app.services.anomaly_event_service import AnomalyEventService
from app.services.gemini_service import GeminiService
from app.services.mode_service import mode_service
from app.core.dependencies.service_factory import set_gemini_service

# Display startup banner with ASCII art first
print_ascii_banner()
print_version_info(version="3.1.0", mode=mode_service.get_mode())

# Setup logging
setup_logger()

# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="""
    # KubeWise: AI-Powered Kubernetes Anomaly Detection and Autonomous Remediation System

    ## Overview
    KubeWise leverages machine learning and the Gemini AI API to detect anomalies in Kubernetes
    clusters and automatically suggest or apply remediation steps.

    ## Key Features
    - **Real-time Kubernetes Monitoring**: Collects metrics from Prometheus for monitoring
    - **ML-powered Anomaly Detection**: Detects abnormal behavior in your cluster
    - **AI-assisted Root Cause Analysis**: Uses Gemini API to analyze anomalies
    - **Smart Remediation**: Suggests or automatically applies appropriate remediation steps
    - **Observability**: Comprehensive dashboard for monitoring and management

    ## API Groups
    - **Health**: Check system health status
    - **Metrics**: Retrieve cluster metrics and manage the anomaly detection model
    - **Anomalies**: Analyze detected anomalies
    - **Remediation**: Manage and apply remediations for detected anomalies
    - **Setup**: Configure system settings and environment variables
    """,
    version="3.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {
            "name": "Root",
            "description": "Basic application information"
        },
        {
            "name": "Health",
            "description": "System health status checks for all components"
        },
        {
            "name": "Metrics",
            "description": "Retrieve cluster metrics and manage the anomaly detection model"
        },
        {
            "name": "Anomalies",
            "description": "Analyze and manage detected anomalies"
        },
        {
            "name": "Remediation",
            "description": "View anomaly events and apply remediation actions"
        },
        {
            "name": "Setup",
            "description": "Configure system mode and environment settings"
        }
    ],
    swagger_ui_parameters={"defaultModelsExpandDepth": -1}  # Hide schema section by default
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add Prometheus middleware
app.middleware("http")(prometheus_middleware)

# Global instances for state management
worker_instance = None
worker_task = None

# Include routers with dependency injection
app.include_router(health_router.router, prefix=settings.API_V1_STR)
app.include_router(metrics_router.router, prefix=settings.API_V1_STR)
app.include_router(remediation_router.router, prefix=settings.API_V1_STR)
app.include_router(setup_router.router, prefix=settings.API_V1_STR)
app.include_router(anomaly_router.router, prefix=settings.API_V1_STR)

async def setup_monitoring_if_needed():
    """
    Set up monitoring server if PROMETHEUS_PORT is configured.
    This allows the application to expose its own metrics for monitoring.
    """
    if settings.PROMETHEUS_PORT:
        try:
            # Start the prometheus client server on the specified port
            prometheus.start_http_server(settings.PROMETHEUS_PORT)
            logger.info(f"Started Prometheus client server on port {settings.PROMETHEUS_PORT}")
            return True
        except Exception as e:
            logger.error(f"Failed to start Prometheus client server: {e}")
            return False
    return False

@app.on_event("startup")
async def startup_event():
    """Handle application startup, including service initialization and worker setup."""
    global worker_task, worker_instance

    # Initialize services status dashboard
    services_status = {
        "Prometheus": {"status": "initializing", "message": "Checking connection..."},
        "Gemini API": {"status": "initializing", "message": "Initializing..."},
        "Worker": {"status": "initializing", "message": "Not started"}
    }

    try:
        logger.info("Starting application initialization...")

        # Initialize the Gemini service if API key is available
        gemini_service_instance = None
        if settings.GEMINI_API_KEY:
            gemini_service_instance = GeminiService()
            # Set the global Gemini service instance in service_factory
            set_gemini_service(gemini_service_instance)

        # Create anomaly service - with file-based storage instead of MongoDB
        anomaly_event_service = AnomalyEventService()

        # Start health check spinner
        health_spinner = start_spinner("Performing dependency checks...")

        # Perform initial health checks
        health_check = HealthChecker()
        health_status = await health_check.check_all()

        health_spinner.stop(True, "Dependency checks completed")

        # Update services status based on health checks
        if "prometheus" in health_status:
            if health_status["prometheus"]["status"] == "healthy":
                services_status["Prometheus"]["status"] = "healthy"
                services_status["Prometheus"]["message"] = "Connection successful"
            else:
                services_status["Prometheus"]["status"] = "warning"
                services_status["Prometheus"]["message"] = health_status["prometheus"]["message"]
                logger.warning(f"Prometheus not healthy: {health_status['prometheus']['message']}")

        if "gemini" in health_status:
            if health_status["gemini"]["status"] == "healthy":
                services_status["Gemini API"]["status"] = "healthy"
                services_status["Gemini API"]["message"] = "Connection successful"
            else:
                services_status["Gemini API"]["status"] = "warning"
                services_status["Gemini API"]["message"] = health_status["gemini"]["message"]
                logger.warning(f"Gemini API not healthy: {health_status['gemini']['message']}")
        else:
            services_status["Gemini API"]["status"] = "warning"
            services_status["Gemini API"]["message"] = "Not configured - AI features limited"

        # Start worker spinner
        worker_spinner = start_spinner("Starting autonomous worker...")

        # Initialize and start the background worker
        worker_instance = AutonomousWorker(
            anomaly_event_svc=anomaly_event_service,
            gemini_svc=gemini_service_instance
        )
        worker_task = asyncio.create_task(worker_instance.run())

        # Update worker status
        services_status["Worker"]["status"] = "healthy"
        services_status["Worker"]["message"] = "Running in background"

        worker_spinner.stop(True, "Autonomous worker started")

        # Print final service status dashboard
        print_service_dashboard(services_status)

        logger.success("KubeWise startup completed successfully. System is operational.")

    except Exception as e:
        logger.error(f"KubeWise startup failed: {e}", exc_info=True)

        print("\n\033[1;91m╔════════════════════════════════════════════════════════════╗\033[0m")
        print("\033[1;91m║                   STARTUP FAILED                            ║\033[0m")
        print("\033[1;91m╚════════════════════════════════════════════════════════════╝\033[0m")

        raise RuntimeError(f"KubeWise startup failed: {e}") from e

@app.on_event("shutdown")
async def shutdown_event():
    """Handle application shutdown, including worker cancellation and resource cleanup."""
    global worker_task

    print_shutdown_message()
    logger.info("Starting application shutdown process...")

    try:
        shutdown_spinner = start_spinner("Cancelling background worker...")

        if worker_task and not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
                shutdown_spinner.stop(True, "Worker task successfully cancelled")
            except asyncio.CancelledError:
                shutdown_spinner.stop(True, "Worker task successfully cancelled")
                logger.info("Worker task successfully cancelled.")
            except Exception as e:
                shutdown_spinner.stop(False, "Error during worker task shutdown")
                logger.error(f"Error during worker task shutdown: {e}", exc_info=True)
        else:
            shutdown_spinner.stop(True, "No active worker to cancel")

        logger.success("KubeWise shutdown completed successfully.")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

        print("\n\033[1;91m╔════════════════════════════════════════════════════════════╗\033[0m")
        print("\033[1;91m║              ERROR DURING SHUTDOWN                          ║\033[0m")
        print("\033[1;91m╚════════════════════════════════════════════════════════════╝\033[0m")

@app.get("/", tags=["Root"])
async def root():
    """Root endpoint providing basic application information."""
    return {
        "name": settings.PROJECT_NAME,
        "version": "3.1.0",
        "description": "AI-Powered Kubernetes Anomaly Detection and Remediation",
        "remediation_mode": mode_service.get_mode()
    }
