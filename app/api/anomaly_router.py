from fastapi import APIRouter, Depends, HTTPException, Body
from app.models.anomaly_detector import anomaly_detector
from app.services.anomaly_event_service import AnomalyEventService
from app.core.database import get_database
from app.core.config import settings
from app.utils.prometheus_scraper import prometheus_scraper
from loguru import logger
from typing import Dict, Any, List, Optional
from datetime import datetime

router = APIRouter(
    prefix="/anomalies",
    tags=["Anomalies"],
    responses={404: {"description": "Not found"}}
)

@router.post("/analyze/{anomaly_id}",
             summary="Analyze a detected anomaly",
             description="Perform AI-powered analysis on a detected anomaly using Gemini API and generate remediation suggestions")
async def analyze_anomaly(
    anomaly_id: str,
    db = Depends(get_database)
):
    """
    Analyze a detected anomaly using Gemini AI.
    Requires Gemini Service to be available.

    Parameters:
        anomaly_id (str): ID of the anomaly event to analyze

    Returns:
        dict: Analysis results containing:
            - status: "success" or "error"
            - message: Description of the operation result
            - analysis: AI-generated anomaly analysis
            - suggestions: Recommended remediation commands
            - event_id: The anomaly event ID

    Raises:
        HTTPException: If the anomaly event is not found or Gemini service is unavailable
    """
    try:
        event_service = AnomalyEventService(db)
        # Get Gemini service from main app
        from app.main import gemini_service_instance
        if not gemini_service_instance:
            raise HTTPException(status_code=503, detail="Gemini service is not configured or available.")

        event = await event_service.get_event(anomaly_id)
        if not event:
            raise HTTPException(status_code=404, detail=f"Anomaly event not found: {anomaly_id}")

        # Prepare context for Gemini
        anomaly_context = {
            "entity_id": event.entity_id,
            "entity_type": event.entity_type,
            "namespace": event.namespace,
            "metrics_snapshot": event.metric_snapshot[-1] if event.metric_snapshot else [], # Last snapshot
            "anomaly_score": event.anomaly_score,
            "timestamp": event.detection_timestamp.isoformat()
        }

        # Generate analysis and suggestions
        analysis_res = await gemini_service_instance.analyze_anomaly(anomaly_context)
        remediation_res = await gemini_service_instance.suggest_remediation(anomaly_context)

        # Extract suggestions safely
        suggestions = []
        if remediation_res and "error" not in remediation_res and "steps" in remediation_res:
            suggestions = remediation_res["steps"]

        # Update the event
        update_data = {
            "ai_analysis": analysis_res if analysis_res and "error" not in analysis_res else event.ai_analysis,
            "suggested_remediation_commands": suggestions if suggestions else event.suggested_remediation_commands
        }
        # Import the update model
        from app.models.anomaly_event import AnomalyEventUpdate
        updated_event = await event_service.update_event(anomaly_id, AnomalyEventUpdate(**update_data))

        return {
            "status": "success",
            "message": "Anomaly analysis completed.",
            "analysis": update_data["ai_analysis"],
            "suggestions": update_data["suggested_remediation_commands"],
            "event_id": anomaly_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing anomaly {anomaly_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to analyze anomaly: {str(e)}")

# Removed the duplicate "/train" endpoint - model training is now handled exclusively in metrics_router.py
