import asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging import setup_logging
from app.routers import health, issues, remediate, cluster
from app.services.kubernetes_monitor import kubernetes_monitor
from app.services.redis_cache import redis_service
from app.services.ai_analyzer import ai_analyzer
from app.services.supabase_client import supabase_service
from app.services.alerter import alert_service
from app.models.database import IssueCreate

# Setup logging
logger = setup_logging()

# Create FastAPI app
app = FastAPI(
    title="Kubernetes Failure Detection",
    description="A service for monitoring Kubernetes clusters, detecting anomalies, predicting failures, and suggesting remediation steps.",
    version="0.1.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with allowed domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router)
app.include_router(issues.router)
app.include_router(remediate.router)
app.include_router(cluster.router)

# Background tasks
analysis_tasks = {}  # Store task futures for cleanup
stale_issue_cleanup_task = None


@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    try:
        # Initialize Redis
        await redis_service.connect()
        logger.info("Redis service initialized")

        # Initialize Kubernetes monitoring
        kubernetes_monitor.initialize()
        if kubernetes_monitor.initialized:
            # Get cluster information
            cluster_name = kubernetes_monitor.get_cluster_name()
            nodes = kubernetes_monitor.core_v1_api.list_node()
            pods = kubernetes_monitor.core_v1_api.list_pod_for_all_namespaces()

            logger.info(f"Connected to Kubernetes cluster: {cluster_name}")
            logger.info(f"Number of nodes: {len(nodes.items)}")
            logger.info(f"Number of pods: {len(pods.items)}")

            # Start monitoring
            await kubernetes_monitor.start_monitoring()
            logger.info("Kubernetes monitoring started")
        else:
            logger.warning("Kubernetes monitoring not initialized - running in development mode")

        # Check if Supabase has required tables
        has_tables = await supabase_service.check_required_tables()
        if not has_tables:
            logger.warning("Supabase tables missing - some features will be limited")
            logger.warning("Please create tables using the create_supabase_tables.sql script")

        # Start analysis task
        analysis_tasks["main"] = asyncio.create_task(run_analysis_loop())

        # Start stale issue cleanup task
        stale_issue_cleanup_task = asyncio.create_task(run_stale_issue_cleanup())

        # Subscribe to Supabase realtime updates for critical issues
        try:
            supabase_service.subscribe_to_critical_issues(alert_service.handle_supabase_notification)
        except Exception as e:
            logger.error(f"Supabase subscription failed: {e}")
            logger.warning("Application will start without Supabase realtime subscriptions")

        logger.info("Application started successfully")

    except Exception as e:
        logger.error(f"Error during startup: {e}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown."""
    logger.info("Shutting down application...")

    # Stop Kubernetes monitoring
    await kubernetes_monitor.stop_monitoring()

    # Disconnect from Redis
    await redis_service.disconnect()

    # Cancel analysis tasks
    for name, task in analysis_tasks.items():
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info(f"Analysis task {name} cancelled")

    # Cancel stale issue cleanup task
    if stale_issue_cleanup_task and not stale_issue_cleanup_task.done():
        stale_issue_cleanup_task.cancel()
        try:
            await stale_issue_cleanup_task
        except asyncio.CancelledError:
            logger.info("Stale issue cleanup task cancelled")

    # Unsubscribe from Supabase realtime
    supabase_service.unsubscribe_realtime()

    logger.info("Application shutdown complete")


async def run_analysis_loop():
    """Run AI analysis on cluster state periodically."""
    logger.info("Starting analysis loop")

    while True:
        try:
            # Get cluster state from Redis
            cluster_state = await redis_service.get_value("k8s:cluster_state")

            if cluster_state:
                logger.info("Running AI analysis on cluster state")

                # Analyze cluster state
                try:
                    issues = await ai_analyzer.analyze_cluster_state(cluster_state)

                    # Save detected issues to database if tables exist
                    if supabase_service.has_tables:
                        for issue in issues:
                            updated_issue = await supabase_service.add_or_update_issue(issue)

                            # Check if this is a new critical issue and send alert if needed
                            if updated_issue.severity == "CRITICAL" and updated_issue.status == "NEW":
                                await alert_service.process_new_critical_issue(updated_issue)
                    else:
                        logger.info("Skipping saving issues to database - Supabase tables don't exist")

                    logger.info(f"AI analysis completed, found {len(issues)} issues")
                except Exception as e:
                    logger.error(f"Error during AI analysis: {str(e)}")
                    # Include traceback for better debugging
                    import traceback
                    logger.error(f"Traceback: {traceback.format_exc()}")
            else:
                logger.warning("No cluster state found in Redis, skipping analysis")

            # Wait for next analysis interval
            await asyncio.sleep(settings.MONITOR_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            logger.info("Analysis loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in analysis loop: {str(e)}")
            # Include traceback for better debugging
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            await asyncio.sleep(10)  # Shorter delay on error


async def run_stale_issue_cleanup():
    """
    Clean up stale issues periodically.
    Mark issues that haven't been detected for a while as resolved.
    """
    logger.info("Starting stale issue cleanup task")

    while True:
        try:
            # Skip database operations if Supabase tables don't exist
            if not supabase_service.has_tables:
                logger.info("Skipping stale issue cleanup - Supabase tables don't exist")
                await asyncio.sleep(settings.MONITOR_INTERVAL_SECONDS * 5)
                continue

            # Calculate threshold time (issues not detected for 3 intervals are considered stale)
            threshold = datetime.utcnow() - timedelta(seconds=settings.MONITOR_INTERVAL_SECONDS * 3)

            logger.info(f"Cleaning up issues not detected since {threshold.isoformat()}")

            # Mark stale issues as resolved
            count = await supabase_service.mark_stale_issues_as_resolved(threshold)

            if count > 0:
                logger.info(f"Marked {count} stale issues as resolved")

            # Run cleanup every 5 monitoring intervals
            await asyncio.sleep(settings.MONITOR_INTERVAL_SECONDS * 5)

        except asyncio.CancelledError:
            logger.info("Stale issue cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in stale issue cleanup: {str(e)}")
            # Include traceback for better debugging
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            await asyncio.sleep(60)  # Retry after 1 minute on error


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Middleware to log requests and add request ID to context."""
    # Generate request ID
    request_id = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{id(request)}"

    # Add context to logger
    with logger.contextualize(request_id=request_id):
        logger.info(f"{request.method} {request.url.path}")

        # Process request
        response = await call_next(request)

        return response
