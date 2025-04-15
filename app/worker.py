import asyncio
from loguru import logger
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import json

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
        self.last_cleanup_time = datetime.utcnow()
        # Set cleanup interval - default to every 30 minutes if not in settings
        self.cleanup_interval_seconds = getattr(settings, 'METRIC_HISTORY_CLEANUP_INTERVAL_SECONDS', 1800)
        # Set stale entity timeout - default to 30 minutes if not in settings
        self.stale_entity_timeout_seconds = getattr(settings, 'STALE_ENTITY_TIMEOUT_SECONDS', 1800)
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

        # Check if model needs retraining
        if anomaly_detector.needs_retraining:
            # Only log retraining attempt once per cycle
            if not hasattr(self, '_retraining_attempted'):
                logger.info("Model needs retraining, collecting training data...")
                self._retraining_attempted = True

            # Collect training data from all entities with sufficient history
            training_data = []
            for entity_metrics in self.entity_metric_history.values():
                if len(entity_metrics) >= 2:  # Only use entities with at least 2 data points
                    training_data.extend(entity_metrics)

            if len(training_data) >= 5:  # Require at least 5 data points for training
                logger.info(f"Retraining model with {len(training_data)} data points")
                retrained = await anomaly_detector.retrain_if_needed(training_data)
                if not retrained:
                    logger.warning("Model retraining failed, skipping prediction")
                    return
            else:
                # Only log insufficient data warning once per cycle
                if not hasattr(self, '_insufficient_data_warned'):
                    logger.warning(f"Insufficient training data: {len(training_data)} points available, need at least 5")
                    self._insufficient_data_warned = True
                return

        # Only predict anomalies if we have enough history
        if len(self.entity_metric_history[entity_key]) >= 2:  # Require at least 2 data points for prediction
            await self._predict_and_handle_anomaly(
                entity_key, entity_data, self.entity_metric_history[entity_key]
            )

    async def _predict_and_handle_anomaly(self, entity_key: str, entity_data: Dict[str, Any], metric_window: List[List[Dict[str, Any]]]):
        """
        Predicts anomalies/failures and handles them appropriately.

        Args:
            entity_key: Unique identifier for the entity
            entity_data: Entity data containing metrics and metadata
            metric_window: Window of historical metrics for the entity
        """
        try:
            # Get the latest metrics snapshot for prediction
            latest_metrics = metric_window[-1] if metric_window else []

            # Use the enhanced prediction with forecasting capability
            prediction_result = await anomaly_detector.predict_with_forecast(
                latest_metrics_snapshot=latest_metrics,
                historical_snapshots=metric_window[:-1] if len(metric_window) > 1 else None
            )

            # Extract prediction results
            is_anomaly = prediction_result.get("is_anomaly", False)
            is_critical = prediction_result.get("is_critical", False)
            anomaly_score = prediction_result.get("anomaly_score", 0.0)
            has_forecast = prediction_result.get("forecast_available", False)

            # Determine if there are predicted failures coming soon
            predicted_failures = prediction_result.get("predicted_failures", [])
            imminent_failure = any(
                failure.get("minutes_until_failure", 9999) < 30 and failure.get("confidence", 0) > 0.6
                for failure in predicted_failures
            )

            # ENHANCEMENT: Directly check for threshold breaches and actual failures in the metrics
            # Initialize our direct detection flags
            has_threshold_breach = False
            has_actual_failure = False
            failure_details = []
            threshold_details = []
            pod_state_issues = []

            # Define critical thresholds for different metrics
            critical_thresholds = {
                "cpu_usage_percent": 90.0,  # 90% CPU usage
                "memory_usage_percent": 90.0,  # 90% memory usage
                "disk_usage_percent": 85.0,  # 85% disk usage
                "pod_status_ready": 0.0,  # Not ready
                "pod_status_phase": 0.0,  # Not running (0 = failed/pending)
                "container_restarts": 3.0,  # More than 3 restarts recently
                "pod_restarts": 3.0,  # More than 3 pod restarts
                "pod_status_condition": 0.0,  # Failed status
                "deployment_available_replicas": 0.0,  # 0 available replicas
                "deployment_unavailable_replicas": 0.0,  # Any unavailable replicas (non-zero means problem)
                "node_condition": 0.0,  # Node condition problem (Ready=0, Others=1)
                "pv_status_phase": 0.0,  # PV not available
                "job_failed": 1.0,  # Job failure
                "unbound_pvc": 1.0,  # Unbound PVCs
                "pending_pods": 1.0,  # Pending pods
            }

            # Direct failure indicators in metrics
            failure_indicators = {
                "pod_status_phase": ["Failed", "Unknown", "CrashLoopBackOff"],
                "node_status": ["NotReady", "SchedulingDisabled"],
                "container_status": ["Terminated", "Waiting"],
                "pod_condition": ["PodScheduled=False", "ContainersReady=False", "Ready=False"],
                "event_type": ["Warning", "Error", "Fatal"],
                "deployment_status": ["Degraded", "Stalled"],
            }

            # Pod waiting reason indicators - these indicate problematic pod states
            pod_waiting_reasons = [
                "CrashLoopBackOff",
                "ImagePullBackOff",
                "ErrImagePull",
                "CreateContainerError",
                "CreateContainerConfigError",
                "ContainerCreating",
                "PodInitializing",
                "ContainerCannotRun"
            ]

            # Examine the latest metrics for threshold breaches and failures
            for metric in latest_metrics:
                metric_name = metric.get("metric", "")
                metric_value = metric.get("value", 0.0)
                metric_labels = metric.get("labels", {})

                # Enhanced check for pod state issues through container waiting reasons
                if "kube_pod_container_status_waiting_reason" in metric_name:
                    reason = metric_labels.get("reason", "")
                    if reason in pod_waiting_reasons and str(metric_value) == "1":
                        has_actual_failure = True
                        pod_state_issues.append(f"Container in pod {metric_labels.get('pod', 'unknown')} is waiting: {reason}")
                        logger.warning(f"Pod state issue detected in {entity_key}: {reason}")

                # Check for restarts count exceeding threshold
                if "kube_pod_container_status_restarts_total" in metric_name:
                    try:
                        restart_count = float(metric_value)
                        if restart_count >= 3:  # Threshold for restart count
                            has_actual_failure = True
                            failure_details.append(f"Container in pod {metric_labels.get('pod', 'unknown')} has restarted {int(restart_count)} times")
                            logger.warning(f"Excessive restarts detected in {entity_key}: {int(restart_count)} restarts")
                    except (ValueError, TypeError):
                        pass

                # Check for direct failure indicators in string-based metrics
                for indicator_key, failure_strings in failure_indicators.items():
                    if indicator_key in metric_name.lower():
                        metric_str_value = str(metric_value).lower()
                        for failure_str in failure_strings:
                            if failure_str.lower() in metric_str_value:
                                has_actual_failure = True
                                failure_details.append(f"{metric_name} has failure status: {metric_value}")
                                logger.warning(f"Direct failure detected in {entity_key}: {metric_name}={metric_value}")

                # Check for threshold breaches in numerical metrics
                for threshold_key, threshold_value in critical_thresholds.items():
                    if threshold_key in metric_name.lower():
                        try:
                            # Convert to float for comparison if it's a string
                            num_value = float(metric_value) if isinstance(metric_value, (int, float, str)) else 0.0

                            # Determine which comparison to use (some metrics are low=bad, some high=bad)
                            if threshold_key in ['pod_status_ready', 'pod_status_phase', 'deployment_available_replicas', 'node_condition', 'pv_status_phase']:
                                # For these metrics, lower values are bad (below threshold)
                                if num_value <= threshold_value:
                                    has_threshold_breach = True
                                    threshold_details.append(f"{metric_name} value {num_value} at or below critical threshold {threshold_value}")
                                    logger.warning(f"Threshold breach in {entity_key}: {metric_name}={num_value} (threshold={threshold_value})")
                            elif threshold_key in ['deployment_unavailable_replicas', 'container_restarts', 'pod_restarts',
                                                 'job_failed', 'unbound_pvc', 'pending_pods']:
                                # For these metrics, higher values are bad (above threshold)
                                if num_value >= threshold_value and threshold_value > 0:
                                    has_threshold_breach = True
                                    threshold_details.append(f"{metric_name} value {num_value} at or above critical threshold {threshold_value}")
                                    logger.warning(f"Threshold breach in {entity_key}: {metric_name}={num_value} (threshold={threshold_value})")
                            else:
                                # For most metrics, higher values are bad (above threshold)
                                if num_value >= threshold_value:
                                    has_threshold_breach = True
                                    threshold_details.append(f"{metric_name} value {num_value} at or above critical threshold {threshold_value}")
                                    logger.warning(f"Threshold breach in {entity_key}: {metric_name}={num_value} (threshold={threshold_value})")
                        except (ValueError, TypeError):
                            # Skip if we can't convert the value to a number
                            continue

            # Check for pod phase issues (Pending, Failed, Unknown)
            for metric in latest_metrics:
                if "kube_pod_status_phase" in metric.get("metric", ""):
                    metric_labels = metric.get("labels", {})
                    phase = metric_labels.get("phase", "")
                    if phase in ["Pending", "Failed", "Unknown"] and str(metric.get("value", "0")) == "1":
                        has_actual_failure = True
                        pod_state_issues.append(f"Pod {metric_labels.get('pod', 'unknown')} is in {phase} phase")
                        logger.warning(f"Pod {metric_labels.get('pod', 'unknown')} in problematic phase: {phase}")

            # Update detection flags based on direct checks
            is_critical = is_critical or has_actual_failure
            is_anomaly = is_anomaly or has_threshold_breach or bool(pod_state_issues)

            # Determine if action is needed based on any detection method
            should_take_action = is_anomaly or is_critical or imminent_failure

            if should_take_action:
                action_reason = "UNKNOWN"
                if has_actual_failure:
                    action_reason = "ACTUAL FAILURE"
                elif is_critical:
                    action_reason = "CRITICAL FAILURE"
                elif bool(pod_state_issues):
                    action_reason = "POD STATE ISSUE"
                elif has_threshold_breach:
                    action_reason = "THRESHOLD BREACH"
                elif imminent_failure:
                    action_reason = "IMMINENT FAILURE"
                elif is_anomaly:
                    action_reason = "ANOMALY"

                logger.warning(f"{action_reason} DETECTED for entity {entity_key} with score {anomaly_score:.2f}")

                # Log specific details about thresholds, pod states, and failures if available
                if threshold_details:
                    logger.warning(f"Threshold breaches for {entity_key}: {', '.join(threshold_details)}")

                if pod_state_issues:
                    logger.warning(f"Pod state issues for {entity_key}: {', '.join(pod_state_issues)}")

                if failure_details:
                    logger.warning(f"Failures detected for {entity_key}: {', '.join(failure_details)}")

                if imminent_failure and not has_actual_failure and not has_threshold_breach and not pod_state_issues:
                    # Extract details about the predicted failures for logging
                    prediction_details = [
                        f"{f.get('metric', 'unknown')} expected to cross {f.get('threshold', 'threshold')} "
                        f"in {f.get('minutes_until_failure', 'unknown')} minutes "
                        f"(confidence: {f.get('confidence', 0):.2f})"
                        for f in predicted_failures if f.get('minutes_until_failure', 9999) < 30
                    ]

                    logger.warning(f"Predicted imminent failures for {entity_key}: {', '.join(prediction_details)}")

                # First check if there's already an active event for this entity
                active_event = await self.anomaly_event_service.find_active_event(
                    entity_id=entity_data["entity_id"],
                    entity_type=entity_data["entity_type"],
                    namespace=entity_data["namespace"]
                )

                if active_event:
                    logger.info(f"Active event already exists for {entity_key}: {active_event.anomaly_id}")
                    return

                # Create appropriate status based on the detection method
                event_status = AnomalyStatus.DETECTED
                if has_actual_failure:
                    event_status = AnomalyStatus.FAILURE_DETECTED
                elif is_critical:
                    event_status = AnomalyStatus.CRITICAL_FAILURE
                elif bool(pod_state_issues):
                    event_status = AnomalyStatus.POD_STATE_ISSUE
                elif has_threshold_breach:
                    event_status = AnomalyStatus.THRESHOLD_BREACH
                elif imminent_failure and not is_anomaly:
                    event_status = AnomalyStatus.PREDICTED_FAILURE

                # Create event data
                event_data = {
                    "entity_id": entity_data["entity_id"],
                    "entity_type": entity_data["entity_type"],
                    "namespace": entity_data["namespace"],
                    "metric_snapshot": metric_window,
                    "anomaly_score": anomaly_score,
                    "status": event_status
                }

                # Add notes about direct threshold checks, pod states, and failures
                notes = []
                if threshold_details:
                    notes.append(f"Threshold breaches: {'; '.join(threshold_details)}")
                if pod_state_issues:
                    notes.append(f"Pod state issues: {'; '.join(pod_state_issues)}")
                if failure_details:
                    notes.append(f"Failures detected: {'; '.join(failure_details)}")
                if notes:
                    event_data["notes"] = " ".join(notes)

                # Add prediction data if available
                if has_forecast:
                    event_data["prediction_data"] = {
                        "predicted_failures": predicted_failures,
                        "forecast": prediction_result.get("forecast", {}),
                        "recommendation": prediction_result.get("recommendation", "monitor")
                    }

                # Process with Gemini if available
                remediation_suggestions_raw = []
                if self.gemini_service and settings.GEMINI_AUTO_ANALYSIS:
                    try:
                        # Prepare context for Gemini
                        anomaly_context = {
                            "entity_id": entity_data["entity_id"],
                            "entity_type": entity_data["entity_type"],
                            "namespace": entity_data["namespace"],
                            "metrics_snapshot": latest_metrics,
                            "anomaly_score": anomaly_score,
                            "is_critical": is_critical or has_actual_failure,
                            "is_predicted": imminent_failure and not (is_anomaly or has_threshold_breach or has_actual_failure),
                            "predicted_failures": predicted_failures if has_forecast else [],
                            "threshold_breaches": threshold_details if threshold_details else [],
                            "pod_state_issues": pod_state_issues if pod_state_issues else [],
                            "failure_details": failure_details if failure_details else [],
                            "timestamp": datetime.utcnow().isoformat()
                        }

                        # Get analysis and remediation suggestions
                        analysis_res = await self.gemini_service.analyze_anomaly(anomaly_context)
                        if analysis_res and "error" not in analysis_res:
                            event_data["ai_analysis"] = analysis_res
                            logger.info(f"Gemini analysis completed for {entity_key}")

                        # Get remediation suggestions with priority for critical failures
                        remediation_res = await self.gemini_service.suggest_remediation(
                            anomaly_context,
                            prioritize_critical=is_critical or has_actual_failure or has_threshold_breach or imminent_failure or bool(pod_state_issues)
                        )
                        if remediation_res and "error" not in remediation_res and "steps" in remediation_res:
                            remediation_suggestions_raw = remediation_res["steps"]
                            event_data["suggested_remediation_commands"] = remediation_suggestions_raw
                            event_data["status"] = AnomalyStatus.REMEDIATION_SUGGESTED

                    except Exception as e:
                        logger.error(f"Error in Gemini processing: {e}")
                        # For critical failures, we might want to trigger fallback remediation

                # Create event and handle remediation
                new_event = await self.anomaly_event_service.create_event(event_data)
                if new_event:
                    await self._decide_remediation(new_event, remediation_suggestions_raw,
                                                  is_critical=is_critical or has_actual_failure or has_threshold_breach,
                                                  is_predicted=imminent_failure and not (is_anomaly or has_threshold_breach or has_actual_failure))

        except Exception as e:
            logger.error(f"Error predicting anomaly for entity {entity_key}: {e}", exc_info=True)

    async def _decide_remediation(self, event: AnomalyEvent, ai_suggestions: List[str],
                                 is_critical: bool = False, is_predicted: bool = False,
                                 is_direct_failure: bool = False):
        """
        Decides and potentially executes remediation actions for an anomaly event.

        Args:
            event: The anomaly event to remediate
            ai_suggestions: AI-generated remediation suggestions
            is_critical: Whether this is a critical failure requiring immediate action
            is_predicted: Whether this is a predicted future failure
            is_direct_failure: Whether this is a directly detected failure (not just an anomaly)
        """
        current_mode = mode_service.get_mode()
        logger.info(f"Deciding remediation for event {event.anomaly_id} ({event.entity_id}). "
                   f"Mode: {current_mode}, Critical: {is_critical}, Predicted: {is_predicted}, Direct: {is_direct_failure}")

        # Start with AI suggestions, or use fallback commands if none available
        all_suggestions = list(set(ai_suggestions))  # Use set to deduplicate

        # Add fallback remediation commands if no AI suggestions available
        # and in AUTO mode, or critical failure, or direct failure detection
        if not all_suggestions and (current_mode == "AUTO" or is_critical or is_direct_failure):
            entity_type = event.entity_type.lower()

            # Different remediation strategies based on resource type and failure type
            if entity_type == "pod":
                if is_direct_failure and "ImagePullBackOff" in event.notes:
                    # For image pull issues, deleting the pod may be better than restarting
                    all_suggestions.append(f"delete_pod namespace={event.namespace} pod_name={event.entity_id}")
                elif is_direct_failure and "CrashLoopBackOff" in event.notes:
                    # For crash loops, consider more aggressive action
                    all_suggestions.append(f"delete_pod namespace={event.namespace} pod_name={event.entity_id}")
                    # Also add scaling command for the parent deployment if we can determine it
                    if event.namespace:
                        all_suggestions.append(f"scale_parent_controller namespace={event.namespace} pod_name={event.entity_id} delta=+1")
                else:
                    # Standard restart for other pod issues
                    all_suggestions.append(f"restart_pod namespace={event.namespace} pod_name={event.entity_id}")

            elif entity_type == "deployment":
                if is_direct_failure:
                    # For directly detected deployment failures, both restart and scale up
                    all_suggestions.append(f"restart_deployment name={event.entity_id} namespace={event.namespace}")
                    all_suggestions.append(f"scale_deployment name={event.entity_id} namespace={event.namespace} replicas=+1")
                else:
                    # For anomalies/predictions, just restart
                    all_suggestions.append(f"restart_deployment name={event.entity_id} namespace={event.namespace}")

            elif entity_type == "statefulset":
                all_suggestions.append(f"restart_statefulset name={event.entity_id} namespace={event.namespace}")

            elif entity_type == "daemonset":
                all_suggestions.append(f"restart_daemonset name={event.entity_id} namespace={event.namespace}")

            elif entity_type == "node":
                if is_direct_failure:
                    # For direct node failures, consider draining with a short grace period
                    all_suggestions.append(f"drain_node name={event.entity_id} gracePeriod=300")
                else:
                    # For anomalies, just cordon
                    all_suggestions.append(f"cordon_node name={event.entity_id}")

            elif entity_type == "persistentvolumeclaim":
                # For PVC issues, we can't do much automatically
                all_suggestions.append(f"describe_pvc name={event.entity_id} namespace={event.namespace}")

            elif entity_type == "service":
                # For service endpoint issues, fetch endpoints for diagnosis
                all_suggestions.append(f"get_endpoints name={event.entity_id} namespace={event.namespace}")

            elif entity_type == "unknown" and event.namespace:
                # For unknown types, try to get pods in the same namespace and restart them
                all_suggestions.append(f"restart_pods_in_namespace namespace={event.namespace}")

            if all_suggestions:
                logger.info(f"Using tailored remediation commands for {event.entity_id}: {all_suggestions}")

        # For predicted failures that aren't critical, consider less invasive actions
        if is_predicted and not is_critical and not ai_suggestions:
            entity_type = event.entity_type.lower()
            all_suggestions = []  # Reset suggestions for predicted non-critical issues

            # Add scaled-down remediation for predicted issues
            if entity_type == "pod":
                # For pods with predicted issues, monitor instead of restarting immediately
                all_suggestions.append(f"monitor_pod name={event.entity_id} namespace={event.namespace}")
            elif entity_type == "deployment":
                # For deployments, consider scaling up rather than restarting
                all_suggestions.append(f"scale_deployment name={event.entity_id} namespace={event.namespace} replicas=+1")
            elif entity_type == "statefulset":
                # For statefulsets, scale up or monitor
                all_suggestions.append(f"scale_statefulset name={event.entity_id} namespace={event.namespace} replicas=+1")
            elif entity_type == "node":
                # For nodes, drain proactively instead of cordoning
                all_suggestions.append(f"drain_node name={event.entity_id} gracePeriod=600")

            if all_suggestions:
                logger.info(f"Using proactive remediation for predicted failure in {event.entity_id}: {all_suggestions}")

        validated_safe_commands = []
        validated_params_map = {}  # Store parsed params for execution

        for cmd_str in all_suggestions:
            validation_result = k8s_executor.parse_and_validate_command(cmd_str)
            if validation_result:
                op_name, params = validation_result

                # For critical failures, check if command is in the safe critical commands list
                if is_critical and hasattr(settings, "SAFE_CRITICAL_COMMANDS") and op_name not in settings.SAFE_CRITICAL_COMMANDS:
                    logger.warning(f"Command {cmd_str} not in safe critical commands list, skipping for critical failure")
                    continue

                # For predicted failures, check if command is in the safe proactive commands list
                if is_predicted and not is_critical and hasattr(settings, "SAFE_PROACTIVE_COMMANDS") and op_name not in settings.SAFE_PROACTIVE_COMMANDS:
                    logger.warning(f"Command {cmd_str} not in safe proactive commands list, skipping for predicted failure")
                    continue

                # For direct failures, prioritize more aggressive remediation commands
                if is_direct_failure:
                    # Direct failures are often actual issues rather than anomalies, so more aggressive remediation is appropriate
                    # Check if we should use this command for direct failures (some might be too risky)
                    if hasattr(settings, "DIRECT_FAILURE_RESTRICTED_COMMANDS") and op_name in settings.DIRECT_FAILURE_RESTRICTED_COMMANDS:
                        logger.warning(f"Command {cmd_str} restricted for direct failures, skipping")
                        continue

                validated_safe_commands.append(cmd_str)
                validated_params_map[cmd_str] = (op_name, params)
            else:
                logger.warning(f"Command deemed unsafe or invalid: {cmd_str}")

        # Update event with validated suggestions
        await self.anomaly_event_service.update_event(
            event.anomaly_id,
            AnomalyEventUpdate(
                suggested_remediation_commands=validated_safe_commands
            )
        )

        # Execute remediation based on mode and criticality/failure type
        should_execute = (
            current_mode == "AUTO" or  # Always execute in AUTO mode
            (is_critical and hasattr(settings, "AUTO_REMEDIATE_CRITICAL") and settings.AUTO_REMEDIATE_CRITICAL) or  # Critical failures
            (is_direct_failure and hasattr(settings, "AUTO_REMEDIATE_DIRECT_FAILURES") and settings.AUTO_REMEDIATE_DIRECT_FAILURES) or  # Directly detected failures
            (is_predicted and hasattr(settings, "AUTO_REMEDIATE_PREDICTED") and settings.AUTO_REMEDIATE_PREDICTED)  # Predicted failures
        )

        if should_execute and validated_safe_commands:
            selected_cmd = validated_safe_commands[0]
            operation, params = validated_params_map[selected_cmd]

            logger.info(f"Executing remediation command: {selected_cmd}")

            try:
                result = await k8s_executor.execute_validated_command(operation, params)

                # Record remediation attempt
                attempt = RemediationAttempt(
                    command=selected_cmd,
                    parameters=params,
                    executor="AUTO",
                    success=True,
                    result=json.dumps(result) if isinstance(result, dict) else str(result),
                    is_proactive=is_predicted and not is_critical
                )

                # Schedule verification - use shorter delay for direct failures
                verification_delay = (
                    settings.CRITICAL_VERIFICATION_DELAY_SECONDS if is_critical else  # Shortest for critical failures
                    (settings.DIRECT_FAILURE_VERIFICATION_DELAY_SECONDS if is_direct_failure and hasattr(settings, "DIRECT_FAILURE_VERIFICATION_DELAY_SECONDS") else  # Medium for direct failures
                    settings.VERIFICATION_DELAY_SECONDS)  # Standard for other issues
                )
                verification_time = datetime.utcnow() + timedelta(seconds=verification_delay)

                # Update event
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
                logger.error(f"Error executing remediation: {e}")
                # Record failed attempt
                attempt = RemediationAttempt(
                    command=selected_cmd,
                    parameters=params,
                    executor="AUTO",
                    success=False,
                    error=str(e),
                    is_proactive=is_predicted and not is_critical
                )
                await self.anomaly_event_service.update_event(
                    event.anomaly_id,
                    AnomalyEventUpdate(remediation_attempts=[attempt])
                )

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
            # Reset cycle-specific flags
            self._retraining_attempted = False
            self._insufficient_data_warned = False

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

            # 4. Cleanup stale entries in metric history
            await self._cleanup_stale_metric_history()

            # 5. Direct scan for failures
            await self._direct_scan_for_failures()

        except Exception as e:
            logger.error(f"CRITICAL Error in worker cycle: {e}", exc_info=True)

        logger.info("Worker cycle finished.")

    async def _cleanup_stale_metric_history(self):
        """
        Cleanup stale entries in the entity_metric_history.
        """
        current_time = datetime.utcnow()
        if (current_time - self.last_cleanup_time).total_seconds() < self.cleanup_interval_seconds:
            return  # Skip cleanup if interval has not passed

        logger.info("Cleaning up stale entries in entity_metric_history...")
        stale_threshold = current_time - timedelta(seconds=self.stale_entity_timeout_seconds)
        keys_to_delete = []

        for entity_key, metrics in self.entity_metric_history.items():
            if metrics and metrics[-1] and metrics[-1][0].get("timestamp"):
                last_timestamp = datetime.fromisoformat(metrics[-1][0]["timestamp"])
                if last_timestamp < stale_threshold:
                    keys_to_delete.append(entity_key)

        for key in keys_to_delete:
            del self.entity_metric_history[key]
            logger.info(f"Removed stale entity from history: {key}")

        self.last_cleanup_time = current_time

    async def _direct_scan_for_failures(self):
        """
        Proactively scans for failures in the cluster using direct Kubernetes API calls.
        This complements metric-based detection by finding failures that may not be
        fully captured in metrics or that occurred before metrics were collected.
        """
        logger.info("Starting direct scan for existing failures in the cluster...")
        failure_count = 0

        try:
            # Get all resources that might be in a failed state
            # 1. Failed pods
            failed_pods = await k8s_executor.list_resources_with_status(
                resource_type="pod",
                status_filter=["Failed", "CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull"]
            )

            # 2. Unhealthy deployments
            unhealthy_deployments = await k8s_executor.list_resources_with_status(
                resource_type="deployment",
                status_filter=["Degraded", "ProgressDeadlineExceeded"]
            )

            # 3. Nodes with issues
            problematic_nodes = await k8s_executor.list_resources_with_status(
                resource_type="node",
                status_filter=["NotReady", "SchedulingDisabled", "Pressure"]
            )

            # 4. Services with endpoints issues
            services_with_issues = await k8s_executor.list_resources_with_status(
                resource_type="service",
                status_filter=["NoEndpoints", "EndpointsNotReady"]
            )

            # 5. PVCs with issues
            pvc_with_issues = await k8s_executor.list_resources_with_status(
                resource_type="persistentvolumeclaim",
                status_filter=["Pending", "Lost", "Bound=False"]
            )

            # Process each failed pod to create an anomaly event
            for pod in failed_pods:
                failure_count += 1
                await self._handle_direct_failure(
                    entity_type="pod",
                    entity_id=pod.get("metadata", {}).get("name"),
                    namespace=pod.get("metadata", {}).get("namespace"),
                    status=pod.get("status", {}).get("phase", "Unknown"),
                    reason=pod.get("status", {}).get("reason", "DirectScanFailure"),
                    message=f"Direct scan found pod in problematic state: {pod.get('status', {}).get('phase')}",
                    resource_data=pod
                )

            # Process unhealthy deployments
            for deployment in unhealthy_deployments:
                failure_count += 1
                conditions = deployment.get("status", {}).get("conditions", [])
                reason = "Unknown"
                message = "Deployment in degraded state"

                for condition in conditions:
                    if condition.get("status") != "True" and condition.get("type") in ["Available", "Progressing"]:
                        reason = condition.get("reason", "DeploymentIssue")
                        message = condition.get("message", "Deployment has availability issues")

                await self._handle_direct_failure(
                    entity_type="deployment",
                    entity_id=deployment.get("metadata", {}).get("name"),
                    namespace=deployment.get("metadata", {}).get("namespace"),
                    status="Degraded",
                    reason=reason,
                    message=message,
                    resource_data=deployment
                )

            # Process problematic nodes
            for node in problematic_nodes:
                failure_count += 1
                conditions = node.get("status", {}).get("conditions", [])
                reason = "Unknown"
                message = "Node issue detected"

                for condition in conditions:
                    if condition.get("type") == "Ready" and condition.get("status") != "True":
                        reason = condition.get("reason", "NodeNotReady")
                        message = condition.get("message", "Node is not in Ready state")
                    elif condition.get("type") in ["DiskPressure", "MemoryPressure", "NetworkUnavailable", "PIDPressure"] and condition.get("status") == "True":
                        reason = condition.get("reason", f"{condition.get('type')}Issue")
                        message = condition.get("message", f"Node has {condition.get('type')} condition")

                await self._handle_direct_failure(
                    entity_type="node",
                    entity_id=node.get("metadata", {}).get("name"),
                    namespace=None,
                    status="Problematic",
                    reason=reason,
                    message=message,
                    resource_data=node
                )

            # Process services with endpoint issues
            for service in services_with_issues:
                failure_count += 1
                await self._handle_direct_failure(
                    entity_type="service",
                    entity_id=service.get("metadata", {}).get("name"),
                    namespace=service.get("metadata", {}).get("namespace"),
                    status="EndpointIssue",
                    reason="NoHealthyEndpoints",
                    message="Service has no healthy endpoints",
                    resource_data=service
                )

            # Process PVCs with issues
            for pvc in pvc_with_issues:
                failure_count += 1
                await self._handle_direct_failure(
                    entity_type="persistentvolumeclaim",
                    entity_id=pvc.get("metadata", {}).get("name"),
                    namespace=pvc.get("metadata", {}).get("namespace"),
                    status=pvc.get("status", {}).get("phase", "Unknown"),
                    reason="StorageIssue",
                    message=f"PVC has storage provisioning issue: {pvc.get('status', {}).get('phase')}",
                    resource_data=pvc
                )

            if failure_count > 0:
                logger.warning(f"Direct scan found {failure_count} existing failures in the cluster")
            else:
                logger.info("Direct scan completed. No existing failures found.")

            return failure_count

        except Exception as e:
            logger.error(f"Error during direct failure scan: {e}", exc_info=True)
            return 0

    async def _handle_direct_failure(self, entity_type: str, entity_id: str, namespace: Optional[str],
                                  status: str, reason: str, message: str, resource_data: Dict[str, Any]):
        """
        Creates and handles anomaly events for failures detected via direct scan.

        Args:
            entity_type: Type of entity (pod, deployment, node, etc.)
            entity_id: Name/ID of the entity
            namespace: Namespace of the entity (if applicable)
            status: Current status of the entity
            reason: Reason for the failure
            message: Detailed message about the failure
            resource_data: Raw resource data from Kubernetes
        """
        try:
            # First check if there's already an active event for this entity
            active_event = await self.anomaly_event_service.find_active_event(
                entity_id=entity_id,
                entity_type=entity_type,
                namespace=namespace
            )

            if active_event:
                logger.info(f"Active event already exists for {entity_type}/{entity_id}: {active_event.anomaly_id}")

                # Add a note about this direct detection if it's not already in remediation
                if active_event.status not in [AnomalyStatus.VERIFICATION_PENDING, AnomalyStatus.REMEDIATION_ATTEMPTED]:
                    notes = f"Direct scan confirmation at {datetime.utcnow().isoformat()}: {reason} - {message}"
                    if active_event.notes:
                        notes = f"{active_event.notes}\n{notes}"

                    await self.anomaly_event_service.update_event(
                        active_event.anomaly_id,
                        AnomalyEventUpdate(
                            notes=notes,
                            status=AnomalyStatus.FAILURE_DETECTED  # Upgrade to direct failure if it was just an anomaly
                        )
                    )
                return

            # Create new event for this failure
            entity_key = f"{entity_type}/{entity_id}"
            if namespace:
                entity_key = f"{namespace}/{entity_key}"

            logger.warning(f"DIRECT FAILURE DETECTED for entity {entity_key}: {reason}")

            # Create appropriate event status
            if entity_type == "node":
                event_status = AnomalyStatus.CRITICAL_FAILURE  # Node issues are critical
            elif status in ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"]:
                event_status = AnomalyStatus.POD_STATE_ISSUE
            else:
                event_status = AnomalyStatus.FAILURE_DETECTED

            # Try to fetch metrics for this entity if available
            metric_snapshot = []
            try:
                all_metrics = await prometheus_scraper.fetch_all_metrics(self.active_promql_queries)
                if entity_key in all_metrics:
                    metric_snapshot = [all_metrics[entity_key].get("metrics", [])]
            except Exception as e:
                logger.warning(f"Could not fetch metrics for direct failure event: {e}")

            # Create event data
            event_data = {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "namespace": namespace,
                "metric_snapshot": metric_snapshot,
                "anomaly_score": 1.0,  # Maximum score for direct failures
                "status": event_status,
                "notes": f"Directly detected failure at {datetime.utcnow().isoformat()}: {reason} - {message}"
            }

            # Create event and get remediation suggestions
            new_event = await self.anomaly_event_service.create_event(event_data)

            if not new_event:
                logger.error(f"Failed to create event for direct failure: {entity_key}")
                return

            # Process with Gemini if available
            remediation_suggestions_raw = []
            if self.gemini_service and settings.GEMINI_AUTO_ANALYSIS:
                try:
                    # Prepare context for Gemini
                    failure_context = {
                        "entity_id": entity_id,
                        "entity_type": entity_type,
                        "namespace": namespace,
                        "status": status,
                        "reason": reason,
                        "message": message,
                        "resource_data": resource_data,
                        "timestamp": datetime.utcnow().isoformat()
                    }

                    # Get remediation suggestions with high priority
                    remediation_res = await self.gemini_service.suggest_remediation(
                        failure_context,
                        prioritize_critical=True
                    )

                    if remediation_res and "error" not in remediation_res and "steps" in remediation_res:
                        remediation_suggestions_raw = remediation_res["steps"]
                        await self.anomaly_event_service.update_event(
                            new_event.anomaly_id,
                            AnomalyEventUpdate(
                                suggested_remediation_commands=remediation_suggestions_raw,
                                status=AnomalyStatus.REMEDIATION_SUGGESTED
                            )
                        )
                except Exception as e:
                    logger.error(f"Error in Gemini processing for direct failure: {e}")

            # Always try to remediate direct failures if appropriate
            is_critical = entity_type == "node" or status in ["Failed", "CrashLoopBackOff"]
            await self._decide_remediation(
                new_event,
                remediation_suggestions_raw,
                is_critical=is_critical,
                is_predicted=False,
                is_direct_failure=True  # Flag to indicate this is a directly detected failure
            )

        except Exception as e:
            logger.error(f"Error handling direct failure for {entity_type}/{entity_id}: {e}", exc_info=True)

    async def run(self):
        """The main continuous loop for the worker."""
        logger.info("AutonomousWorker starting run loop...")
        while True:
            await self.run_cycle()
            logger.debug(f"Worker sleeping for {settings.WORKER_SLEEP_INTERVAL_SECONDS} seconds...")
            await asyncio.sleep(settings.WORKER_SLEEP_INTERVAL_SECONDS)
