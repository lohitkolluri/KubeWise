import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings
from app.models.anomaly_detector import anomaly_detector
from app.models.anomaly_event import (
    AnomalyEvent,
    AnomalyEventUpdate,
    AnomalyStatus,
    RemediationAttempt,
)
from app.services.anomaly_event_service import AnomalyEventService
from app.services.gemini_service import GeminiService, RemediationAction
from app.services.mode_service import mode_service
from app.utils.k8s_executor import k8s_executor, list_resources
from app.utils.prometheus_scraper import prometheus_scraper


class AutonomousWorker:
    """
    Background worker that continuously monitors Kubernetes metrics,
    detects anomalies, and manages the remediation process.
    """

    def __init__(
        self,
        anomaly_event_svc: AnomalyEventService,
        gemini_svc: Optional[GeminiService],
    ):
        """
        Initialize the autonomous worker.

        Args:
            anomaly_event_svc: Service for managing anomaly events
            gemini_svc: Optional service for AI-powered analysis and remediation
        """
        self.anomaly_event_service = anomaly_event_svc
        self.gemini_service = gemini_svc  # Can be None
        self.entity_metric_history = {}
        self.history_limit = (
            settings.ANOMALY_METRIC_WINDOW_SECONDS
            // settings.PROMETHEUS_SCRAPE_INTERVAL_SECONDS
            + 1
        )

        # Initialize with empty list instead of using settings.DEFAULT_PROMQL_QUERIES
        # Queries will be populated from queries.py in _initialize_queries method
        self.active_promql_queries: List[str] = []

        self.is_initialized = False
        self.last_cleanup_time = datetime.utcnow()
        # Set cleanup interval - default to every 30 minutes if not in settings
        self.cleanup_interval_seconds = getattr(
            settings, "METRIC_HISTORY_CLEANUP_INTERVAL_SECONDS", 1800
        )
        # Set stale entity timeout - default to 30 minutes if not in settings
        self.stale_entity_timeout_seconds = getattr(
            settings, "STALE_ENTITY_TIMEOUT_SECONDS", 1800
        )
        # Replace the lambda with a proper method reference
        self.safe_serialize = self._deep_serialize
        logger.info("AutonomousWorker initialized.")

    def _deep_serialize(self, obj):
        """
        Recursively serializes an object to ensure all nested structures are JSON-serializable.
        Handles Kubernetes API objects, datetime objects, and other complex types.

        Args:
            obj: The object to serialize

        Returns:
            A JSON-serializable representation of the object
        """
        if isinstance(obj, dict):
            return {str(k): self._deep_serialize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._deep_serialize(item) for item in obj]
        elif isinstance(obj, (datetime, timedelta)):
            return str(obj)
        elif isinstance(obj, (int, float, bool, str, type(None))):
            return obj
        elif hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
            # Handle Kubernetes API objects that have to_dict method
            return self._deep_serialize(obj.to_dict())
        elif hasattr(obj, "__dict__"):
            # Handle general objects with __dict__
            return self._deep_serialize(obj.__dict__)
        else:
            # For any other type, convert to string
            return str(obj)

    async def _initialize_queries(self):
        """
        Initialize PromQL queries for metrics collection.
        Uses standardized queries to ensure consistent data format for the model.
        """
        logger.info(
            "Initializing standardized PromQL queries for metrics collection..."
        )

        try:
            # Import centralized query utilities
            from app.core.queries import STANDARD_QUERIES, get_default_query_list, create_placeholder_query, FAILURE_DETECTION_QUERIES

            # Get required feature metrics, default to empty list if not available
            feature_metrics = getattr(settings, "FEATURE_METRICS", [])
            if not feature_metrics:
                logger.error("No FEATURE_METRICS defined in settings. Using default set.")
                feature_metrics = [
                    "cpu_usage_seconds_total",
                    "memory_working_set_bytes",
                    "network_receive_bytes_total",
                    "network_transmit_bytes_total",
                    "pod_status_phase",
                    "container_restarts_rate",
                    "node_cpu_usage",
                    "node_memory_usage",
                    "deployment_replicas_available",  # Note: renamed from deployment_available_replicas
                    "deployment_replicas_unavailable", # Note: renamed from deployment_unavailable_replicas
                    "disk_usage_bytes",
                    "filesystem_available",
                    "filesystem_size"
                ]

            # Initialize collections
            final_queries = []
            covered_metrics = set()

            # Use the standardized queries from our centralized module
            logger.info("Using queries from centralized queries module")

            for feature_metric in feature_metrics:
                if feature_metric in STANDARD_QUERIES:
                    query = STANDARD_QUERIES[feature_metric]
                    final_queries.append(query)
                    covered_metrics.add(feature_metric)
                    logger.info(f"Using standard query for {feature_metric}: {query}")
                else:
                    # For metrics not in our standardized list, add a placeholder
                    logger.warning(f"No standard query for {feature_metric}, using placeholder")
                    placeholder_query = create_placeholder_query(feature_metric)
                    final_queries.append(placeholder_query)
                    covered_metrics.add(feature_metric)

            # Append OOMKilled failure detection query
            final_queries.append(FAILURE_DETECTION_QUERIES["oom_killed_containers"])
            covered_metrics.add("oom_killed_containers")

            logger.info(f"Final query selection: {len(final_queries)} queries covering {len(covered_metrics)}/{len(feature_metrics)} required metrics")
            for query in final_queries:
                logger.debug(f"Query: {query}")  # Change to debug to reduce log verbosity

            # Set the active queries
            self.active_promql_queries = final_queries
            self.is_initialized = True
            logger.info("Query initialization completed successfully")
            return final_queries

        except Exception as e:
            logger.error(f"Error initializing queries: {str(e)}", exc_info=True)
            # Set a minimal set of queries to allow the system to continue
            from app.core.queries import get_fallback_query_list
            self.active_promql_queries = get_fallback_query_list()
            self.is_initialized = True  # Mark as initialized even with fallback queries
            logger.warning("Using fallback queries due to initialization error")
            return self.active_promql_queries

    async def _find_best_query_for_metric(
        self, metric_type, candidates, fallback, available_metrics
    ):
        """
        Find the best query for a specific metric type by testing all candidates in parallel.

        Args:
            metric_type: The type of metric (cpu_usage, memory_usage, etc.)
            candidates: List of candidate queries to test
            fallback: Fallback query to use if no candidates work
            available_metrics: List of available metrics from Prometheus

        Returns:
            Tuple of (metric_type, best_query, source) or None if failed
        """
        logger.info(f"Finding best query for {metric_type}...")

        # Filter candidates based on available metrics
        filtered_candidates = []
        for candidate_query in candidates:
            # Extract base metric from the query to check availability
            base_metric = self._extract_base_metric_from_query(candidate_query)

            # If we can identify a base metric, check if it exists
            if base_metric and not any(
                m == base_metric or base_metric in m for m in available_metrics
            ):
                logger.debug(
                    f"Skipping candidate with unavailable metric: {base_metric}"
                )
                continue

            filtered_candidates.append(candidate_query)

        if not filtered_candidates:
            logger.warning(
                f"No suitable candidates found for {metric_type}, using fallback"
            )
            return metric_type, fallback, f"fallback:{metric_type}"

        # Test all candidates in parallel
        test_tasks = []
        for query in filtered_candidates:
            test_tasks.append(self._test_and_score_query(query, metric_type))

        # Wait for all tests to complete
        results = await asyncio.gather(*test_tasks, return_exceptions=True)

        # Process results and find the best query
        valid_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.debug(
                    f"Error testing query '{filtered_candidates[i]}': {str(result)}"
                )
                continue

            if not result:  # None result means query failed or returned no data
                continue

            query, score, data = result
            valid_results.append((query, score, data))

        if not valid_results:
            logger.warning(
                f"No working queries found for {metric_type}. Using fallback."
            )
            return metric_type, fallback, f"fallback:{metric_type}"

        # Find the query with the highest score
        best_query, best_score, _ = max(valid_results, key=lambda x: x[1])
        logger.info(
            f"Found best query for {metric_type}: {best_query} (score: {best_score})"
        )

        return metric_type, best_query, f"parallel_discovery:{metric_type}"

    async def _test_and_score_query(self, query, metric_type):
        """
        Test a single query and score it based on the results.

        Args:
            query: The PromQL query to test
            metric_type: The type of metric we're testing for

        Returns:
            Tuple of (query, score, data) or None if query failed
        """
        try:
            # Validate the query
            validation_result = await prometheus_scraper.validate_query(query)
            if not validation_result["valid"]:
                logger.debug(f"Invalid query: {query}")
                return None

            # Try to execute the query to check for data
            data = await prometheus_scraper.fetch_metric_data_for_label_check(
                query, limit=5
            )
            if not data:
                logger.debug(f"Query returned no data: {query}")
                return None

            # Score the query based on its structure and results
            score = self._score_query(query, data, metric_type)

            return query, score, data
        except Exception as e:
            logger.debug(f"Error testing query '{query}': {str(e)}")
            return None

    async def _test_query(self, query):
        """
        Test a single PromQL query and return its results if valid.

        Args:
            query: The PromQL query to test

        Returns:
            Query results if successful, None if invalid or no data
        """
        try:
            # Validate the query syntax
            validation_result = await prometheus_scraper.validate_query(query)
            if not validation_result["valid"]:
                logger.debug(f"Invalid query: {query}")
                return None

            # Try to execute the query to check for data
            data = await prometheus_scraper.fetch_metric_data_for_label_check(
                query, limit=5
            )
            if not data:
                logger.debug(f"Query returned no data: {query}")
                return None

            return data
        except Exception as e:
            logger.debug(f"Error testing query '{query}': {str(e)}")
            return None

    def _score_query(self, query, results, metric_type):
        """
        Score a query based on its structure and results.

        Args:
            query: The PromQL query to score
            results: The results returned by the query
            metric_type: The type of metric we're scoring for (cpu_usage, memory_usage, etc.)

        Returns:
            A numerical score where higher is better
        """
        score = 10  # Base score

        # Define the required feature metrics for our model
        required_metrics = [
            "cpu_usage_seconds_total",  # Rate
            "memory_working_set_bytes",  # Absolute value
            "network_receive_bytes_total",  # Rate
            "network_transmit_bytes_total",  # Rate
            "pod_status_phase",  # Categorical/Binary (Running=1, else=0)
            "container_restarts_rate",  # Counter / Rate of change
        ]

        query_lower = query.lower()

        # Enhanced scoring weights
        weights = {
            "metric_match": 10,       # Weight for matching a required metric
            "pod_level": 8,           # Weight for pod-level metrics (preferred)
            "deployment_level": 6,    # Weight for deployment-level metrics
            "node_level": 5,          # Weight for node-level metrics
            "aggregation": 4,         # Weight for proper aggregation methods
            "rate_usage": 6,          # Weight for proper rate usage on counters
            "result_volume": 3,       # Weight for number of results returned
            "label_quality": 5,       # Weight for having proper entity labels
            "type_match": 7,          # Weight for query matching the metric type
            "namespace_filtering": 4, # Weight for proper namespace filtering
            "value_presence": 3       # Weight for presence of values
        }

        # 1. Give extra points for required metrics
        for required_metric in required_metrics:
            if required_metric.lower() in query_lower:
                score += weights["metric_match"]
                logger.info(
                    f"Query '{query}' contains required metric '{required_metric}', +{weights['metric_match']} points"
                )

        # 2. Score based on query structure (entity level granularity)
        if " by (namespace, pod)" in query:
            score += weights["pod_level"]  # Pod-level metrics preferred
        elif " by (namespace, pod, container)" in query:
            score += weights["pod_level"] + 1  # Even better - container level
        elif " by (node)" in query:
            score += weights["node_level"]  # Node-level metrics good too
        elif " by (namespace, deployment)" in query:
            score += weights["deployment_level"]  # Deployment-level metrics also good

        # 3. Score based on aggregation function
        if "sum(" in query:
            score += weights["aggregation"]  # Sum is generally good
        elif "avg(" in query:
            score += weights["aggregation"] - 1  # Average is fine
        elif "max(" in query:
            score += weights["aggregation"] - 1  # Max is fine for certain metrics
        elif "min(" in query:
            score += weights["aggregation"] - 2  # Min is less commonly useful

        # 4. Score based on rate usage for counters
        if "_total" in query and "rate(" not in query and "irate(" not in query and "increase(" not in query:
            score -= weights["rate_usage"]  # Counter without rate is bad
        elif "_total" in query and "rate(" in query:
            score += weights["rate_usage"]  # Rate for counters is good
        elif "_total" in query and "irate(" in query:
            score += weights["rate_usage"] - 1  # irate is good for high resolution
        elif "_total" in query and "increase(" in query:
            score += weights["rate_usage"] - 2  # increase is good for some use cases

        # 5. Score based on returned data volume
        if len(results) >= 5:
            score += weights["result_volume"]  # More results are better (within reason)
        elif len(results) >= 3:
            score += weights["result_volume"] - 1  # Some results
        elif len(results) >= 1:
            score += 1  # At least some results

        # 6. Check that returned data has useful labels
        has_namespace_pod = False
        has_node = False
        has_container = False

        for result in results:
            labels = result.get("labels", {})
            if "namespace" in labels and "pod" in labels:
                has_namespace_pod = True
                score += weights["label_quality"]
            if "node" in labels:
                has_node = True
                score += weights["label_quality"] // 2
            if "container" in labels:
                has_container = True
                score += weights["label_quality"] // 2

        # 7. Additional points for complete entity identification
        if has_namespace_pod and has_container:
            score += weights["label_quality"]  # Namespace+pod+container is ideal for container metrics

        # 8. Extra scoring based on metric type match
        if metric_type == "cpu_usage" and any(x in query_lower for x in ["cpu", "usage_seconds"]):
            score += weights["type_match"]  # Query is clearly about CPU
        elif metric_type == "memory_usage" and any(x in query_lower for x in ["memory", "bytes"]):
            score += weights["type_match"]  # Query is clearly about memory
        elif metric_type == "network_receive" and any(x in query_lower for x in ["network", "receive"]):
            score += weights["type_match"]  # Query is clearly about network receive
        elif metric_type == "network_transmit" and any(x in query_lower for x in ["network", "transmit"]):
            score += weights["type_match"]  # Query is clearly about network transmit
        elif metric_type == "pod_status" and any(x in query_lower for x in ["pod", "status", "phase"]):
            score += weights["type_match"]  # Query is clearly about pod status
        elif metric_type == "container_restarts" and any(x in query_lower for x in ["restart", "container_status"]):
            score += weights["type_match"]  # Query is clearly about container restarts
        elif metric_type == "container_cpu_throttling" and "throttle" in query_lower:
            score += weights["type_match"]  # Query is clearly about throttling
        elif metric_type == "container_memory_limit" and "limit" in query_lower:
            score += weights["type_match"]  # Query is clearly about memory limits

        # 9. Check for value presence and type
        for result in results:
            if "value" not in result or result["value"] is None:
                score -= weights["value_presence"]  # Missing values are bad

        # 10. Bonus for namespace filtering that improves query efficiency
        if "{namespace" in query_lower or "{namespace=" in query_lower:
            score += weights["namespace_filtering"]  # Efficient namespace filtering

        # 11. Penalize overly complex queries that might be slow
        if query.count("(") > 4:
            score -= 3  # Too many nested functions
        if query.count("{") > 3:
            score -= 2  # Too many label matchers

        # 12. Bonus for using time() function to keep queries current
        if "time()" in query:
            score += 2

        return score

    async def _update_metric_history(
        self, entity_key: str, current_metrics: List[Dict[str, Any]]
    ):
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
            self.entity_metric_history[entity_key] = self.entity_metric_history[
                entity_key
            ][-self.history_limit :]

    async def _process_entity_metrics(
        self, entity_key: str, entity_data: Dict[str, Any]
    ):
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
        if len(self.entity_metric_history[entity_key]) >= 2:  # Require at least 2 data points for prediction
            await self._predict_and_handle_anomaly(
                entity_key, entity_data, self.entity_metric_history[entity_key]
            )

    async def _predict_and_handle_anomaly(
        self,
        entity_key: str,
        entity_data: Dict[str, Any],
        metric_window: List[List[Dict[str, Any]]],
    ):
        """
        Predicts anomalies/failures using historical data and handles them appropriately.

        Args:
            entity_key: Unique identifier for the entity
            entity_data: Entity data containing the latest metrics and metadata
            metric_window: Window of historical metric snapshots for the entity
        """
        try:
            entity_id = entity_data.get("entity_id", "unknown")
            entity_type = entity_data.get("entity_type", "unknown")
            namespace = entity_data.get("namespace", None)  # Use None if not present

            # Only analyze entities with proper identification
            if entity_id == "unknown" or entity_type == "unknown":
                logger.debug(f"Skipping prediction for unidentified entity: {entity_key}")
                return

            logger.debug(f"Predicting anomalies for {entity_key} using {len(metric_window)} historical snapshots")

            # Get the current metrics snapshot
            current_metrics_snapshot = entity_data.get("metrics", [])

            # The history should exclude the current snapshot if it was just added
            historical_snapshots = metric_window[:-1] if metric_window else []

            if not current_metrics_snapshot:
                logger.warning(f"No current metrics snapshot available for {entity_key}")
                return

            # Use predict_with_forecast which accepts historical data for better analysis
            prediction_result = await anomaly_detector.predict_with_forecast(
                latest_metrics_snapshot=current_metrics_snapshot,
                historical_snapshots=historical_snapshots
            )

            # Check if prediction is successful (returns a dict)
            if prediction_result is None or not isinstance(prediction_result, dict):
                logger.warning(f"No prediction result or invalid format for {entity_key}")
                return

            # Unpack results from the dictionary
            is_anomaly = prediction_result.get("is_anomaly", False)
            anomaly_score = prediction_result.get("anomaly_score", 0.0)
            is_critical = prediction_result.get("is_critical", False)
            recommendation = prediction_result.get("recommendation", "monitor")
            predicted_failures = prediction_result.get("predicted_failures", [])
            forecast_available = prediction_result.get("forecast_available", False)

            # Store the prediction result for potential later use
            self.latest_predictions = getattr(self, 'latest_predictions', {})
            self.latest_anomaly_scores = getattr(self, 'latest_anomaly_scores', {})
            self.latest_predictions[entity_key] = prediction_result
            self.latest_anomaly_scores[entity_key] = anomaly_score

            # Determine if this is primarily a predicted failure event
            # Check if it's not currently anomalous but has critical predictions
            is_primarily_prediction = (
                not is_anomaly
                and any(p.get('minutes_until_failure', 999) < 15 and p.get('confidence', 0) > 0.7 for p in predicted_failures)
            )

            # Log anomaly or prediction if detected
            if is_anomaly or is_primarily_prediction:
                if is_primarily_prediction:
                    event_status = AnomalyStatus.PREDICTED_FAILURE
                    log_message = f"Predicted failure for {entity_key}. Recommendation: {recommendation}"
                    logger.warning(log_message)
                elif is_critical:
                    event_status = AnomalyStatus.CRITICAL_FAILURE
                    log_message = f"CRITICAL Anomaly detected for {entity_key} with score: {anomaly_score:.4f}. Recommendation: {recommendation}"
                    logger.error(log_message)
                else:
                    event_status = AnomalyStatus.DETECTED
                    log_message = f"Anomaly detected for {entity_key} with score: {anomaly_score:.4f}. Recommendation: {recommendation}"
                    logger.warning(log_message)

                # Check if an active event already exists for this entity
                active_event = await self.anomaly_event_service.find_active_event(
                    entity_id=entity_id, entity_type=entity_type, namespace=namespace
                )

                if active_event:
                    # Update existing event
                    logger.info(f"Updating existing active event {active_event.anomaly_id} for {entity_key}")
                    update_data = AnomalyEventUpdate(
                        status=event_status if event_status == AnomalyStatus.CRITICAL_FAILURE else active_event.status,
                        anomaly_score=anomaly_score,
                        metric_snapshot=current_metrics_snapshot,
                        prediction_data=prediction_result,
                        notes=f"{active_event.notes}\nUpdate: Score={anomaly_score:.4f}, Recommendation={recommendation}"
                    )
                    await self.anomaly_event_service.update_event(active_event.anomaly_id, update_data)
                    anomaly_id = active_event.anomaly_id
                    event = active_event
                else:
                    # Create a new anomaly event
                    event = AnomalyEvent(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        namespace=namespace,
                        anomaly_score=anomaly_score,
                        status=event_status,
                        metric_snapshot=current_metrics_snapshot,
                        prediction_data=prediction_result,
                        is_proactive=is_primarily_prediction,
                        notes=log_message,
                    )

                    # Create event in database
                    anomaly_id = await self.anomaly_event_service.create_event(event)

                if anomaly_id:
                    logger.info(f"{'Created' if not active_event else 'Updated'} anomaly event {anomaly_id} for {entity_key}")

                    # Check if AI analysis is enabled and AI service is available
                    if self.gemini_service and not self.gemini_service.is_api_key_missing():
                        try:
                            # Format the metrics for AI analysis (using history)
                            ai_metrics_context = await self.format_metrics_for_ai(
                                entity_id=entity_id,
                                entity_type=entity_type,
                                namespace=namespace,
                                current_metrics=current_metrics_snapshot,
                                historical_metrics=historical_snapshots
                            )

                            # Get AI analysis of the anomaly/prediction with enhanced context
                            analysis_context = {
                                "anomaly_score": anomaly_score,
                                "is_critical": is_critical,
                                "is_predicted": is_primarily_prediction,
                                "recommendation": recommendation,
                                "prediction_data": prediction_result
                            }

                            analysis_result = await self.gemini_service.analyze_anomaly(
                                anomaly_data=analysis_context,
                                entity_info={"entity_id": entity_id, "entity_type": entity_type, "namespace": namespace},
                                related_metrics=ai_metrics_context.get("metrics", {}),
                                additional_context={"metric_window": metric_window}
                            )

                            # Log AI analysis results
                            if analysis_result and "error" not in analysis_result:
                                logger.info(f"AI analysis completed for anomaly {anomaly_id}")

                                # Update the event with AI analysis
                                await self.anomaly_event_service.update_event(
                                    anomaly_id,
                                    AnomalyEventUpdate(
                                        ai_analysis=analysis_result,
                                        notes=f"AI Analysis: {analysis_result.get('root_cause', 'Analysis completed')}"
                                    )
                                )

                                # Get remediation suggestions if in autonomous or assisted mode
                                if mode_service.get_mode() != "learning":
                                    ai_suggestions = await self.gemini_service.suggest_remediation(
                                        anomaly_context=analysis_result
                                    )

                                    # Fix: Safely check for errors and handle None response
                                    if ai_suggestions and isinstance(ai_suggestions, dict) and not ai_suggestions.get("error"):
                                        # Convert to remediation action objects
                                        remediation_actions = []
                                        for step in ai_suggestions.get("steps", []):
                                            remediation_actions.append(
                                                RemediationAction(
                                                    action_type="command",
                                                    resource_type=entity_type,
                                                    resource_name=entity_id,
                                                    namespace=namespace,
                                                    parameters={"command": step}
                                                )
                                            )

                                        # Decide remediation based on mode and criticality
                                        await self._decide_remediation(
                                            event=await self.anomaly_event_service.get_event_by_id(anomaly_id),
                                            ai_suggestions=remediation_actions,
                                            is_critical=is_critical,
                                            is_predicted=is_primarily_prediction
                                        )
                                    else:
                                        # Fix: Safely handle error messages
                                        error_msg = "Unknown error"
                                        if isinstance(ai_suggestions, dict) and "error" in ai_suggestions:
                                            error_msg = ai_suggestions.get("error")
                                        elif isinstance(ai_suggestions, dict) and "details" in ai_suggestions:
                                            error_msg = ai_suggestions.get("details")
                                        elif ai_suggestions is None:
                                            error_msg = "No response from AI service"
                                        logger.warning(f"AI analysis failed for anomaly {anomaly_id}: {error_msg}")
                            else:
                                logger.warning(f"AI analysis failed for anomaly {anomaly_id}: {analysis_result.get('error', 'Unknown error')}")
                        except Exception as ai_error:
                            logger.error(f"Error during AI analysis for anomaly {anomaly_id}: {ai_error}", exc_info=True)
                else:
                    logger.error(f"Failed to create/update anomaly event for {entity_key}")
            else:
                # Check if there was a previously active event that is now resolved
                active_event = await self.anomaly_event_service.find_active_event(
                    entity_id=entity_id, entity_type=entity_type, namespace=namespace
                )

                if active_event and active_event.status not in [AnomalyStatus.RESOLVED]:
                    logger.info(f"Entity {entity_key} appears to have recovered. Marking event {active_event.anomaly_id} as resolved.")
                    await self.anomaly_event_service.update_event(
                        active_event.anomaly_id,
                        AnomalyEventUpdate(
                            status=AnomalyStatus.RESOLVED,
                            resolution_time=datetime.utcnow(),
                            notes=f"{active_event.notes}\nResolved: Entity returned to normal state with score {anomaly_score:.4f}"
                        )
                    )
                else:
                    logger.debug(f"No anomaly detected for {entity_key} (score: {anomaly_score:.4f})")

        except Exception as e:
            logger.error(f"Error in anomaly prediction for {entity_key}: {e}", exc_info=True)

    async def _decide_remediation(
        self,
        event: AnomalyEvent,
        ai_suggestions: List[RemediationAction],
        is_critical: bool = False,
        is_predicted: bool = False,
        is_direct_failure: bool = False,
    ):
        """
        Decide what remediation action to take based on mode, criticality, and AI suggestions.

        Args:
            event: The anomaly event to remediate
            ai_suggestions: List of AI-suggested remediation actions
            is_critical: Whether the anomaly is critical
            is_predicted: Whether the anomaly is predicted (not yet occurred)
            is_direct_failure: Whether the anomaly was detected via direct failure check
        """
        current_mode = mode_service.get_mode()
        logger.debug(f"Deciding remediation for event {event.anomaly_id} in mode {current_mode}")

        # Filter suggestions to only include valid RemediationAction objects
        valid_suggestions = []

        for suggestion in ai_suggestions:
            # Handle string suggestions by converting them to RemediationAction objects
            if isinstance(suggestion, str):
                suggestion = RemediationAction(
                    action_type="command",
                    resource_type=event.entity_type,
                    resource_name=event.entity_id,
                    namespace=event.namespace,
                    parameters={"command": suggestion},
                    confidence=0.5,  # Default confidence
                    command=suggestion,  # Directly set the command attribute
                )

            if not isinstance(suggestion, RemediationAction):
                # Skip invalid suggestions that don't match our model
                continue

            # Extract the action type from the command if not specified
            if suggestion.action_type == "command" and not suggestion.command:
                cmd = suggestion.parameters.get("command", "")
                if cmd:
                    parts = cmd.split()
                    if parts and parts[0] in k8s_executor.command_templates:
                        suggestion.action_type = parts[0]
                    # Set the command attribute to ensure it's available
                    suggestion.command = cmd

            # Ensure action_type is one of our supported template types
            if suggestion.action_type not in k8s_executor.command_templates:
                logger.warning(f"Skipping unsupported action type: {suggestion.action_type}")
                continue

            # Check for required parameters based on action type
            try:
                # This will validate the parameters against the template
                formatted_cmd = k8s_executor.format_command_from_template(
                    suggestion.action_type,
                    suggestion.parameters
                )
                suggestion.command = formatted_cmd
                valid_suggestions.append(suggestion)
            except Exception as e:
                logger.warning(f"Invalid parameters for {suggestion.action_type}: {e}")
                continue

        if not valid_suggestions:
            # Enhanced fallbacks: Generate remediation suggestions based on entity type and failure indicators
            logger.info(f"No valid AI-suggested remediations, generating fallbacks for {event.entity_type}/{event.entity_id}")

            try:
                event_notes = str(event.notes or "").lower()
                status = str(event.status or "").lower()

                # For pods in crash loops, suggest restart
                if event.entity_type == "pod":
                    if ("crashloop" in event_notes or "restart" in event_notes or
                        "oomkilled" in event_notes or "crash" in event_notes):
                        restart_action = RemediationAction(
                            action_type="restart_pod",
                            resource_type="pod",
                            resource_name=event.entity_id,
                            namespace=event.namespace,
                            parameters={
                                "pod_name": event.entity_id,
                                "namespace": event.namespace or "default",
                                "force_restart": "crashloop" in event_notes or "crash" in event_notes  # Enable force_restart for CrashLoopBackOff pods
                            },
                            confidence=0.7,
                            command=f"kubectl delete pod {event.entity_id} -n {event.namespace or 'default'} --grace-period=0"
                        )
                        valid_suggestions.append(restart_action)
                        logger.info(f"Added fallback restart_pod suggestion for {event.entity_id}" +
                                   (" with force_restart=True for CrashLoopBackOff" if "crashloop" in event_notes else ""))

                    # For OOMKilled pods, suggest increasing memory limits
                    if "oomkilled" in event_notes or "out of memory" in event_notes:
                        # Get the deployment that owns this pod if possible
                        try:
                            owner_ref = await k8s_executor.get_pod_owner(
                                event.entity_id,
                                event.namespace or "default"
                            )
                            if owner_ref and owner_ref.get("kind") and owner_ref.get("name"):
                                owner_kind = owner_ref.get("kind").lower()
                                owner_name = owner_ref.get("name")

                                scale_action = RemediationAction(
                                    action_type="increase_resources",
                                    resource_type=owner_kind,
                                    resource_name=owner_name,
                                    namespace=event.namespace,
                                    parameters={
                                        "name": owner_name,
                                        "namespace": event.namespace or "default",
                                        "resource": "memory",
                                        "increase_factor": "1.5"
                                    },
                                    confidence=0.6,
                                    command=f"kubectl patch {owner_kind} {owner_name} -n {event.namespace or 'default'} --type='json' -p='[{{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/resources/limits/memory\", \"value\": \"1Gi\"}}]'"
                                )
                                valid_suggestions.append(scale_action)
                                logger.info(f"Added fallback increase_resources suggestion for {owner_kind}/{owner_name}")
                        except Exception as e:
                            logger.warning(f"Error getting pod owner for resource increase suggestion: {e}")

                    # For pods with image pull issues
                    if "imagepullbackoff" in event_notes or "errimagepull" in event_notes:
                        logs_action = RemediationAction(
                            action_type="view_logs",
                            resource_type="pod",
                            resource_name=event.entity_id,
                            namespace=event.namespace,
                            parameters={
                                "name": event.entity_id,
                                "namespace": event.namespace or "default"
                            },
                            confidence=0.8,
                            command=f"kubectl describe pod {event.entity_id} -n {event.namespace or 'default'}"
                        )
                        valid_suggestions.append(logs_action)
                        logger.info(f"Added fallback view_logs suggestion for {event.entity_id} with image pull issues")

                # For deployment issues, suggest scaling up
                elif event.entity_type == "deployment":
                    # For deployments with unavailable replicas or degraded status
                    if ("unavailable" in event_notes or "degraded" in status or
                        "progress" in status):
                        scale_action = RemediationAction(
                            action_type="scale_deployment",
                            resource_type="deployment",
                            resource_name=event.entity_id,
                            namespace=event.namespace,
                            parameters={
                                "name": event.entity_id,
                                "namespace": event.namespace or "default",
                                "replicas": "3"
                            },
                            confidence=0.6,
                            command=f"kubectl scale deployment {event.entity_id} -n {event.namespace or 'default'} --replicas=3"
                        )
                        valid_suggestions.append(scale_action)
                        logger.info(f"Added fallback scale_deployment suggestion for {event.entity_id}")

                # For node issues, suggest cordoning if critical
                elif event.entity_type == "node" and is_critical:
                    cordon_action = RemediationAction(
                        action_type="cordon_node",
                        resource_type="node",
                        resource_name=event.entity_id,
                        namespace=None,
                        parameters={
                            "name": event.entity_id
                        },
                        confidence=0.7,
                        command=f"kubectl cordon {event.entity_id}"
                    )
                    valid_suggestions.append(cordon_action)
                    logger.info(f"Added fallback cordon_node suggestion for {event.entity_id}")

                # Generic fallback for any entity if no specific remediation found
                if not valid_suggestions:
                    # Always add a diagnostic action as last resort
                    describe_action = RemediationAction(
                        action_type="describe",
                        resource_type=event.entity_type,
                        resource_name=event.entity_id,
                        namespace=event.namespace,
                        parameters={
                            "name": event.entity_id,
                            "namespace": event.namespace or "default" if event.namespace else None
                        },
                        confidence=0.5,
                        command=f"kubectl describe {event.entity_type} {event.entity_id} {'-n ' + event.namespace if event.namespace else ''}"
                    )
                    valid_suggestions.append(describe_action)
                    logger.info(f"Added generic fallback describe suggestion for {event.entity_type}/{event.entity_id}")
            except Exception as e:
                logger.warning(f"Error creating fallback remediation suggestions: {e}")

        if not valid_suggestions:
            logger.warning(f"No valid remediation suggestions for {event.anomaly_id}")
            return

        # Sort by priority: higher confidence first
        valid_suggestions.sort(key=lambda x: x.confidence or 0.0, reverse=True)

        # Log the valid suggestions
        logger.info(f"Valid remediation suggestions for {event.anomaly_id}: {len(valid_suggestions)}")
        for i, sugg in enumerate(valid_suggestions[:3]):  # Log first 3
            logger.info(f"  Suggestion {i+1}: {sugg.action_type} - {sugg.command}")

        # Check for auto-remediation applicability
        # Fix: Accept both "autonomous" and "AUTO" as valid auto-remediation mode values
        can_auto_remediate = (
            (current_mode == "autonomous" or current_mode == "AUTO")
            and (is_critical or is_direct_failure or
                (event.status in [AnomalyStatus.CRITICAL_FAILURE, AnomalyStatus.FAILURE_DETECTED]))
        )

        # In learning mode, we only update the event with suggestions
        if current_mode == "learning":
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(
                    suggested_remediations=valid_suggestions,
                    status=AnomalyStatus.REMEDIATION_SUGGESTED,
                )
            )
            logger.info(f"Added {len(valid_suggestions)} remediation suggestions to event {event.anomaly_id} (learning mode)")
            return

        # For assisted mode, suggest but don't apply
        if current_mode == "assisted":
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(
                    suggested_remediations=valid_suggestions,
                    status=AnomalyStatus.REMEDIATION_SUGGESTED,
                )
            )
            logger.info(f"Added {len(valid_suggestions)} remediation suggestions to event {event.anomaly_id} (assisted mode)")
            return

        # For autonomous mode with auto-remediation qualification
        if can_auto_remediate:
            # Get the highest confidence suggestion
            best_suggestion = valid_suggestions[0]

            # For critical actions, verify dependencies
            if best_suggestion.action_type in ["delete_deployment", "delete_pod", "drain_node"]:
                # Extract the resource type and name for dependency checking
                resource_type = best_suggestion.resource_type
                resource_name = best_suggestion.resource_name
                namespace = best_suggestion.namespace

                deps = await k8s_executor.check_dependencies(
                    resource_type=resource_type,
                    name=resource_name,
                    namespace=namespace or "default"
                )

                if deps and len(deps) > 0:
                    logger.warning(f"Cannot auto-remediate: {best_suggestion.action_type} has dependencies: {deps}")
                    # Downgrade to suggestion only
                    await self.anomaly_event_service.update_event(
                        event.anomaly_id,
                        AnomalyEventUpdate(
                            suggested_remediations=valid_suggestions,
                            notes=f"Auto-remediation blocked due to dependencies: {deps}",
                            status=AnomalyStatus.REMEDIATION_SUGGESTED,
                        )
                    )
                    return

            # Execute the best suggestion
            try:
                logger.info(f"Executing auto-remediation: {best_suggestion.command}")
                result = await k8s_executor.execute_remediation_action(best_suggestion)

                # Create a remediation attempt record
                attempt = RemediationAttempt(
                    command=best_suggestion.command,
                    action_type=best_suggestion.action_type,
                    parameters=best_suggestion.parameters,
                    output=result.get("output", ""),
                    status="success" if result.get("success") else "failed",
                    timestamp=datetime.utcnow(),
                    error=result.get("error", ""),
                )

                # Update the event with the attempt
                await self.anomaly_event_service.update_remediation_status(
                    event.anomaly_id,
                    attempt=attempt,
                    suggested_remediations=valid_suggestions,
                    status=AnomalyStatus.REMEDIATION_APPLIED
                      if attempt.status == "success"
                      else AnomalyStatus.REMEDIATION_FAILED,
                )

                logger.info(f"Auto-remediation applied for event {event.anomaly_id}: {attempt.status}")

                # Schedule verification if successful
                if attempt.status == "success":
                    # Schedule verification after a delay to allow changes to take effect
                    verification_delay = settings.REMEDIATION_VERIFICATION_DELAY_SECONDS or 60
                    loop = asyncio.get_event_loop()
                    loop.call_later(
                        verification_delay,
                        lambda: asyncio.create_task(self._verify_remediation(event)),
                    )
            except Exception as e:
                logger.error(f"Error during auto-remediation: {e}", exc_info=True)
                # Update event with error
                await self.anomaly_event_service.update_event(
                    event.anomaly_id,
                    AnomalyEventUpdate(
                        notes=f"Auto-remediation error: {e}",
                        status=AnomalyStatus.REMEDIATION_FAILED,
                    )
                )
        else:
            # Just add suggestions without applying
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(
                    suggested_remediations=valid_suggestions,
                    status=AnomalyStatus.REMEDIATION_SUGGESTED,
                    notes=f"Auto-remediation not applicable: critical={is_critical}, mode={current_mode}",
                )
            )

    async def _verify_remediation(self, event: AnomalyEvent):
        """
        Verify if remediation was successful using specific, targeted checks based on the remediation action.
        This method performs rule-based verification specific to the type of remediation that was applied,
        rather than relying solely on generic anomaly scores or AI assessment.

        Args:
            event: The anomaly event to verify
        """
        logger.info(
            f"Verifying remediation for event {event.anomaly_id} ({event.entity_id})"
        )
        verification_notes = f"Verification check at {datetime.utcnow().isoformat()}. "
        final_status = AnomalyStatus.REMEDIATION_FAILED  # Default to failed
        verification_checks = []  # Track specific checks performed
        verification_reason = "Generic verification failed"

        # Exit early if no remediation attempts were made
        if not event.remediation_attempts:
            verification_notes += "No remediation attempts recorded, nothing to verify."
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(status=final_status, notes=verification_notes),
            )
            return

        try:
            # Get the last remediation attempt
            last_attempt = event.remediation_attempts[-1]
            command = last_attempt.command
            command_parts = command.split()
            operation = command_parts[0] if command_parts else "unknown"

            # 1. Fetch current metrics for the entity - do this first to avoid undefined variables later
            entity_key = f"{event.entity_type}/{event.entity_id}"
            if event.namespace:
                entity_key = f"{event.namespace}/{entity_key}"

            all_current_metrics = await prometheus_scraper.fetch_all_metrics(
                self.active_promql_queries
            )

            current_entity_data = all_current_metrics.get(entity_key, {})
            current_metrics_snapshot = (
                current_entity_data.get("metrics", []) if current_entity_data else []
            )

            # 2. Get detailed K8s resource status
            current_k8s_status = {}
            resource_exists = True  # Track if resource still exists
            try:
                if event.entity_type and event.entity_id:
                    current_k8s_status = await k8s_executor.get_resource_status(
                        resource_type=event.entity_type,
                        name=event.entity_id,
                        namespace=event.namespace or "default",
                    )
                    verification_checks.append(f"Checked {event.entity_type} status")
            except Exception as e:
                # If resource not found after delete operation, that's actually successful
                if "not found" in str(e).lower() and operation in [
                    "delete_pod",
                    "delete_deployment",
                ]:
                    resource_exists = False
                    verification_notes += (
                        f"Resource no longer exists (expected after deletion). "
                    )
                    verification_checks.append("Verified resource was deleted")
                else:
                    logger.warning(
                        f"Could not fetch K8s status during verification: {e}"
                    )
                    verification_notes += f"K8s status fetch error: {str(e)}. "

            # 3. Operation-specific verification logic
            is_resolved = False

            # Pod operations
            if operation in ["restart_pod", "delete_pod"]:
                if not resource_exists and operation == "delete_pod":
                    # For delete operations, non-existence is success
                    is_resolved = True
                    verification_reason = "Pod was successfully deleted"
                elif resource_exists:
                    # For restart operations, check pod is now Running and Ready
                    pod_phase = current_k8s_status.get("status", {}).get("phase", "")
                    container_statuses = current_k8s_status.get("status", {}).get(
                        "containerStatuses", []
                    )
                    all_containers_ready = (
                        all(status.get("ready", False) for status in container_statuses)
                        if container_statuses
                        else False
                    )
                    restart_count_reduced = False

                    # Check if restart count threshold breaches are fixed
                    for metric in current_metrics_snapshot:
                        metric_type = metric.get("metric_type", "")
                        metric_value = metric.get("value", 0)
                        if metric_type == "container_restarts_rate" and metric_value is not None:
                            # If restart count is low, consider this fixed
                            if metric_value < 3:  # Arbitrary threshold
                                restart_count_reduced = True
                                break

                    if pod_phase == "Running" and all_containers_ready:
                        is_resolved = True
                        verification_reason = f"Pod is now in Running state and all containers are ready"
                    elif restart_count_reduced:
                        is_resolved = True
                        verification_reason = f"Pod restart count is now below threshold"

                    verification_checks.append(f"Checked pod phase: {pod_phase}")
                    verification_checks.append(f"Checked container readiness: {all_containers_ready}")


            # Deployment operations
            elif operation in ["restart_deployment", "scale_deployment"]:
                if resource_exists:
                    replicas = current_k8s_status.get("spec", {}).get("replicas", 0)
                    available_replicas = current_k8s_status.get("status", {}).get("availableReplicas", 0)
                    ready_replicas = current_k8s_status.get("status", {}).get("readyReplicas", 0)

                    if available_replicas > 0 and available_replicas >= replicas * 0.8:  # 80% of desired replicas
                        is_resolved = True
                        verification_reason = f"Deployment has {available_replicas}/{replicas} available replicas"

                    verification_checks.append(f"Checked deployment replicas: {available_replicas}/{replicas}")

            # Node operations
            elif operation in ["cordon_node", "drain_node", "uncordon_node"]:
                if resource_exists:
                    node_conditions = current_k8s_status.get("status", {}).get("conditions", [])
                    scheduling_status = None

                    for condition in node_conditions:
                        if condition.get("type") == "Ready":
                            scheduling_status = condition.get("status")
                            break

                    if operation == "cordon_node" and scheduling_status == "False":
                        is_resolved = True
                        verification_reason = "Node successfully cordoned"
                    elif operation == "uncordon_node" and scheduling_status == "True":
                        is_resolved = True
                        verification_reason = "Node successfully uncordoned"
                    elif operation == "drain_node":
                        # For drain, check if node has no pods (except system daemonsets)
                        pod_count = await k8s_executor.count_pods_on_node(event.entity_id)
                        if pod_count < 3:  # Allow for some system pods
                            is_resolved = True
                            verification_reason = f"Node successfully drained (remaining pods: {pod_count})"

                    verification_checks.append(f"Checked node scheduling status: {scheduling_status}")

            # StatefulSet operations
            elif operation in ["restart_statefulset", "scale_statefulset"]:
                if resource_exists:
                    replicas = current_k8s_status.get("spec", {}).get("replicas", 0)
                    ready_replicas = current_k8s_status.get("status", {}).get("readyReplicas", 0)

                    if ready_replicas > 0 and ready_replicas >= replicas:
                        is_resolved = True
                        verification_reason = f"StatefulSet has {ready_replicas}/{replicas} ready replicas"

                    verification_checks.append(f"Checked statefulset replicas: {ready_replicas}/{replicas}")

            # DaemonSet operations
            elif operation == "restart_daemonset":
                if resource_exists:
                    desired_pods = current_k8s_status.get("status", {}).get("desiredNumberScheduled", 0)
                    available_pods = current_k8s_status.get("status", {}).get("numberAvailable", 0)

                    if available_pods > 0 and available_pods >= desired_pods:
                        is_resolved = True
                        verification_reason = f"DaemonSet has {available_pods}/{desired_pods} available pods"

                    verification_checks.append(f"Checked daemonset pods: {available_pods}/{desired_pods}")

            # Resource adjustment operations
            elif operation == "adjust_resources":
                # Get original issue to verify it's fixed
                original_issue = ""
                if (
                    "cpu_usage_percent" in event.notes
                    or "memory_usage_percent" in event.notes
                ):
                    original_issue = "resource_usage"

                # Check current resource usage
                resource_usage_improved = False
                for metric in current_metrics_snapshot:
                    metric_type = metric.get("metric_type", "")
                    metric_value = metric.get("value", 0)

                    if "cpu_usage" in metric_type and metric_value is not None:
                        # If CPU usage is now below threshold (e.g., 80%)
                        if metric_value < 0.8:  # 80% usage
                            resource_usage_improved = True
                    elif "memory_usage" in metric_type and metric_value is not None:
                        # If memory usage is now below threshold
                        if metric_value < 0.8 * (1024 * 1024 * 1024):  # 80% of 1GB
                            resource_usage_improved = True

                if resource_usage_improved:
                    is_resolved = True
                    verification_reason = "Resource usage has improved after adjustment"

                verification_checks.append(f"Checked resource usage metrics")

            # 4. Check for any threshold breaches or failures in current metrics
            has_threshold_breach = False
            has_actual_failure = False
            current_pod_state_issues = []

            # Define critical thresholds (same as in anomaly detection)
            critical_thresholds = {
                "cpu_usage_percent": 90.0,  # 90% CPU usage
                "memory_usage_percent": 90.0,  # 90% memory usage
                "pod_status_ready": 0.0,  # Not ready
                "container_restarts": 3.0,  # More than 3 restarts recently
                "deployment_unavailable_replicas": 0.0,  # Any unavailable replicas
            }

            # Pod waiting reason indicators
            pod_waiting_reasons = [
                "CrashLoopBackOff",
                "ImagePullBackOff",
                "ErrImagePull",
                "CreateContainerError",
                "ContainerCreating",
                "ContainerCannotRun",
            ]

            # Check metrics
            for metric in current_metrics_snapshot:
                metric_name = metric.get("metric", "")
                metric_value = metric.get("value", 0.0)
                metric_labels = metric.get("labels", {})

                # Check for container waiting reasons
                if "kube_pod_container_status_waiting_reason" in metric_name:
                    reason = metric_labels.get("reason", "")
                    if reason in pod_waiting_reasons and metric_value > 0:
                        current_pod_state_issues.append(f"Container {reason}")
                        has_actual_failure = True

                # Check for pod phase issues
                if "kube_pod_status_phase" in metric_name:
                    phase = metric_labels.get("phase", "")
                    if phase in ["Failed", "Unknown"] and metric_value > 0:
                        current_pod_state_issues.append(f"Pod phase {phase}")
                        has_actual_failure = True

                # Check for threshold breaches
                for threshold_key, threshold_value in critical_thresholds.items():
                    if threshold_key in metric_name:
                        if metric_value > threshold_value:
                            has_threshold_breach = True
                            break

            # 5. Decide final verification status
            # If we have specific operation results, trust those first
            if is_resolved:
                final_status = AnomalyStatus.RESOLVED
            else:
                # Otherwise check if the original issues are fixed
                has_original_issues = False

                event_notes = str(event.notes or "")

                # Compare with original anomaly conditions
                if "ThresholdBreach" in str(event.status) and has_threshold_breach:
                    has_original_issues = True
                elif "PodStateIssue" in str(event.status) and current_pod_state_issues:
                    has_original_issues = True
                elif (
                    "FailureDetected" in str(event.status)
                    or "CriticalFailure" in str(event.status)
                ) and (has_actual_failure or has_threshold_breach):
                    has_original_issues = True

                # If no issues found from original type, consider resolved
                if not has_original_issues:
                    final_status = AnomalyStatus.RESOLVED
                    verification_reason = "Original issues appear to be resolved"

                # Additional fallback checks
                if final_status == AnomalyStatus.REMEDIATION_FAILED:
                    # Check for overall improvement - if we have historical anomaly scores
                    if hasattr(self, 'latest_anomaly_scores'):
                        current_score = self.latest_anomaly_scores.get(entity_key, 0)
                        if current_score < event.anomaly_score * 0.7:  # Significant improvement
                            final_status = AnomalyStatus.RESOLVED
                            verification_reason = f"Anomaly score improved from {event.anomaly_score:.4f} to {current_score:.4f}"

            # 6. Use Gemini AI verification as additional confirmation if enabled
            if self.gemini_service and settings.GEMINI_AUTO_VERIFICATION:
                logger.debug(
                    f"Using Gemini for additional verification insight on {event.anomaly_id}"
                )
                try:
                    # Prepare context for AI verification
                    verification_context = {
                        "entity": {
                            "entity_id": event.entity_id,
                            "entity_type": event.entity_type,
                            "namespace": event.namespace,
                        },
                        "remediation": {
                            "action": operation,
                            "output": last_attempt.output,
                            "status": last_attempt.status,
                        },
                        "original_issue": {
                            "status": str(event.status),
                            "anomaly_score": event.anomaly_score,
                            "notes": event.notes,
                        },
                        "current_state": {
                            "metrics": current_metrics_snapshot[:5],  # First 5 for brevity
                            "resource_exists": resource_exists,
                            "k8s_status": current_k8s_status,
                            "issues_detected": current_pod_state_issues,
                        },
                    }

                    # Get AI verification assessment
                    ai_verification = await self.gemini_service.verify_remediation(
                        verification_context
                    )

                    if ai_verification and ai_verification.get("success") is not None:
                        # AI is confident in a determination
                        if ai_verification.get("confidence", 0) > 0.7:
                            ai_result = ai_verification.get("success")
                            # If AI says resolved and we're not sure, trust AI
                            if ai_result and final_status == AnomalyStatus.REMEDIATION_FAILED:
                                final_status = AnomalyStatus.RESOLVED
                                verification_reason = f"AI assessment: {ai_verification.get('reasoning')}"
                            # If AI says failed and we thought resolved, be conservative
                            elif not ai_result and final_status == AnomalyStatus.RESOLVED:
                                # Only override if AI is very confident
                                if ai_verification.get("confidence", 0) > 0.9:
                                    final_status = AnomalyStatus.REMEDIATION_FAILED
                                    verification_reason = f"AI assessment: {ai_verification.get('reasoning')}"

                        # Add AI insight to notes
                        verification_notes += f"\nAI Verification: {ai_verification.get('reasoning')} (Confidence: {ai_verification.get('confidence'):.2f})"
                        verification_checks.append("AI verification assessment")

                except Exception as e:
                    logger.warning(f"Error in AI verification: {e}")
                    verification_notes += f"\nAI verification error: {str(e)}"

            # Add detailed notes about what was checked
            verification_notes += f"\nVerification result: {final_status.value}. Reason: {verification_reason}. "
            verification_notes += (
                f"\nChecks performed: {'; '.join(verification_checks)}."
            )
            if current_pod_state_issues:
                verification_notes += f"\nCurrent pod state issues: {'; '.join(current_pod_state_issues)}."
            if has_threshold_breach:
                verification_notes += f"\nSome metrics still exceed thresholds."
        except Exception as e:
            logger.error(
                f"Error during verification of event {event.anomaly_id}: {e}",
                exc_info=True,
            )
            verification_notes += f"\nError during verification: {str(e)}"
            # Keep status as failed on exception

        # Update the event with verification results
        await self.anomaly_event_service.update_event(
            event.anomaly_id,
            AnomalyEventUpdate(
                status=final_status,
                notes=verification_notes,
                resolution_time=(
                    datetime.utcnow()
                    if final_status == AnomalyStatus.RESOLVED
                    else None
                ),
            ),
        )
        logger.info(
            f"Verification completed for event {event.anomaly_id}: {final_status.value}"
        )

    async def _fetch_metrics(self):
        """
        Fetch metrics from Prometheus using the active queries.

        Returns:
            Dict of entity metrics keyed by entity identifier
        """
        logger.info("Fetching metrics from Prometheus...")
        try:
            # Use centralized query module for working queries
            from app.core.queries import get_default_query_list, get_fallback_query_list
            from app.utils.metric_tagger import MetricTagger

            # Log what queries we'll execute for debugging
            logger.debug(f"Will execute these PromQL queries: {self.active_promql_queries}")

            # Make a copy of active queries
            queries_to_execute = list(self.active_promql_queries)

            # Ensure we're using the known working queries for required metrics
            if len(queries_to_execute) == 0 or not self.is_initialized:
                logger.warning("No active queries defined, using known working queries")
                queries_to_execute = get_default_query_list()
                # If default query list is empty, use fallback
                if not queries_to_execute:
                    queries_to_execute = get_fallback_query_list()

            logger.info(f"Executing {len(queries_to_execute)} PromQL queries")

            # Fetch all metrics
            all_metrics = await prometheus_scraper.fetch_all_metrics(queries_to_execute)

            # MetricTagger takes care of tagging metrics, but we can apply batch tagging
            # to ensure any missed metrics are properly tagged
            all_metrics = MetricTagger.tag_metrics_batch(all_metrics)

            # Count and log metrics fetched
            entity_count = len(all_metrics)
            metrics_count = sum(len(entity_data.get("metrics", [])) for entity_data in all_metrics.values())
            logger.info(f"Fetched {metrics_count} metrics for {entity_count} entities")

            # Log detailed metrics info for a sample entity for debugging
            if entity_count > 0:
                sample_entity_key = next(iter(all_metrics.keys()))
                sample_entity = all_metrics[sample_entity_key]
                sample_metrics = sample_entity.get("metrics", [])
                logger.debug(f"Sample entity {sample_entity_key} has {len(sample_metrics)} metrics")

                for idx, metric in enumerate(sample_metrics[:3]):  # Show first 3 metrics only
                    logger.debug(f"  Metric {idx+1}: type={metric.get('metric_type', 'unknown')}")
                    logger.debug(f"    Name: {metric.get('metric', 'unnamed')}")
                    logger.debug(f"    Query: {metric.get('query', '')[:50]}")
                    if metric.get('value') is not None:
                        logger.debug(f"    Value: {metric.get('value')}")
                    elif metric.get('values'):
                        logger.debug(f"    Values array length: {len(metric.get('values', []))}")

            return all_metrics
        except Exception as e:
            logger.error(f"Error fetching metrics: {e}", exc_info=True)
            return {}

    async def _process_entities(self, all_metrics):
        """
        Process metrics and detect/handle anomalies for each entity.

        Args:
            all_metrics: Dict of entity metrics from _fetch_metrics
        """
        logger.info(f"Processing metrics for {len(all_metrics)} entities")

        # Create processing tasks for each entity
        process_tasks = [
            self._process_entity_metrics(key, data)
            for key, data in all_metrics.items()
        ]

        # Execute all tasks in parallel
        await asyncio.gather(*process_tasks, return_exceptions=True)

        logger.info("Entity metrics processing complete")

    async def _handle_verification(self):
        """
        Handle verification of pending remediations.
        """
        logger.info("Checking for events pending verification")

        try:
            # Get events that need verification
            events_to_verify = await self.anomaly_event_service.list_events(
                status=AnomalyStatus.VERIFICATION_PENDING
            )

            if not events_to_verify:
                logger.debug("No events pending verification")
                return

            # Filter for events that have passed the verification delay
            verification_time_threshold = datetime.utcnow() - timedelta(
                seconds=settings.VERIFICATION_DELAY_SECONDS
            )

            events_to_verify = [
                e for e in events_to_verify
                if e.verification_time and e.verification_time <= datetime.utcnow()
            ]

            if events_to_verify:
                logger.info(f"Found {len(events_to_verify)} events ready for verification")

                # Process verifications in parallel
                verification_tasks = [
                    self._verify_remediation(event) for event in events_to_verify
                ]
                await asyncio.gather(*verification_tasks, return_exceptions=True)
            else:
                logger.debug("No events ready for verification yet")

        except Exception as e:
            logger.error(f"Error handling verification: {e}", exc_info=True)

    async def run_cycle(self):
        """Runs a single cycle of fetch, detect, remediate, verify."""
        logger.info(f"Starting worker cycle... Mode: {mode_service.get_mode()}")

        if not self.is_initialized:
            await self._initialize_queries()  # Initialize queries on first run

        try:
            # 1. Fetch metrics from Prometheus
            all_metrics = await self._fetch_metrics()

            # 2. Process metrics and detect/handle anomalies for each entity
            await self._process_entities(all_metrics)

            # 3. Handle verification of pending remediations
            await self._handle_verification()

            # 4. Cleanup stale entries in metric history
            await self._cleanup_stale_metric_history()

            # 5. Direct scan for failures
            await self._direct_scan_for_failures()

            # 6. Generate cluster overview summary
            await self._generate_cluster_overview(all_metrics)

        except Exception as e:
            logger.error(f"CRITICAL Error in worker cycle: {e}", exc_info=True)

        logger.info("Worker cycle finished.")

    async def _cleanup_stale_metric_history(self):
        """
        Cleanup stale entries in the entity_metric_history.
        """
        current_time = datetime.utcnow()
        if (
            current_time - self.last_cleanup_time
        ).total_seconds() < self.cleanup_interval_seconds:
            return  # Skip cleanup if interval has not passed

        logger.info("Cleaning up stale entries in entity_metric_history...")
        stale_threshold = current_time - timedelta(
            seconds=self.stale_entity_timeout_seconds
        )
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
            all_pods = await list_resources("pod")
            failed_pods: List[Dict[str, Any]] = []
            for pod in all_pods:
                status = pod.get("status", {})
                # Phase 'Failed' indicates pod failure
                if status.get("phase") == "Failed":
                    failed_pods.append(pod)
                    continue
                # Inspect container statuses for errors, OOM, crash loops, image pulls
                for cs in status.get("container_statuses", []):
                    state = cs.get("state", {}) or {}
                    last_state = cs.get("last_state", {}) or {}
                    # Check terminated state
                    term = state.get("terminated") or last_state.get("terminated") or {}
                    reason = term.get("reason")
                    exit_code = term.get("exit_code")
                    if reason in ("OOMKilled", "Error") or (exit_code is not None and exit_code != 0):
                        failed_pods.append(pod)
                        break
                    # Check waiting state
                    waiting = state.get("waiting", {}) or {}
                    wait_reason = waiting.get("reason")
                    if wait_reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                        failed_pods.append(pod)
                        break

            # 2. Unhealthy deployments
            unhealthy_deployments = await k8s_executor.list_resources_with_status(
                resource_type="deployment",
                status_filter=["Degraded", "ProgressDeadlineExceeded"],
            )

            # 3. Nodes with issues
            problematic_nodes = await k8s_executor.list_resources_with_status(
                resource_type="node",
                status_filter=["NotReady", "SchedulingDisabled", "Pressure"],
            )

            # 4. Services with endpoints issues
            services_with_issues = await k8s_executor.list_resources_with_status(
                resource_type="service",
                status_filter=["NoEndpoints", "EndpointsNotReady"],
            )

            # 5. PVCs with issues
            pvc_with_issues = await k8s_executor.list_resources_with_status(
                resource_type="persistentvolumeclaim",
                status_filter=["Pending", "Lost", "Bound=False"],
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
                    resource_data=pod,
                )

            # Process unhealthy deployments
            for deployment in unhealthy_deployments:
                failure_count += 1
                conditions = deployment.get("status", {}).get("conditions", [])
                reason = "Unknown"
                message = "Deployment in degraded state"
                for condition in conditions:
                    if condition.get("status") != "True" and condition.get("type") in [
                        "Available",
                        "Progressing",
                    ]:
                        reason = condition.get("reason", "DeploymentIssue")
                        message = condition.get(
                            "message", "Deployment has availability issues"
                        )
                await self._handle_direct_failure(
                    entity_type="deployment",
                    entity_id=deployment.get("metadata", {}).get("name"),
                    namespace=deployment.get("metadata", {}).get("namespace"),
                    status="Degraded",
                    reason=reason,
                    message=message,
                    resource_data=deployment,
                )

            # Process problematic nodes
            for node in problematic_nodes:
                failure_count += 1
                conditions = node.get("status", {}).get("conditions", [])
                reason = "Unknown"
                message = "Node issue detected"
                for condition in conditions:
                    if (
                        condition.get("type") == "Ready"
                        and condition.get("status") != "True"
                    ):
                        reason = condition.get("reason", "NodeNotReady")
                        message = condition.get("message", "Node is not in Ready state")
                    elif (
                        condition.get("type")
                        in [
                            "DiskPressure",
                            "MemoryPressure",
                            "NetworkUnavailable",
                            "PIDPressure",
                        ]
                        and condition.get("status") == "True"
                    ):
                        reason = condition.get(
                            "reason", f"{condition.get('type')}Issue"
                        )
                        message = condition.get(
                            "message", f"Node has {condition.get('type')} condition"
                        )
                await self._handle_direct_failure(
                    entity_type="node",
                    entity_id=node.get("metadata", {}).get("name"),
                    namespace=None,
                    status="Problematic",
                    reason=reason,
                    message=message,
                    resource_data=node,
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
                    resource_data=service,
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
                    resource_data=pvc,
                )

            if failure_count > 0:
                logger.warning(
                    f"Direct scan found {failure_count} existing failures in the cluster"
                )
            else:
                logger.info("Direct scan completed. No existing failures found.")

            return failure_count
        except Exception as e:
            logger.error(f"Error during direct failure scan: {e}", exc_info=True)
            return 0

    async def _handle_direct_failure(
        self,
        entity_type: str,
        entity_id: str,
        namespace: Optional[str],
        status: str,
        reason: str,
        message: str,
        resource_data: Dict[str, Any],
    ):
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
                entity_id=entity_id, entity_type=entity_type, namespace=namespace
            )

            if active_event:
                logger.info(
                    f"Active event already exists for {entity_type}/{entity_id}: {active_event.anomaly_id}"
                )
                # Add a note about this direct detection if it's not already in remediation
                if active_event.status not in [
                    AnomalyStatus.VERIFICATION_PENDING,
                    AnomalyStatus.REMEDIATION_ATTEMPTED,
                ]:
                    notes = f"Direct scan confirmation at {datetime.utcnow().isoformat()}: {reason} - {message}"
                    if active_event.notes:
                        notes = f"{active_event.notes}\n{notes}"
                    await self.anomaly_event_service.update_event(
                        active_event.anomaly_id,
                        AnomalyEventUpdate(
                            notes=notes,
                            status=AnomalyStatus.FAILURE_DETECTED,  # Upgrade to direct failure if it was just an anomaly
                        ),
                    )
                return

            # Create new event for this failure
            entity_key = f"{entity_type}/{entity_id}"
            if namespace:
                entity_key = f"{namespace}/{entity_key}"
            logger.warning(f"DIRECT FAILURE DETECTED for entity {entity_key}: {reason}")

            # Create appropriate event status
            if entity_type == "node":
                event_status = (
                    AnomalyStatus.CRITICAL_FAILURE
                )  # Node issues are critical
            elif status in ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"]:
                event_status = AnomalyStatus.POD_STATE_ISSUE
            else:
                event_status = AnomalyStatus.FAILURE_DETECTED

            # Try to fetch metrics for this entity if available
            metric_snapshot = []
            try:
                all_metrics = await prometheus_scraper.fetch_all_metrics(
                    self.active_promql_queries
                )
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
                "notes": f"Directly detected failure at {datetime.utcnow().isoformat()}: {reason} - {message}",
            }

            # Create event in database
            new_event = await self.anomaly_event_service.create_event(event_data)
            if not new_event:
                logger.error(f"Failed to create event for direct failure: {entity_key}")
                return

            # Process with Gemini if available
            remediation_actions = []
            if self.gemini_service and settings.GEMINI_AUTO_ANALYSIS:
                try:
                    # Prepare context for Gemini - safely serialize resource_data
                    failure_context = {
                        "entity_id": entity_id,
                        "entity_type": entity_type,
                        "namespace": namespace,
                        "status": status,
                        "reason": reason,
                        "message": message,
                        "resource_data": self.safe_serialize(resource_data),
                        "timestamp": datetime.utcnow().isoformat(),
                    }

                    # Get remediation suggestions
                    remediation_res = await self.gemini_service.suggest_remediation(
                        failure_context
                    )

                    # Process the response - properly handle the new response format
                    if remediation_res and isinstance(remediation_res, dict):
                        # Check if we have valid steps in the response
                        if "steps" in remediation_res and isinstance(remediation_res["steps"], list):
                            remediation_steps = remediation_res["steps"]
                            if remediation_steps:
                                # Convert steps to RemediationAction objects
                                for step in remediation_steps:
                                    if isinstance(step, str):
                                        # Create a proper RemediationAction object for each step
                                        action = RemediationAction(
                                            action_type="command",
                                            resource_type=entity_type,
                                            resource_name=entity_id,
                                            namespace=namespace,
                                            parameters={"command": step},
                                            confidence=remediation_res.get("confidence", 0.5),
                                            command=step  # Explicitly set the command attribute
                                        )
                                        remediation_actions.append(action)
                                    elif isinstance(step, dict) and "action_type" in step:
                                        # Handle dict-based steps from structured responses
                                        action_type = step.get("action_type", "command")

                                        # Prepare parameters based on the action type requirements
                                        params = step.get("parameters", {})

                                        # Add required parameters if they're missing
                                        if action_type == "delete_pod":
                                            params["name"] = step.get("resource_name", entity_id)
                                            params["namespace"] = step.get("namespace", namespace or "default")
                                        elif action_type == "restart_pod":
                                            params["pod_name"] = step.get("resource_name", entity_id)
                                            params["namespace"] = step.get("namespace", namespace or "default")
                                        elif action_type in ["restart_deployment", "scale_deployment"]:
                                            params["name"] = step.get("resource_name", entity_id)
                                            params["namespace"] = step.get("namespace", namespace or "default")
                                            if action_type == "scale_deployment" and "replicas" not in params:
                                                params["replicas"] = 3
                                        elif action_type == "adjust_resources":
                                            params["name"] = step.get("resource_name", entity_id)
                                            params["namespace"] = step.get("namespace", namespace or "default")
                                        elif action_type in ["cordon_node", "drain_node", "uncordon_node"]:
                                            params["name"] = step.get("resource_name", entity_id)
                                        elif action_type == "describe":
                                            params["name"] = step.get("resource_name", entity_id)
                                            params["namespace"] = step.get("namespace", namespace) if namespace else None
                                            params["resource_type"] = step.get("resource_type", entity_type)

                                        # Create the RemediationAction with proper parameters
                                        action = RemediationAction(
                                            action_type=action_type,
                                            resource_type=step.get("resource_type", entity_type),
                                            resource_name=step.get("resource_name", entity_id),
                                            namespace=step.get("namespace", namespace),
                                            parameters=params,
                                            confidence=step.get("confidence", remediation_res.get("confidence", 0.5)),
                                            command=""  # Will be generated from format_command_from_template
                                        )

                                        remediation_actions.append(action)

                                # Update the event with the suggestions as RemediationAction objects
                                if remediation_actions:
                                    await self.anomaly_event_service.update_event(
                                        new_event.anomaly_id,
                                        AnomalyEventUpdate(
                                            suggested_remediations=remediation_actions,
                                            status=AnomalyStatus.REMEDIATION_SUGGESTED,
                                        ),
                                    )

                        # If we have risks, add them to the notes
                        if "risks" in remediation_res and remediation_res["risks"]:
                            risks_note = "\nPotential risks: " + ", ".join(remediation_res["risks"])
                            await self.anomaly_event_service.update_event(
                                new_event.anomaly_id,
                                AnomalyEventUpdate(
                                    notes=new_event.notes + risks_note,
                                ),
                            )
                except Exception as e:
                    logger.error(f"Error in Gemini processing for direct failure: {e}", exc_info=True)

            # Always try to remediate direct failures if appropriate
            is_critical = entity_type == "node" or status in [
                "Failed",
                "CrashLoopBackOff",
            ]

            # For CrashLoopBackOff pods, add a fallback restart action with force_restart=True if none exists
            if entity_type == "pod" and (status == "CrashLoopBackOff" or (reason and "crashloop" in reason.lower())):
                if not any(action.action_type == "restart_pod" for action in remediation_actions):
                    logger.info(f"Adding fallback force restart for pod {entity_id} in CrashLoopBackOff state")
                    restart_action = RemediationAction(
                        action_type="restart_pod",
                        resource_type="pod",
                        resource_name=entity_id,
                        namespace=namespace,
                        parameters={
                            "pod_name": entity_id,
                            "namespace": namespace or "default",
                            "force_restart": True  # Enable force_restart for CrashLoopBackOff pods
                        },
                        confidence=0.7,
                        command=f"kubectl delete pod {entity_id} -n {namespace or 'default'} --grace-period=0"
                    )
                    remediation_actions.append(restart_action)

                    # Update the event with the additional remediation action
                    await self.anomaly_event_service.update_event(
                        new_event.anomaly_id,
                        AnomalyEventUpdate(
                            suggested_remediations=remediation_actions,
                            notes=new_event.notes + "\nAdded force_restart remediation for CrashLoopBackOff pod.",
                            status=AnomalyStatus.REMEDIATION_SUGGESTED,
                        ),
                    )

            await self._decide_remediation(
                new_event,
                remediation_actions,
                is_critical=is_critical,
                is_predicted=False,
                is_direct_failure=True,  # Flag to indicate this is a directly detected failure
            )
        except Exception as e:
            logger.error(
                f"Error handling direct failure for {entity_type}/{entity_id}: {e}",
                exc_info=True,
            )

    async def run(self):
        """The main continuous loop for the worker."""
        logger.info("AutonomousWorker starting run loop...")

        # Log startup summary before first cycle
        await self._log_startup_summary()

        while True:
            await self.run_cycle()
            logger.debug(
                f"Worker sleeping for {settings.WORKER_SLEEP_INTERVAL_SECONDS} seconds..."
            )
            await asyncio.sleep(settings.WORKER_SLEEP_INTERVAL_SECONDS)

    async def _scan_for_anomalies(self, metrics_snapshot_list=None):
        """
        Scans metrics for anomalies.

        Args:
            metrics_snapshot_list: List of metrics snapshots to scan for anomalies
        """
        if not metrics_snapshot_list:
            logger.warning("No metrics provided for anomaly scanning")
            return

        for metrics_snapshot in metrics_snapshot_list:
            labels = metrics_snapshot.get("metric", {})
            # determine entity type & id from labels
            if "pod" in labels:
                entity_type = "pod"
                entity_id = labels.get("pod", "unknown")
            elif "node" in labels:
                entity_type = "node"
                entity_id = labels.get("node", "unknown")
            else:
                entity_type = "metric"
                # pick first remaining label as id, or default
                entity_id = next(iter(labels.values()), "unknown")

            anomaly_context = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "namespace": labels.get("namespace"),
                "metrics_snapshot": metrics_snapshot,
            }
            score = self.anomaly_detector.predict(metrics_snapshot)
            await self._predict_and_handle_anomaly(anomaly_context, score)

    async def _discover_metric_metadata(self, metric_name, start_time=None, timeout=5):
        """
        Discovers metadata about a metric from Prometheus to better understand its structure and labels.

        Args:
            metric_name: The name of the metric to discover metadata for
            start_time: Optional start time for timeout calculation
            timeout: Maximum seconds to spend on this operation

        Returns:
            Dictionary with metric metadata including type, help text, and sample labels
        """
        metadata = {
            "type": None,
            "help": None,
            "base_labels": [],
            "sample_labels": {},
            "sample_value": None,
        }

        try:
            # First try to get metric metadata (type, help)
            metadata_query = f"count({metric_name})"
            validation = await prometheus_scraper.validate_query(metadata_query)
            if not validation["valid"]:
                logger.debug(f"Basic metadata query failed for {metric_name}")
                return metadata

            # Get a sample of the metric to understand its structure
            sample_query = f"{metric_name}{{}} limit 1"
            samples = await prometheus_scraper.fetch_metric_data_for_label_check(
                sample_query
            )
            if samples:
                sample = samples[0]
                metadata["sample_labels"] = sample.get("labels", {})
                metadata["base_labels"] = list(metadata["sample_labels"].keys())
                metadata["sample_value"] = sample.get("value")

                # Determine metric type based on naming conventions
                if (
                    metric_name.endswith("_total")
                    or metric_name.endswith("_sum")
                    or metric_name.endswith("_count")
                ):
                    metadata["type"] = "counter"
                elif metric_name.endswith("_bucket"):
                    metadata["type"] = "histogram"
                elif "_seconds_" in metric_name:
                    metadata["type"] = "timing"
                elif metric_name.endswith("_bytes") or metric_name.endswith(
                    "_bytes_total"
                ):
                    metadata["type"] = "bytes"
                elif metric_name.startswith("kube_") and "_status_" in metric_name:
                    metadata["type"] = "status"
                else:
                    metadata["type"] = "gauge"  # Default assumption

            return metadata
        except Exception as e:
            logger.warning(f"Error discovering metadata for {metric_name}: {e}")
            return metadata

    async def _build_optimal_query(
        self, metric_name, metadata, metric_type, rate_interval
    ):
        """
        Builds an optimal query for a metric based on its metadata and intended use.

        Args:
            metric_name: The name of the metric
            metadata: Metadata about the metric from _discover_metric_metadata
            metric_type: The type of metric we're building a query for (cpu_usage, memory_usage, etc.)
            rate_interval: Rate interval to use for rate() functions

        Returns:
            A tuple of (query_string, entity_type) where entity_type is pod, node, etc.
        """
        query = None
        entity_type = "unknown"
        labels = metadata.get("base_labels", [])

        # Identify the entity type based on available labels
        if "pod" in labels and "namespace" in labels:
            entity_type = "pod"
        elif "node" in labels:
            entity_type = "node"
        elif "instance" in labels:
            entity_type = "instance"
        elif "deployment" in labels:
            entity_type = "deployment"

        # Build query based on metric type and entity type
        metric_category = metadata.get("type")

        if metric_category == "counter" and entity_type == "pod":
            # Rate for counters, grouped by pod
            query = f"sum(rate({metric_name}[{rate_interval}])) by (namespace, pod)"
        elif metric_category == "counter" and entity_type == "node":
            # Rate for counters, grouped by node
            query = f"sum(rate({metric_name}[{rate_interval}])) by (node)"
        elif metric_category == "counter" and entity_type == "instance":
            # Rate for counters, grouped by instance
            query = f"sum(rate({metric_name}[{rate_interval}])) by (instance)"
        elif metric_category == "counter" and entity_type == "deployment":
            # Rate for counters, grouped by deployment
            query = (
                f"sum(rate({metric_name}[{rate_interval}])) by (namespace, deployment)"
            )
        elif metric_category == "gauge" and entity_type == "pod":
            # Sum for gauges, grouped by pod
            query = f"sum({metric_name}) by (namespace, pod)"
        elif metric_category == "gauge" and entity_type == "node":
            # Sum for gauges, grouped by node
            query = f"sum({metric_name}) by (node)"
        elif metric_category == "gauge" and entity_type == "instance":
            # Sum for gauges, grouped by instance
            query = f"sum({metric_name}) by (instance)"
        elif metric_category in ["bytes", "bytes_total"] and entity_type == "pod":
            # For memory/disk metrics
            query = f"sum({metric_name}) by (namespace, pod)"
        elif metric_category == "status" and "phase" in labels:
            # For kube_pod_status_phase
            if metric_name == "kube_pod_status_phase":
                query = (
                    f"sum by (namespace, pod) ({metric_name}{{phase='Running'}}) == 1"
                )
        elif metric_category == "status" and "condition" in labels:
            # For condition-based status metrics
            if "kube_pod_status_ready" in metric_name:
                query = (
                    f"sum by (namespace, pod) ({metric_name}{{condition='true'}}) == 1"
                )
            elif "kube_node_status_condition" in metric_name:
                query = f"sum by (node) ({metric_name}{{condition='Ready', status='true'}}) == 1"

        # If we couldn't determine a good query, return None
        return query, entity_type

    async def _test_and_rank_queries(self, candidate_queries, metric_type):
        """
        Tests multiple candidate queries and ranks them by quality and suitability.

        Args:
            candidate_queries: List of queries to test
            metric_type: Type of metric we're testing for (used for scoring)

        Returns:
            The best query string, or None if none were suitable
        """
        if not candidate_queries:
            return None

        # Set a timeout for the entire ranking process
        ranking_start = datetime.utcnow()
        ranking_timeout = 10  # seconds
        query_scores = []

        for query in candidate_queries:
            # Check timeout
            if (datetime.utcnow() - ranking_start).total_seconds() > ranking_timeout:
                logger.warning(
                    f"Query ranking timed out after {ranking_timeout} seconds."
                )
                break

            score = 0
            validation_result = await prometheus_scraper.validate_query(query)

            if not validation_result["valid"]:
                continue  # Skip invalid queries

            # Fetch sample data to evaluate query quality (just one sample)
            sample_data = await prometheus_scraper.fetch_metric_data_for_label_check(
                query, limit=1
            )
            if not sample_data:
                continue  # Skip queries that return no data

            # Base score = 10
            score = 10

            # Score based on query structure
            if " by (namespace, pod)" in query:
                score += 5  # Pod-level metrics preferred
            elif " by (node)" in query:
                score += 3  # Node-level metrics good too
            elif " by (deployment)" in query:
                score += 4  # Deployment-level metrics also good

            # Score based on aggregation function
            if "sum(" in query:
                score += 2  # Sum is generally good
            elif "avg(" in query:
                score += 1  # Average is fine
            elif "max(" in query:
                score += 1  # Max is fine for certain metrics
            elif "min(" in query:
                score += 1  # Min is fine for certain metrics

            # Score based on rate usage for counters
            if "_total" in query and "rate(" not in query and "irate(" not in query:
                score -= 5  # Counter without rate is bad
            elif "_total" in query and "rate(" in query:
                score += 2  # Rate for counters is good
            elif "_total" in query and "irate(" in query:
                score += 1  # irate is good for high resolution
            elif "_total" in query and "increase(" in query:
                score += 1  # increase is good for some use cases

            # Score based on returned data
            # Check if the sample data has the expected entity identification
            first_sample = sample_data[0]
            labels = first_sample.get("labels", {})

            # Check that we have appropriate entity labels for identification
            if "namespace" in labels and "pod" in labels:
                score += 3  # Has pod identification
            elif "node" in labels:
                score += 2  # Has node identification
            elif "deployment" in labels:
                score += 2  # Has deployment identification
            else:
                score -= 5  # No good entity identifier

            # Penalize queries that return no data points
            if not first_sample.get("value"):
                score -= 3

            # Extra scoring based on metric type
            if metric_type == "cpu_usage" and "cpu" in query.lower():
                score += 2  # Query is clearly about CPU
            elif metric_type == "memory_usage" and "memory" in query.lower():
                score += 2  # Query is clearly about memory
            elif metric_type == "pod_status" and "status" in query.lower():
                score += 2  # Status is excellent

            # Add to list of candidates with scores
            query_scores.append((query, score))
            logger.debug(f"Query '{query}' scored {score}")

        # Sort by score descending
        query_scores.sort(key=lambda x: x[1], reverse=True)

        # Return the highest scoring query if we have any
        if query_scores and query_scores[0][1] > 0:
            best_query, best_score = query_scores[0]
            logger.info(
                f"Selected best query for {metric_type}: '{best_query}' (score: {best_score})"
            )
            return best_query

        return None

    async def format_metrics_for_ai(self, entity_id, entity_type, namespace, current_metrics, historical_metrics=None):
        """
        Format metrics in a structured way for AI analysis with clear context.

        Args:
            entity_id: ID of the entity (pod, node)
            entity_type: Type of the entity
            namespace: Kubernetes namespace
            current_metrics: Current metrics snapshot with anomaly
            historical_metrics: Optional historical metrics snapshots (up to 10)

        Returns:
            Dict containing structured metrics data for AI analysis
        """
        try:
            # Basic entity information
            entity_info = {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "namespace": namespace
            }

            # Process current metrics (the anomalous ones)
            formatted_current = []
            for metric in current_metrics:
                metric_name = metric.get("metric", "unknown")
                if "labels" in metric:
                    labels = metric.get("labels", {})
                else:
                    labels = {}

                # Extract values with timestamps
                values = []
                raw_values = metric.get("values", [])
                for val in raw_values:
                    if isinstance(val, list) and len(val) == 2:
                        # Convert timestamp to readable format
                        try:
                            timestamp = datetime.fromtimestamp(val[0]).isoformat()
                            values.append({"timestamp": timestamp, "value": val[1]})
                        except:
                            # If timestamp conversion fails, use raw value
                            values.append({"timestamp": str(val[0]), "value": val[1]})

                # Create structured metric info
                formatted_current.append({
                    "metric_name": metric_name,
                    "query": metric.get("query", ""),
                    "labels": labels,
                    "values": values
                })

            # Format historical metrics if available (up to 10 snapshots)
            historical_data = []
            if historical_metrics:
                # Limit to last 10 historical snapshots
                historical_subset = historical_metrics[-10:] if len(historical_metrics) > 10 else historical_metrics

                for idx, snapshot in enumerate(historical_subset):
                    snapshot_metrics = []
                    for metric in snapshot:
                        metric_name = metric.get("metric", "unknown")
                        # Extract values with timestamps
                        values = []
                        raw_values = metric.get("values", [])
                        for val in raw_values:
                            if isinstance(val, list) and len(val) == 2:
                                try:
                                    timestamp = datetime.fromtimestamp(val[0]).isoformat()
                                    values.append({"timestamp": timestamp, "value": val[1]})
                                except:
                                    values.append({"timestamp": str(val[0]), "value": val[1]})

                        snapshot_metrics.append({
                            "metric_name": metric_name,
                            "query": metric.get("query", ""),
                            "values": values
                        })

                    # Calculate the time offset from current metrics
                    time_offset_seconds = idx * -60  # Assume snapshots are 1 minute apart

                    historical_data.append({
                        "time_offset_seconds": time_offset_seconds,
                        "metrics": snapshot_metrics
                    })

            # Add system resource metadata if available
            metadata = {}
            if entity_type == "pod":
                try:
                    # Try to get pod metadata if possible
                    # Fix: Swap parameter order to match function signature (namespace first, then pod_name)
                    pod_data = await prometheus_scraper.get_pod_metadata(namespace, entity_id)
                    if pod_data:
                        metadata = pod_data
                except Exception as e:
                    logger.warning(f"Couldn't fetch pod metadata: {e}")

            # Bundle everything together
            return {
                "entity_info": entity_info,
                "current_metrics": formatted_current,
                "historical_metrics": historical_data,
                "metadata": metadata,
                "anomaly_score": self.latest_anomaly_scores.get(entity_id, 0) if hasattr(self, 'latest_anomaly_scores') else 0,
                "prediction_data": self.latest_predictions.get(entity_id, {}) if hasattr(self, 'latest_predictions') else {}
            }

        except Exception as e:
            logger.error(f"Error formatting metrics for AI analysis: {e}")
            # Return at least basic info if something fails
            return {
                "entity_info": {
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "namespace": namespace
                },
                "current_metrics": current_metrics,
                "error": str(e)
            }

    async def _generate_cluster_overview(self, all_metrics):
        """Generate and log a summary of the current cluster state based on metrics."""
        return

    async def _log_startup_summary(self):
        """
        Generate and log a summary of the application and its configuration at startup.
        """
        try:
            logger.info("=" * 80)
            logger.info("KUBEWISE APPLICATION STARTUP SUMMARY")
            logger.info("=" * 80)

            # Application info
            import os
            from datetime import datetime
            import platform

            logger.info(f"Application started at: {datetime.utcnow().isoformat()}")
            logger.info(f"Operating system: {platform.system()} {platform.release()}")
            logger.info(f"Python version: {platform.python_version()}")

            # Configuration info
            logger.info(f"Current mode: {mode_service.get_mode()}")
            logger.info(f"Prometheus URL: {settings.PROMETHEUS_URL}")
            logger.info(f"Worker interval: {settings.WORKER_SLEEP_INTERVAL_SECONDS} seconds")
            logger.info(f"Metric window: {settings.ANOMALY_METRIC_WINDOW_SECONDS} seconds")

            # ML model info
            model_loaded = hasattr(anomaly_detector, 'model') and anomaly_detector.model is not None
            scaler_loaded = hasattr(anomaly_detector, 'scaler') and anomaly_detector.scaler is not None

            logger.info(f"ML model loaded: {model_loaded}")
            logger.info(f"ML scaler loaded: {scaler_loaded}")
            logger.info(f"Anomaly detection threshold: {settings.AUTOENCODER_THRESHOLD}")

            # Kubernetes integration
            k8s_available = False
            try:
                from kubernetes import client
                v1 = client.CoreV1Api()
                k8s_available = True
            except Exception:
                pass

            logger.info(f"Kubernetes API access: {k8s_available}")

            # AI integration
            gemini_available = self.gemini_service is not None and not self.gemini_service.is_api_key_missing()
            logger.info(f"AI assistance available: {gemini_available}")

            logger.info("=" * 80)

        except Exception as e:
            logger.error(f"Error generating startup summary: {e}")

    def _extract_metric_value(self, metric):
        """
        Helper method to extract numeric values from a metric in a consistent way.
        Handles both scalar and vector formats from Prometheus.

        Args:
            metric: The metric dictionary from which to extract the value

        Returns:
            The numeric value as float, or None if no valid value could be extracted
        """
        try:
            # First try to get value from the 'value' field
            if "value" in metric and metric["value"] is not None:
                if isinstance(metric["value"], (list, tuple)) and len(metric["value"]) > 1:
                    # Handle Prometheus vector format [timestamp, value]
                    return float(metric["value"][1])
                elif isinstance(metric["value"], (int, float)):
                    return float(metric["value"])
                elif isinstance(metric["value"], str):
                    # Try to convert string to float if possible
                    try:
                        return float(metric["value"])
                    except ValueError:
                        pass

            # If no value, try the 'values' array
            if "values" in metric and metric["values"]:
                if isinstance(metric["values"], list) and metric["values"]:
                    # Get most recent value (last in the array)
                    last_value = metric["values"][-1] if len(metric["values"]) > 0 else None
                    if isinstance(last_value, (list, tuple)) and len(last_value) > 1:
                        # Attempt to process as [timestamp, value] format
                        try:
                            return float(last_value[1])
                        except (ValueError, TypeError):
                            pass
                    elif isinstance(last_value, (int, float)):
                        return float(last_value)
                    elif isinstance(last_value, str):
                        try:
                            return float(last_value)
                        except ValueError:
                            pass

                    # If last value didn't work, try iterating through values to find a usable one
                    for val in reversed(metric["values"]):
                        if isinstance(val, (list, tuple)) and len(val) > 1:
                            try:
                                return float(val[1])
                            except (ValueError, TypeError):
                                continue
                        elif isinstance(val, (int, float)):
                            return float(val)
                        elif isinstance(val, str):
                            try:
                                return float(val)
                            except ValueError:
                                continue

            # If all else fails, try to extract raw_value
            if "raw_value" in metric and metric["raw_value"] is not None:
                try:
                    return float(metric["raw_value"])
                except (ValueError, TypeError):
                    pass

            # Additional check for sample_value
            if "sample_value" in metric and metric["sample_value"] is not None:
                try:
                    return float(metric["sample_value"])
                except (ValueError, TypeError):
                    pass

            logger.debug(f"Could not extract value from metric: {metric.get('metric', 'unknown')}")
        except (ValueError, TypeError, IndexError) as e:
            logger.debug(f"Error extracting metric value: {e} - metric: {metric}")

        return None

    async def _predict_and_handle_anomaly(self, entity_type, entity_name, entity_namespace, metrics):
        """
        Predict and handle potential anomalies for an entity.

        Args:
            entity_type: The type of entity (pod, node)
            entity_name: The name of the entity
            entity_namespace: The namespace of the entity (or None for nodes)
            metrics: The metrics data for the entity
        """
        try:
            # Get entity ID for logging
            entity_id = f"{entity_namespace}/{entity_type}/{entity_name}"

            # Get historical metrics if available
            historical_snapshots = await self._get_historical_metrics(entity_type, entity_name, entity_namespace)

            # Use the enhanced prediction method that includes forecast
            result = await self.anomaly_detector.predict_with_forecast(metrics, historical_snapshots)

            # Determine anomaly type for clearer reporting
            anomaly_type = self._determine_anomaly_type(metrics)

            is_anomaly = result.get("is_anomaly", False)
            anomaly_score = result.get("anomaly_score", 0.0)
            is_critical = result.get("is_critical", False)

            if is_anomaly:
                # Build a more descriptive message
                severity = "Critical" if is_critical else "Anomaly"
                self.logger.warning(
                    f"{severity} detected for {entity_id} with score: {anomaly_score:.4f} ({anomaly_type}). Recommendation: {result.get('recommendation', 'investigate')}"
                )

                # Store the anomaly event
                await self._store_anomaly_event(entity_type, entity_name, entity_namespace, result)

                # Handle the anomaly
                if is_critical and settings.AUTO_REMEDIATE_CRITICAL:
                    await self._handle_critical_anomaly(entity_type, entity_name, entity_namespace, result)
                elif not is_critical and settings.AUTO_REMEDIATE_PREDICTED:
                    await self._handle_predicted_anomaly(entity_type, entity_name, entity_namespace, result)

            # Return the result for further processing
            return result

        except Exception as e:
            self.logger.error(f"Error predicting anomalies for {entity_namespace}/{entity_type}/{entity_name}: {e}")
            return {
                "is_anomaly": False,
                "anomaly_score": 0.0,
                "is_critical": False,
                "recommendation": "error",
            }

    def _determine_anomaly_type(self, metrics):
        """
        Determine the type of anomaly based on the metrics.

        Args:
            metrics: The metrics data for the entity

        Returns:
            str: A short description of the anomaly type
        """
        # Default anomaly type
        anomaly_type = "unknown"

        try:
            # Extract relevant metrics
            cpu_values = []
            memory_values = []
            network_receive_values = []
            network_transmit_values = []
            restart_values = []

            for metric in metrics:
                metric_type = metric.get("metric_type", "")
                if not metric_type:
                    continue

                # Extract values - handle both direct values and array values
                value = metric.get("value", 0)
                if isinstance(value, list) and len(value) >= 2:
                    try:
                        value = float(value[1])
                    except (ValueError, TypeError):
                        value = 0

                # Categorize metrics
                if "cpu_usage" in metric_type:
                    cpu_values.append(float(value) if isinstance(value, (int, float, str)) else 0)
                elif "memory" in metric_type:
                    memory_values.append(float(value) if isinstance(value, (int, float, str)) else 0)
                elif "network_receive" in metric_type:
                    network_receive_values.append(float(value) if isinstance(value, (int, float, str)) else 0)
                elif "network_transmit" in metric_type:
                    network_transmit_values.append(float(value) if isinstance(value, (int, float, str)) else 0)
                elif "restart" in metric_type:
                    restart_values.append(float(value) if isinstance(value, (int, float, str)) else 0)

            # Determine anomaly type based on metric values
            if restart_values and max(restart_values) > 0:
                anomaly_type = "high_restarts"
            elif cpu_values and max(cpu_values) > 0.8:  # 80% CPU usage
                anomaly_type = "high_cpu"
            elif memory_values and max(memory_values) > 0.8 * 1024 * 1024 * 1024:  # 80% of 1GB
                anomaly_type = "high_memory"
            elif network_receive_values and max(network_receive_values) > 50 * 1024 * 1024:  # 50MB/s
                anomaly_type = "high_network_in"
            elif network_transmit_values and max(network_transmit_values) > 50 * 1024 * 1024:  # 50MB/s
                anomaly_type = "high_network_out"
            else:
                # If we can't identify a specific cause but know metrics format is off
                anomaly_type = "data_quality"

            return anomaly_type

        except Exception as e:
            self.logger.debug(f"Error determining anomaly type: {e}")
            return "unknown"
