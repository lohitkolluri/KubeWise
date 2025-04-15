from fastapi import APIRouter, Depends, HTTPException, Body
from app.models.anomaly_detector import anomaly_detector
from app.services.anomaly_event_service import AnomalyEventService
from app.services.gemini_service import GeminiService
from app.core.dependencies.service_factory import get_gemini_service, get_anomaly_event_service
from app.core.config import settings
from app.utils.prometheus_scraper import prometheus_scraper
from loguru import logger
from typing import Dict, Any, List, Optional
from datetime import datetime
from app.models.anomaly_event import AnomalyEventUpdate

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
    event_service: AnomalyEventService = Depends(get_anomaly_event_service),
    gemini_service: GeminiService = Depends(get_gemini_service)
):
    """
    Analyze a detected anomaly using Gemini AI.
    Requires Gemini Service to be available.

    Parameters:
        anomaly_id (str): ID of the anomaly event to analyze
        event_service: Service for anomaly event management (injected)
        gemini_service: Service for Gemini AI integration (injected)

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
        # Check if Gemini API key is missing
        api_key_missing = False
        if gemini_service and gemini_service.is_api_key_missing():
            api_key_missing = True
            logger.warning("Gemini API Key is missing. Analysis will be limited.")
        elif not gemini_service:
            raise HTTPException(status_code=503, detail="Gemini service is not configured or available.")

        event = await event_service.get_event(anomaly_id)
        if not event:
            raise HTTPException(status_code=404, detail=f"Anomaly event not found: {anomaly_id}")

        # Prepare context for Gemini
        anomaly_context = {
            "entity_id": event.entity_id,
            "entity_type": event.entity_type,
            "namespace": event.namespace,
            "metrics_snapshot": event.metric_snapshot if isinstance(event.metric_snapshot, list) else [], # Ensure it's a list
            "anomaly_score": event.anomaly_score,
            "timestamp": event.detection_timestamp.isoformat(),
            "prediction_data": event.prediction_data if event.prediction_data else None  # Handle null case
        }

        # Generate analysis and suggestions
        analysis_res = None
        remediation_res = None

        if not api_key_missing:
            # Try the new batch processing method first for efficiency
            batch_result = await gemini_service.batch_process_anomaly(anomaly_context)

            if batch_result and "error" not in batch_result:
                # Extract info from batch processing
                analysis_res = batch_result.get("analysis")

                # Extract suggestions from batch processing
                suggestions = batch_result.get("remediation", {}).get("steps", [])
            else:
                # Fallback to separate calls if batch processing failed
                logger.info("Batch processing failed, falling back to separate API calls")
                analysis_res = await gemini_service.analyze_anomaly(anomaly_context)
                remediation_res = await gemini_service.suggest_remediation(anomaly_context)

                # Extract suggestions safely from separate call
                suggestions = []
                if remediation_res and "error" not in remediation_res and "steps" in remediation_res:
                    suggestions = remediation_res["steps"]

        # Update the event
        update_data = {
            "ai_analysis": analysis_res if analysis_res and "error" not in analysis_res else event.ai_analysis,
            "suggested_remediation_commands": suggestions if suggestions else event.suggested_remediation_commands
        }

        updated_event = await event_service.update_event(anomaly_id, AnomalyEventUpdate(**update_data))

        # For the response, provide clear feedback on API key status
        if api_key_missing:
            response_message = "Anomaly analysis not available: Gemini API Key is not configured"
        else:
            response_message = "Anomaly analysis completed."

        return {
            "status": "success",
            "message": response_message,
            "analysis": update_data["ai_analysis"],
            "suggestions": update_data["suggested_remediation_commands"],
            "event_id": anomaly_id,
            "api_key_required": api_key_missing
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing anomaly {anomaly_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to analyze anomaly: {str(e)}")

# Removed the duplicate "/train" endpoint - model training is now handled exclusively in metrics_router.py
