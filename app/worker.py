import asyncio
from loguru import logger
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

from app.core.config import settings
from app.utils.prometheus_scraper import prometheus_scraper
from app.models.anomaly_detector import anomaly_detector
from app.services.gemini_service import GeminiService
from app.services.anomaly_event_service import AnomalyEventService
from app.services.mode_service import mode_service
from app.utils.k8s_executor import k8s_executor
from app.models.anomaly_event import AnomalyStatus, RemediationAttempt, AnomalyEvent, AnomalyEventUpdate


class AutonomousWorker:
    """
    Background worker that continuously monitors Kubernetes metrics,
    detects anomalies, and manages the remediation process.
    """

    def __init__(self, anomaly_event_svc: AnomalyEventService, gemini_svc: Optional[GeminiService]):
        """
        Initialize the autonomous worker.

        Args:
            anomaly_event_svc: Service for managing anomaly events
            gemini_svc: Optional service for AI-powered analysis and remediation
        """
        self.anomaly_event_service = anomaly_event_svc
        self.gemini_service = gemini_svc  # Can be None
        self.entity_metric_history = {}
        self.history_limit = settings.ANOMALY_METRIC_WINDOW_SECONDS // settings.PROMETHEUS_SCRAPE_INTERVAL_SECONDS + 1
        self.active_promql_queries: List[str] = list(settings.DEFAULT_PROMQL_QUERIES)  # Start with defaults
        self.is_initialized = False
        logger.info("AutonomousWorker initialized.")

    async def _initialize_queries(self):
        """
        Attempt to generate PromQL queries using Gemini on first run.
        Falls back to default queries if Gemini is unavailable or generation fails.
        """
        if settings.GEMINI_AUTO_QUERY_GENERATION and self.gemini_service:
            logger.info("Attempting to generate PromQL queries using Gemini...")
            try:
                cluster_context = await k8s_executor.get_cluster_context_for_promql()
                if not cluster_context.get("nodes") and not cluster_context.get("workloads"):
                     logger.warning("Could not get sufficient cluster context, using default queries.")
                     return  # Use defaults if context is empty

                query_gen_result = await self.gemini_service.generate_promql_queries(cluster_context)

                if query_gen_result and "queries" in query_gen_result and query_gen_result["queries"]:
                    self.active_promql_queries = query_gen_result["queries"]
                    logger.info(f"Successfully generated {len(self.active_promql_queries)} PromQL queries using Gemini.")
                    logger.debug(f"Generated queries: {self.active_promql_queries}")
                    logger.info(f"Gemini Reasoning for queries: {query_gen_result.get('reasoning', 'N/A')}")
                else:
                    logger.warning(f"Failed to generate PromQL queries using Gemini ({query_gen_result.get('error', 'Unknown error')}). Falling back to default queries.")
            except Exception as e:
                logger.error(f"Error during Gemini PromQL query generation: {e}. Using default queries.", exc_info=True)
        else:
            logger.info("Using default PromQL queries (Gemini generation disabled or unavailable).")
        self.is_initialized = True

    async def _update_metric_history(self, entity_key: str, current_metrics: List[Dict[str, Any]]):
        """
        Updates the history of metrics for a given entity.

        Args:
            entity_key: Unique identifier for the entity
            current_metrics: Current metrics snapshot for the entity
        """
        if entity_key not in self.entity_metric_history:
            self.entity_metric_history[entity_key] = []

        # Add the current metrics to history and maintain limited window
        self.entity_metric_history[entity_key].append(current_metrics)

        # Trim history to limit
        if len(self.entity_metric_history[entity_key]) > self.history_limit:
            self.entity_metric_history[entity_key] = self.entity_metric_history[entity_key][-self.history_limit:]

    async def _process_entity_metrics(self, entity_key: str, entity_data: Dict[str, Any]):
        """
        Processes metrics for an entity, detecting anomalies and handling them if needed.

        Args:
            entity_key: Unique identifier for the entity
            entity_data: Entity data containing metrics and metadata
        """
        if not entity_data.get("metrics"):
            logger.warning(f"No metrics available for entity: {entity_key}")
            return

        # Update metric history for this entity
        await self._update_metric_history(entity_key, entity_data["metrics"])

        # Only predict anomalies if we have enough history
        if len(self.entity_metric_history[entity_key]) >= 1:
            await self._predict_and_handle_anomaly(
                entity_key, entity_data, self.entity_metric_history[entity_key]
            )

    async def _predict_and_handle_anomaly(self, entity_key: str, entity_data: Dict[str, Any], metric_window: List[List[Dict[str, Any]]]):
        """
        Predicts anomaly and handles it if detected.

        Args:
            entity_key: Unique identifier for the entity
            entity_data: Entity data containing metrics and metadata
            metric_window: Window of historical metrics for the entity
        """
        try:
            # Get the latest metrics snapshot for prediction
            latest_metrics = metric_window[-1] if metric_window else []

            # Predict anomaly using the model
            is_anomaly, score = await anomaly_detector.predict(latest_metrics)

            # If anomaly is detected
            if is_anomaly:
                logger.warning(f"Anomaly DETECTED for entity {entity_key} with score {score:.2f}")

                # First check if there's already an active event for this entity
                active_event = await self.anomaly_event_service.find_active_event(
                    entity_id=entity_data["entity_id"],
                    entity_type=entity_data["entity_type"],
                    namespace=entity_data["namespace"]
                )

                if active_event:
                    logger.info(f"Active event already exists for {entity_key}: {active_event.anomaly_id}")
                    return

                # Create new anomaly event
                event_data = {
                    "entity_id": entity_data["entity_id"],
                    "entity_type": entity_data["entity_type"],
                    "namespace": entity_data["namespace"],
                    "metric_snapshot": metric_window,
                    "anomaly_score": score,
                    "status": AnomalyStatus.DETECTED
                }

                # Process with Gemini if available
                remediation_suggestions_raw = []
                if self.gemini_service and settings.GEMINI_AUTO_ANALYSIS:
                    # Prepare context for Gemini
                    anomaly_context = {
                        "entity_id": entity_data["entity_id"],
                        "entity_type": entity_data["entity_type"],
                        "namespace": entity_data["namespace"],
                        "metrics_snapshot": latest_metrics,
                        "anomaly_score": score,
                        "timestamp": datetime.utcnow().isoformat()
                    }

                    try:
                        # Get analysis and remediation suggestions
                        analysis_res = await self.gemini_service.analyze_anomaly(anomaly_context)
                        if analysis_res and "error" not in analysis_res:
                            event_data["ai_analysis"] = analysis_res
                            logger.info(f"Gemini analysis completed for {entity_key}")

                        remediation_res = await self.gemini_service.suggest_remediation(anomaly_context)
                        if remediation_res and "error" not in remediation_res and "steps" in remediation_res:
                            remediation_suggestions_raw = remediation_res["steps"]
                            event_data["suggested_remediation_commands"] = remediation_suggestions_raw
                            event_data["status"] = AnomalyStatus.REMEDIATION_SUGGESTED
                            logger.info(f"Gemini remediation suggestions generated for {entity_key}")
                    except Exception as e:
                        logger.error(f"Error in Gemini processing: {e}")

                # Create event and handle remediation
                new_event = await self.anomaly_event_service.create_event(event_data)
                if new_event:
                    await self._decide_remediation(new_event, remediation_suggestions_raw)
        except Exception as e:
            logger.error(f"Error predicting anomaly for entity {entity_key}: {e}", exc_info=True)

    async def _decide_remediation(self, event: AnomalyEvent, ai_suggestions: List[str]):
        """
        Decides and potentially executes remediation actions for an anomaly event.

        Args:
            event: The anomaly event to remediate
            ai_suggestions: AI-generated remediation suggestions
        """
        current_mode = mode_service.get_mode()
        logger.info(f"Deciding remediation for event {event.anomaly_id} ({event.entity_id}). Mode: {current_mode}")

        all_suggestions = list(set(ai_suggestions))  # Use set to deduplicate

        validated_safe_commands = []
        validated_params_map = {}  # Store parsed params for execution
        for cmd_str in all_suggestions:
            validation_result = k8s_executor.parse_and_validate_command(cmd_str)
            if validation_result:
                op_name, params = validation_result
                validated_safe_commands.append(cmd_str)
                validated_params_map[cmd_str] = (op_name, params)
            else:
                logger.warning(f"Suggested command deemed unsafe or invalid for event {event.anomaly_id}: {cmd_str}")

        # Update event with validated suggestions regardless of mode
        await self.anomaly_event_service.update_event(
            event.anomaly_id,
            AnomalyEventUpdate(
                suggested_remediation_commands=validated_safe_commands
            )
        )

        # In AUTO mode, execute the first validated command
        if current_mode == "AUTO" and validated_safe_commands:
            selected_cmd = validated_safe_commands[0]
            operation, params = validated_params_map[selected_cmd]

            logger.info(f"AUTO mode - executing remediation command: {selected_cmd}")

            try:
                result = await k8s_executor.execute_validated_command(operation, params)

                # Record remediation attempt
                attempt = RemediationAttempt(
                    command=selected_cmd,
                    parameters=params,
                    executor="AUTO",
                    success=True,
                    result=result
                )

                # Schedule verification after remediation
                verification_time = datetime.utcnow() + timedelta(seconds=settings.VERIFICATION_DELAY_SECONDS)

                # Update event with remediation attempt
                await self.anomaly_event_service.update_event(
                    event.anomaly_id,
                    AnomalyEventUpdate(
                        status=AnomalyStatus.VERIFICATION_PENDING,
                        remediation_attempts=[attempt],
                        verification_time=verification_time
                    )
                )
                logger.info(f"Remediation applied to event {event.anomaly_id}. Verification scheduled at {verification_time}")

            except Exception as e:
                logger.error(f"Error executing remediation for event {event.anomaly_id}: {e}")
                # Record failed attempt
                attempt = RemediationAttempt(
                    command=selected_cmd,
                    parameters=params,
                    executor="AUTO",
                    success=False,
                    error=str(e)
                )
                await self.anomaly_event_service.update_event(
                    event.anomaly_id,
                    AnomalyEventUpdate(remediation_attempts=[attempt])
                )
        elif current_mode == "AUTO" and not validated_safe_commands:
            logger.warning(f"AUTO mode active but no valid commands for event {event.anomaly_id}")
        else:
            logger.info(f"MANUAL mode - no automatic remediation for event {event.anomaly_id}")

    async def _verify_remediation(self, event: AnomalyEvent):
        """
        Verify if remediation was successful using Gemini if enabled.

        Args:
            event: The anomaly event to verify
        """
        logger.info(f"Verifying remediation for event {event.anomaly_id} ({event.entity_id})")
        verification_notes = f"Verification check at {datetime.utcnow().isoformat()}. "
        final_status = AnomalyStatus.REMEDIATION_FAILED  # Default to failed

        try:
            # 1. Fetch current metrics for the entity
            entity_key = f"{event.entity_type}/{event.entity_id}"
            if event.namespace: entity_key = f"{event.namespace}/{entity_key}"

            # Fetch all metrics and filter for this entity
            all_current_metrics = await prometheus_scraper.fetch_all_metrics(self.active_promql_queries)
            current_entity_data = all_current_metrics.get(entity_key)
            current_metrics_snapshot = current_entity_data['metrics'] if current_entity_data else []

            if not current_metrics_snapshot:
                logger.warning(f"Verification failed for {event.anomaly_id}: Could not fetch current metrics.")
                verification_notes += "Could not fetch current metrics for verification."
                # Keep status as failed on no metrics

            # Get K8s status for the entity if possible
            current_k8s_status = {}
            try:
                if event.entity_type and event.entity_id:
                    current_k8s_status = await k8s_executor.get_resource_status(
                        resource_type=event.entity_type,
                        name=event.entity_id,
                        namespace=event.namespace or "default"
                    )
            except Exception as e:
                logger.warning(f"Could not fetch K8s status during verification: {e}")
                verification_notes += f"K8s status fetch error: {str(e)}. "

            # 2. Determine verification method
            if self.gemini_service and settings.GEMINI_AUTO_VERIFICATION:
                # --- AI-Powered Verification ---
                logger.debug(f"Using Gemini for verification of {event.anomaly_id}")

                # Get the last remediation attempt
                if not event.remediation_attempts:
                    verification_notes += "No remediation attempt details found for AI verification."
                else:
                    last_attempt = event.remediation_attempts[-1]
                    verify_result = await self.gemini_service.verify_remediation_success(
                        anomaly_event=event.model_dump(mode='json'),  # Pass event as dict
                        remediation_attempt=last_attempt.model_dump(mode='json'),  # Pass attempt as dict
                        current_metrics=current_metrics_snapshot,
                        current_k8s_status=current_k8s_status
                    )

                    if verify_result and "error" not in verify_result:
                        is_resolved = verify_result.get("success", False)
                        reason = verify_result.get("reasoning", "No reasoning provided.")
                        confidence = verify_result.get("confidence", 0.0)
                        verification_notes += f"AI Assessment (Confidence: {confidence:.2f}): {reason}"
                        final_status = AnomalyStatus.RESOLVED if is_resolved else AnomalyStatus.REMEDIATION_FAILED
                        logger.info(f"Gemini verification for {event.anomaly_id}: Success={is_resolved}. Reason: {reason}")
                    else:
                         logger.error(f"Gemini verification API call failed for {event.anomaly_id}: {verify_result.get('error')}")
                         verification_notes += f"AI verification failed: {verify_result.get('error')}"
                         # Keep status as failed on API error

            else:
                # --- Fallback Basic Verification (if Gemini disabled/failed) ---
                logger.debug(f"Using basic verification for {event.anomaly_id}")
                is_currently_anomaly, current_score = await anomaly_detector.predict(current_metrics_snapshot)
                if not is_currently_anomaly:
                    final_status = AnomalyStatus.RESOLVED
                    verification_notes += f"Basic verification: Entity is no longer anomalous. Current score: {current_score:.2f}"
                    logger.info(f"Basic verification success for {event.anomaly_id}: score {current_score:.2f}")
                else:
                    verification_notes += f"Basic verification: Entity is still anomalous. Current score: {current_score:.2f}"
                    logger.warning(f"Basic verification failed for {event.anomaly_id}: score still anomalous {current_score:.2f}")

        except Exception as e:
            logger.error(f"Error during verification of event {event.anomaly_id}: {e}", exc_info=True)
            verification_notes += f"Error during verification: {str(e)}"
            # Keep status as failed on exception

        # Update the event with verification results
        await self.anomaly_event_service.update_event(
            event.anomaly_id,
            AnomalyEventUpdate(
                status=final_status,
                notes=verification_notes,
                resolution_time=datetime.utcnow() if final_status == AnomalyStatus.RESOLVED else None
            )
        )

    async def run_cycle(self):
        """Runs a single cycle of fetch, detect, remediate, verify."""
        logger.info(f"Starting worker cycle... Mode: {mode_service.get_mode()}")

        if not self.is_initialized:
            await self._initialize_queries()  # Try to generate queries on first run

        try:
            # 1. Fetch Metrics using current queries
            all_metrics = await prometheus_scraper.fetch_all_metrics(self.active_promql_queries)

            # 2. Process Metrics & Detect/Handle Anomalies for each entity
            process_tasks = [
                self._process_entity_metrics(key, data)
                for key, data in all_metrics.items()
            ]
            await asyncio.gather(*process_tasks, return_exceptions=True)  # Log exceptions if gather fails

            # 3. Check for Events Pending Verification
            events_to_verify = await self.anomaly_event_service.list_events(status=AnomalyStatus.VERIFICATION_PENDING)
            if events_to_verify:
                verification_time_threshold = datetime.utcnow() - timedelta(seconds=settings.VERIFICATION_DELAY_SECONDS)
                events_to_verify = [
                    e for e in events_to_verify
                    if e.verification_time and e.verification_time <= datetime.utcnow()
                ]

                if events_to_verify:
                    logger.info(f"Found {len(events_to_verify)} events pending verification.")
                    verification_tasks = [self._verify_remediation(event) for event in events_to_verify]
                    await asyncio.gather(*verification_tasks, return_exceptions=True)  # Log exceptions

        except Exception as e:
            logger.error(f"CRITICAL Error in worker cycle: {e}", exc_info=True)

        logger.info("Worker cycle finished.")

    async def run(self):
        """The main continuous loop for the worker."""
        logger.info("AutonomousWorker starting run loop...")
        while True:
            await self.run_cycle()
            logger.debug(f"Worker sleeping for {settings.WORKER_SLEEP_INTERVAL_SECONDS} seconds...")
            await asyncio.sleep(settings.WORKER_SLEEP_INTERVAL_SECONDS)
