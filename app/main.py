from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import asyncio
import os
import sys
from app.core.config import settings
from app.core.database import database, get_database
from app.core.logger import setup_logger
from app.core.middleware import prometheus_middleware
from app.utils.health_checker import HealthChecker
from app.utils.prometheus_client import prometheus
from app.utils.cli_aesthetics import (
    print_ascii_banner, print_version_info,
    start_spinner, print_service_dashboard, print_shutdown_message,
    ServiceStatus
)
from app.api import health_router, metrics_router, remediation_router, setup_router, anomaly_router
from app.worker import AutonomousWorker
from app.services.anomaly_event_service import AnomalyEventService
from app.services.gemini_service import GeminiService
from app.services.mode_service import mode_service

# Setup logging
setup_logger()

# --- Global Service Instances ---
# Instantiate GeminiService globally if API key is available
gemini_service_instance = GeminiService() if settings.GEMINI_API_KEY else None

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
    version="3.0.0",
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

# Include routers
app.include_router(health_router.router, prefix=settings.API_V1_STR)
app.include_router(metrics_router.router, prefix=settings.API_V1_STR)
app.include_router(remediation_router.router, prefix=settings.API_V1_STR)
app.include_router(setup_router.router, prefix=settings.API_V1_STR)
app.include_router(anomaly_router.router, prefix=settings.API_V1_STR)

# Global worker state
worker_task = None
worker_instance = None

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

async def load_env_settings_from_db(db):
    """Load environment settings from database and apply them to the current runtime."""
    try:
        config_doc = await db.system_config.find_one({"_id": "environment_settings"})
        if not config_doc or "settings" not in config_doc:
            logger.info("No stored environment settings found during startup")
            return False

        stored_settings = config_doc["settings"]
        if not stored_settings:
            logger.info("Empty environment settings document found during startup")
            return False

        # Apply environment variables
        env_vars_applied = 0
        for key, value in stored_settings.items():
            if value is not None and value != "":
                os.environ[key] = str(value)
                env_vars_applied += 1

        logger.info(f"Applied {env_vars_applied} environment variables from database during startup")
        return True
    except Exception as e:
        logger.error(f"Error loading environment settings from database: {e}")
        return False

@app.on_event("startup")
async def startup_event():
    """Handle application startup, including service initialization and worker setup."""
    global worker_task, worker_instance

    # Display startup banner with ASCII art
    print_ascii_banner()
    print_version_info(version="3.0.0", mode=mode_service.get_mode())

    # Initialize services status dashboard
    services_status = {
        "MongoDB": {"status": "initializing", "message": "Connecting to database..."},
        "Prometheus": {"status": "initializing", "message": "Checking connection..."},
        "Gemini API": {"status": "initializing", "message": "Initializing..."},
        "Worker": {"status": "initializing", "message": "Not started"}
    }

    try:
        # Initialize MongoDB spinner
        mongo_spinner = start_spinner("Connecting to MongoDB...")

        logger.info("Starting application initialization...")
        await database.connect_to_database()
        db_instance = await get_database()

        # Update MongoDB status and stop spinner
        mongo_spinner.stop(True, "MongoDB connection successful")
        services_status["MongoDB"] = {"status": "healthy", "message": "Connection established"}

        # Start environment settings spinner
        env_spinner = start_spinner("Loading environment settings...")

        # Load environment settings from database
        await load_env_settings_from_db(db_instance)

        env_spinner.stop(True, "Environment settings loaded")

        # Create anomaly service
        anomaly_event_service = AnomalyEventService(db_instance)

        # Start health check spinner
        health_spinner = start_spinner("Performing dependency checks...")

        # Perform initial health checks
        health_check = HealthChecker()
        health_status = await health_check.check_all()

        health_spinner.stop(True, "Dependency checks completed")

        # Update services status based on health checks
        if health_status["mongo"]["status"] == "healthy":
            services_status["MongoDB"]["status"] = "healthy"
            services_status["MongoDB"]["message"] = "Connection successful"
        else:
            services_status["MongoDB"]["status"] = "error"
            services_status["MongoDB"]["message"] = health_status["mongo"]["message"]
            logger.error(f"MongoDB connection failed: {health_status['mongo']['message']}")
            raise RuntimeError("MongoDB connection failed at startup. Aborting.")

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

        db_spinner = start_spinner("Closing database connections...")
        await database.close_database_connection()
        db_spinner.stop(True, "Database connections closed")

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
        "version": "3.0.0",
        "description": "AI-Powered Kubernetes Anomaly Detection and Remediation",
        "remediation_mode": mode_service.get_mode()
    }
