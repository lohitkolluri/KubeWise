import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import uuid

from loguru import logger

from app.core.config import settings
from app.models.anomaly_detector import anomaly_detector
from app.models.anomaly_event import (
    AnomalyEvent,
    AnomalyEventUpdate,
    AnomalyStatus,
    RemediationAttempt,
    AnomalyEventCreate, # Imported but not explicitly used, keep for potential future use
)
from app.services.anomaly_event_service import AnomalyEventService
from app.services.gemini_service import GeminiService, RemediationAction
from app.services.mode_service import mode_service
from app.utils.k8s_executor import k8s_executor, list_resources # Imported but not explicitly used, keep for potential future use
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
        # self.safe_serialize = self._deep_serialize # Keep this logic within the method where it's needed
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
            # Use str(k) to handle non-string keys, if any
            return {str(k): self._deep_serialize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._deep_serialize(item) for item in obj]
        elif isinstance(obj, (datetime, timedelta)):
            return obj.isoformat() # Use ISO format for better compatibility
        elif isinstance(obj, (int, float, bool, str, type(None))):
            return obj
        elif hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
            # Handle Kubernetes API objects that have to_dict method
            # Ensure the result of to_dict() is also serialized
            return self._deep_serialize(obj.to_dict())
        elif hasattr(obj, "__dict__"):
            # Handle general objects with __dict__ - ensure it's serializable
            return self._deep_serialize(obj.__dict__)
        else:
            # For any other type, convert to string as a fallback
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
            from app.core.queries import STANDARD_QUERIES, create_placeholder_query, FAILURE_DETECTION_QUERIES

            # Get required feature metrics, default to empty list if not available
            feature_metrics = getattr(settings, "FEATURE_METRICS", [])
            if not feature_metrics:
                logger.error("No FEATURE_METRICS defined in settings. Using default set.")
                # Use a standard set of metrics if none are defined
                feature_metrics = [
                    "cpu_usage_seconds_total",
                    "memory_working_set_bytes",
                    "network_receive_bytes_total",
                    "network_transmit_bytes_total",
                    "pod_status_phase",
                    "container_restarts_rate",
                    "node_cpu_usage",
                    "node_memory_usage",
                    "deployment_replicas_available",
                    "deployment_replicas_unavailable",
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
            oom_query = FAILURE_DETECTION_QUERIES.get("oom_killed_containers")
            if oom_query:
                final_queries.append(oom_query)
                covered_metrics.add("oom_killed_containers")
            else:
                logger.warning("OOM Killed containers query not found in FAILURE_DETECTION_QUERIES")


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

    def _extract_base_metric_from_query(self, query: str) -> Optional[str]:
        """Extracts the base metric name from a PromQL query."""
        # Simple extraction logic - find the first word before '{' or '(' or a space
        import re
        match = re.search(r"(\w+)(?:\s*\{|\s*\()", query)
        if match:
            return match.group(1)
        # Fallback: find the first sequence of word characters
        match = re.search(r"\w+", query)
        if match:
            return match.group(0)
        return None

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
                logger.debug(f"Query '{filtered_candidates[i]}' failed test or returned no data.")
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
                logger.debug(f"Invalid query: {query}. Reason: {validation_result.get('error', 'Unknown')}")
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
                logger.debug(f"Invalid query: {query}. Reason: {validation_result.get('error', 'Unknown')}")
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
        query_lower = query.lower() # Define once for reuse

        # Define the required feature metrics for our model
        required_metrics = [
            "cpu_usage_seconds_total",  # Rate
            "memory_working_set_bytes",  # Absolute value
            "network_receive_bytes_total",  # Rate
            "network_transmit_bytes_total",  # Rate
            "pod_status_phase",  # Categorical/Binary (Running=1, else=0)
            "container_restarts_rate",  # Counter / Rate of change
        ]

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
                logger.debug( # Changed to debug
                    f"Query '{query[:50]}...' contains required metric '{required_metric}', +{weights['metric_match']} points"
                )

        # 2. Score based on query structure (entity level granularity)
        if " by (namespace, pod)" in query_lower:
            score += weights["pod_level"]  # Pod-level metrics preferred
        elif " by (namespace, pod, container)" in query_lower:
            score += weights["pod_level"] + 1  # Even better - container level
        elif " by (node)" in query_lower:
            score += weights["node_level"]  # Node-level metrics good too
        elif " by (namespace, deployment)" in query_lower:
            score += weights["deployment_level"]  # Deployment-level metrics also good

        # 3. Score based on aggregation function
        if "sum(" in query_lower:
            score += weights["aggregation"]  # Sum is generally good
        elif "avg(" in query_lower:
            score += weights["aggregation"] - 1  # Average is fine
        elif "max(" in query_lower:
            score += weights["aggregation"] - 1  # Max is fine for certain metrics
        elif "min(" in query_lower:
            score += weights["aggregation"] - 2  # Min is less commonly useful

        # 4. Score based on rate usage for counters
        if "_total" in query_lower and "rate(" not in query_lower and "irate(" not in query_lower and "increase(" not in query_lower:
            score -= weights["rate_usage"]  # Counter without rate is bad
        elif "_total" in query_lower and "rate(" in query_lower:
            score += weights["rate_usage"]  # Rate for counters is good
        elif "_total" in query_lower and "irate(" in query_lower:
            score += weights["rate_usage"] - 1  # irate is good for high resolution
        elif "_total" in query_lower and "increase(" in query_lower:
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
            if "node" in labels:
                has_node = True
            if "container" in labels:
                has_container = True

        if has_namespace_pod:
            score += weights["label_quality"]
        elif has_node:
            score += weights["label_quality"] // 2
        if has_container:
            score += weights["label_quality"] // 2

        # 7. Additional points for complete entity identification
        if has_namespace_pod and has_container:
            score += weights["label_quality"] // 2 # Bonus for container level detail

        # 8. Metric type specific scoring
        if metric_type == "cpu_usage" and "cpu" in query_lower:
            score += 3
        elif metric_type == "memory_usage" and "memory" in query_lower:
            score += 3
        elif metric_type == "pod_status" and "status" in query_lower:
            score += 3

        # 9. Check for value presence and type
        for result in results:
            # Prometheus returns value as a list [timestamp, value_string]
            if "value" not in result or not isinstance(result["value"], list) or len(result["value"]) != 2 or result["value"][1] is None:
                score -= weights["value_presence"]  # Missing values are bad
                break # One bad value is enough penalty

        # 10. Bonus for namespace filtering that improves query efficiency
        if "{namespace" in query_lower or "{namespace=" in query_lower:
            score += weights["namespace_filtering"]  # Efficient namespace filtering

        # 11. Penalize overly complex queries that might be slow
        if query.count("(") > 4:
            score -= 3  # Too many nested functions
        if query.count("{") > 3:
            score -= 2  # Too many label matchers

        # 12. Bonus for using time() function (though less common, can be useful)
        if "time()" in query_lower:
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
        # Require at least 2 data points (e.g., current + 1 historical) for prediction
        if len(self.entity_metric_history.get(entity_key, [])) >= 2:
            await self._predict_and_handle_anomaly(
                entity_key, entity_data, self.entity_metric_history[entity_key]
            )
        else:
            logger.debug(f"Not enough history for {entity_key} to predict anomaly (needs >= 2, has {len(self.entity_metric_history.get(entity_key, []))})")


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

            # Get the current metrics snapshot (latest in the window)
            current_metrics_snapshot = metric_window[-1] if metric_window else []

            # The history excludes the current snapshot
            historical_snapshots = metric_window[:-1] if len(metric_window) > 1 else []

            if not current_metrics_snapshot:
                logger.warning(f"No current metrics snapshot available for {entity_key} in window")
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
            # forecast_available = prediction_result.get("forecast_available", False) # Not used currently

            # Store the prediction result for potential later use (like AI formatting)
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
                event_status = AnomalyStatus.DETECTED # Default
                log_message_prefix = "Anomaly"
                log_level = logger.warning

                if is_primarily_prediction:
                    event_status = AnomalyStatus.PREDICTED_FAILURE
                    log_message_prefix = "Predicted failure"
                elif is_critical:
                    event_status = AnomalyStatus.CRITICAL_FAILURE
                    log_message_prefix = "CRITICAL Anomaly"
                    log_level = logger.error
                else: # Normal anomaly
                    event_status = AnomalyStatus.DETECTED

                log_message = f"{log_message_prefix} detected for {entity_key} with score: {anomaly_score:.4f}. Recommendation: {recommendation}"
                log_level(log_message)

                # Check if an active event already exists for this entity
                active_event = await self.anomaly_event_service.find_active_event(
                    entity_id=entity_id, entity_type=entity_type, namespace=namespace
                )

                anomaly_id = None
                event = None

                if active_event:
                    # Update existing event
                    anomaly_id = active_event.anomaly_id
                    logger.info(f"Updating existing active event {anomaly_id} for {entity_key}")
                    update_data = AnomalyEventUpdate(
                        # Only escalate status to CRITICAL, don't downgrade
                        status=event_status if event_status == AnomalyStatus.CRITICAL_FAILURE else active_event.status,
                        anomaly_score=anomaly_score,
                        metric_snapshot=self._deep_serialize(current_metrics_snapshot), # Serialize metrics
                        prediction_data=self._deep_serialize(prediction_result), # Serialize prediction data
                        notes=f"{active_event.notes}\nUpdate @ {datetime.utcnow().isoformat()}: Score={anomaly_score:.4f}, Recommendation={recommendation}, Status={event_status.value}"
                    )
                    await self.anomaly_event_service.update_event(anomaly_id, update_data)
                    # Refresh event data after update
                    event = await self.anomaly_event_service.get_event(anomaly_id)
                else:
                    # Create a new anomaly event
                    event_data = {
                        "entity_id": entity_id,
                        "entity_type": entity_type,
                        "namespace": namespace,
                        "anomaly_score": anomaly_score,
                        "status": event_status,
                        "metric_snapshot": self._deep_serialize(current_metrics_snapshot), # Serialize metrics
                        "prediction_data": self._deep_serialize(prediction_result), # Serialize prediction data
                        "is_proactive": is_primarily_prediction,
                        "notes": log_message,
                        "anomaly_id": str(uuid.uuid4()) # Generate ID here
                    }

                    try:
                        created_event = await self.anomaly_event_service.create_event(event_data)
                        if created_event:
                            anomaly_id = created_event.anomaly_id
                            event = created_event
                        else:
                             logger.error(f"Failed to create anomaly event for {entity_key}, service returned None")
                    except Exception as create_err:
                        logger.error(f"Exception during anomaly event creation for {entity_key}: {create_err}", exc_info=True)


                if anomaly_id and event: # Ensure event exists
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
                                "prediction_data": self._deep_serialize(prediction_result)
                            }

                            analysis_result = await self.gemini_service.analyze_anomaly(
                                anomaly_data=analysis_context,
                                entity_info={"entity_id": entity_id, "entity_type": entity_type, "namespace": namespace},
                                related_metrics=ai_metrics_context.get("metrics", {}),
                                additional_context={"metric_window": self._deep_serialize(metric_window)} # Serialize history
                            )

                            # Log AI analysis results
                            if analysis_result and "error" not in analysis_result:
                                logger.info(f"AI analysis completed for anomaly {anomaly_id}")

                                # Ensure AI analysis is serializable
                                serializable_ai_analysis = self._deep_serialize(analysis_result)

                                # Update the event with AI analysis
                                ai_notes = f"AI Analysis: {analysis_result.get('root_cause', 'Analysis completed')}"
                                await self.anomaly_event_service.update_event(
                                    anomaly_id,
                                    AnomalyEventUpdate(
                                        ai_analysis=serializable_ai_analysis,
                                        notes=f"{event.notes}\n{ai_notes}"
                                    )
                                )
                                # Refresh event after update
                                event = await self.anomaly_event_service.get_event(anomaly_id)

                                # Get remediation suggestions if in autonomous or assisted mode
                                if mode_service.get_mode() != "learning":
                                    ai_suggestions = await self.gemini_service.suggest_remediation(
                                        anomaly_context=analysis_result # Use the original analysis result
                                    )

                                    # Safely check for errors and handle None response
                                    if ai_suggestions and isinstance(ai_suggestions, dict) and not ai_suggestions.get("error"):
                                        # Convert to remediation action objects
                                        remediation_actions = []
                                        for step in ai_suggestions.get("steps", []):
                                            # Handle different suggestion formats (string or dict)
                                            if isinstance(step, str):
                                                action = self._create_safe_remediation_action(
                                                    action_type="command", # Assume it's a command string
                                                    resource_type=entity_type,
                                                    resource_name=entity_id,
                                                    namespace=namespace,
                                                    parameters={"command": step},
                                                    confidence=ai_suggestions.get("confidence", 0.6) # Use overall confidence
                                                )
                                                remediation_actions.append(action)
                                            elif isinstance(step, dict):
                                                action = self._create_safe_remediation_action(
                                                    action_type=step.get("action_type", "command"),
                                                    resource_type=step.get("resource_type", entity_type),
                                                    resource_name=step.get("resource_name", entity_id),
                                                    namespace=step.get("namespace", namespace),
                                                    parameters=step.get("parameters", {}),
                                                    confidence=step.get("confidence", ai_suggestions.get("confidence", 0.6))
                                                )
                                                remediation_actions.append(action)
                                            else:
                                                logger.warning(f"Skipping unknown remediation suggestion format: {step}")


                                        # Decide remediation based on mode and criticality
                                        await self._decide_remediation(
                                            event=event, # Use the refreshed event
                                            ai_suggestions=remediation_actions,
                                            is_critical=is_critical,
                                            is_predicted=is_primarily_prediction
                                        )
                                    else:
                                        # Safely handle error messages
                                        error_msg = "Unknown error suggesting remediation"
                                        if isinstance(ai_suggestions, dict):
                                             error_msg = ai_suggestions.get("error", ai_suggestions.get("details", error_msg))
                                        elif ai_suggestions is None:
                                            error_msg = "No response from AI service for remediation suggestions"
                                        logger.warning(f"AI remediation suggestion failed for anomaly {anomaly_id}: {error_msg}")
                            else:
                                error_msg = "Unknown error"
                                if isinstance(analysis_result, dict):
                                    error_msg = analysis_result.get("error", error_msg)
                                logger.warning(f"AI analysis failed for anomaly {anomaly_id}: {error_msg}")
                        except Exception as ai_error:
                            logger.error(f"Error during AI processing for anomaly {anomaly_id}: {ai_error}", exc_info=True)
                else:
                    logger.error(f"Failed to create/update anomaly event for {entity_key}")
            else:
                # Check if there was a previously active event that is now resolved
                active_event = await self.anomaly_event_service.find_active_event(
                    entity_id=entity_id, entity_type=entity_type, namespace=namespace
                )

                if active_event and active_event.status not in [AnomalyStatus.RESOLVED, AnomalyStatus.CLOSED]:
                    logger.info(f"Entity {entity_key} appears to have recovered. Marking event {active_event.anomaly_id} as resolved.")
                    resolution_notes = f"{active_event.notes}\nResolved @ {datetime.utcnow().isoformat()}: Entity returned to normal state with score {anomaly_score:.4f}"
                    await self.anomaly_event_service.update_event(
                        active_event.anomaly_id,
                        AnomalyEventUpdate(
                            status=AnomalyStatus.RESOLVED,
                            resolution_time=datetime.utcnow(),
                            notes=resolution_notes
                        )
                    )
                else:
                    logger.debug(f"No anomaly detected for {entity_key} (score: {anomaly_score:.4f})")

        except Exception as e:
            logger.error(f"Error in anomaly prediction/handling for {entity_key}: {e}", exc_info=True)


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
        if not event:
            logger.error("Cannot decide remediation: AnomalyEvent object is None")
            return

        current_mode = mode_service.get_mode()
        logger.debug(f"Deciding remediation for event {event.anomaly_id} in mode {current_mode}")

        valid_suggestions = []

        # Process suggestions ensuring they are valid RemediationAction objects
        for suggestion in ai_suggestions:
            if isinstance(suggestion, str):
                # Convert string command to RemediationAction
                logger.debug(f"Converting string suggestion '{suggestion[:50]}...' to RemediationAction")
                suggestion = self._create_safe_remediation_action(
                    action_type="command",
                    resource_type=event.entity_type,
                    resource_name=event.entity_id,
                    namespace=event.namespace,
                    parameters={"command": suggestion},
                    confidence=0.5  # Default confidence for raw command string
                )
            elif not isinstance(suggestion, RemediationAction):
                logger.warning(f"Skipping invalid suggestion type: {type(suggestion)}")
                continue

            # Ensure essential fields are present
            if not all([suggestion.action_type, suggestion.resource_type, suggestion.resource_name]):
                 logger.warning(f"Skipping suggestion with missing essential fields: {suggestion}")
                 continue

            # Re-generate command if missing, ensuring parameters are valid
            try:
                if not getattr(suggestion, 'command', None):
                    suggestion.command = k8s_executor.format_command_from_template(
                        suggestion.action_type,
                        suggestion.parameters or {} # Ensure parameters is a dict
                    )
            except Exception as e:
                logger.warning(f"Failed to generate command for {suggestion.action_type} ({suggestion.resource_name}): {e}. Using fallback.")
                # Fallback command generation
                ns_part = f"-n {suggestion.namespace}" if suggestion.namespace else ""
                suggestion.command = f"kubectl get {suggestion.resource_type} {suggestion.resource_name} {ns_part}"

            # Ensure action_type is supported by our executor templates
            if suggestion.action_type not in k8s_executor.command_templates:
                logger.warning(f"Skipping unsupported action type: {suggestion.action_type}")
                continue

            valid_suggestions.append(suggestion)


        # Generate fallback suggestions if none were provided or valid
        if not valid_suggestions:
            logger.info(f"No valid AI suggestions for {event.anomaly_id}, generating fallback suggestions.")
            fallback_suggestions = []
            try:
                event_notes = str(event.notes or "").lower()
                status_str = str(event.status.value if event.status else "").lower() # Use status value

                # Pod failures
                if event.entity_type == "pod":
                    if any(term in event_notes for term in ["crashloop", "restart", "oomkilled", "crash", "failed"]):
                        fallback_suggestions.append(self._create_safe_remediation_action(
                            action_type="restart_pod", resource_type="pod", resource_name=event.entity_id, namespace=event.namespace,
                            parameters={"pod_name": event.entity_id, "namespace": event.namespace or "default"}, confidence=0.7
                        ))
                    if any(term in event_notes for term in ["oomkilled", "out of memory"]):
                        fallback_suggestions.append(self._create_safe_remediation_action(
                            action_type="describe", resource_type="pod", resource_name=event.entity_id, namespace=event.namespace,
                            parameters={}, confidence=0.6
                        ))
                    if any(term in event_notes for term in ["imagepull", "errimagepull"]):
                         fallback_suggestions.append(self._create_safe_remediation_action(
                            action_type="describe", resource_type="pod", resource_name=event.entity_id, namespace=event.namespace,
                            parameters={}, confidence=0.6
                        ))

                # Deployment failures
                elif event.entity_type == "deployment":
                    if any(term in event_notes for term in ["unavailable", "degraded", "progress"]):
                        # Suggest scaling up as a potential fix (adjust replicas as needed)
                        current_replicas = 1 # Default guess
                        try:
                            # Attempt to get current replicas for smarter suggestion
                            dep_status = await k8s_executor.get_resource_status("deployment", event.entity_id, event.namespace or "default")
                            current_replicas = dep_status.get('spec', {}).get('replicas', 1)
                        except Exception: pass # Ignore errors getting status
                        suggested_replicas = current_replicas + 1

                        fallback_suggestions.append(self._create_safe_remediation_action(
                            action_type="scale_deployment", resource_type="deployment", resource_name=event.entity_id, namespace=event.namespace,
                            parameters={"replicas": str(suggested_replicas), "name": event.entity_id, "namespace": event.namespace or "default"}, confidence=0.6
                        ))

                # Node failures
                elif event.entity_type == "node" and is_critical:
                    fallback_suggestions.append(self._create_safe_remediation_action(
                        action_type="cordon_node", resource_type="node", resource_name=event.entity_id, namespace=None,
                        parameters={"name": event.entity_id}, confidence=0.7
                    ))

                # Generic fallback: describe the resource
                if not fallback_suggestions:
                    fallback_suggestions.append(self._create_safe_remediation_action(
                        action_type="describe", resource_type=event.entity_type, resource_name=event.entity_id, namespace=event.namespace,
                        parameters={}, confidence=0.5
                    ))

                valid_suggestions.extend(fallback_suggestions)
                logger.info(f"Added {len(fallback_suggestions)} fallback suggestions for {event.anomaly_id}")

            except Exception as e:
                logger.error(f"Error generating fallback remediation suggestions: {e}", exc_info=True)


        if not valid_suggestions:
            logger.warning(f"No valid remediation suggestions (AI or fallback) found for {event.anomaly_id}. No action taken.")
            # Update event status to indicate failed suggestion
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(
                    status=AnomalyStatus.REMEDIATION_FAILED, # Or a new status like SUGGESTION_FAILED
                    notes=f"{event.notes}\nFailed to generate any valid remediation suggestions."
                )
            )
            return

        # Sort by priority: higher confidence first
        valid_suggestions.sort(key=lambda x: x.confidence or 0.0, reverse=True)

        logger.info(f"Top remediation suggestions for {event.anomaly_id} (sorted by confidence):")
        for i, sugg in enumerate(valid_suggestions[:3]):  # Log top 3
            logger.info(f"  Suggestion {i+1}: [{sugg.confidence:.2f}] {sugg.action_type} - {sugg.command}")

        # Check for auto-remediation applicability
        # Use explicit string comparison for mode
        can_auto_remediate = (
            current_mode == "autonomous" # Check for exact string match
            and (is_critical or is_direct_failure or event.status in [AnomalyStatus.CRITICAL_FAILURE, AnomalyStatus.FAILURE_DETECTED])
        )

        # Learning Mode: Only update event with suggestions
        if current_mode == "learning":
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(
                    suggested_remediation_commands=[s.model_dump() for s in valid_suggestions], # Store as dicts
                    status=AnomalyStatus.REMEDIATION_SUGGESTED,
                    notes=f"{event.notes}\nRemediation suggested (learning mode)."
                )
            )
            logger.info(f"Added {len(valid_suggestions)} remediation suggestions to event {event.anomaly_id} (learning mode)")
            return

        # Assisted Mode: Suggest but don't apply automatically
        if current_mode == "assisted":
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(
                    suggested_remediation_commands=[s.model_dump() for s in valid_suggestions], # Store as dicts
                    status=AnomalyStatus.REMEDIATION_SUGGESTED,
                    notes=f"{event.notes}\nRemediation suggested (assisted mode)."
                )
            )
            logger.info(f"Added {len(valid_suggestions)} remediation suggestions to event {event.anomaly_id} (assisted mode)")
            return

        # Autonomous Mode: Proceed with auto-remediation if applicable
        if can_auto_remediate:
            # Get the highest confidence suggestion
            best_suggestion = valid_suggestions[0]
            logger.info(f"Attempting auto-remediation for {event.anomaly_id} with action: {best_suggestion.action_type} (Confidence: {best_suggestion.confidence:.2f})")

            # Check for destructive actions and dependencies
            if best_suggestion.action_type in ["delete_deployment", "delete_pod", "drain_node", "restart_pod", "restart_deployment"]:
                logger.warning(f"Action '{best_suggestion.action_type}' is potentially destructive. Checking dependencies...")
                try:
                    deps = await k8s_executor.check_dependencies(
                        resource_type=best_suggestion.resource_type,
                        name=best_suggestion.resource_name,
                        namespace=best_suggestion.namespace # Pass namespace correctly
                    )

                    if deps:
                        logger.warning(f"Cannot auto-remediate '{best_suggestion.action_type}' for {best_suggestion.resource_name}: Found dependencies: {deps}")
                        # Downgrade to suggestion only
                        await self.anomaly_event_service.update_event(
                            event.anomaly_id,
                            AnomalyEventUpdate(
                                suggested_remediation_commands=[s.model_dump() for s in valid_suggestions],
                                notes=f"{event.notes}\nAuto-remediation ({best_suggestion.action_type}) blocked due to dependencies: {deps}",
                                status=AnomalyStatus.REMEDIATION_SUGGESTED,
                            )
                        )
                        return
                    else:
                         logger.info(f"No blocking dependencies found for '{best_suggestion.action_type}' on {best_suggestion.resource_name}.")

                except Exception as dep_check_err:
                    logger.error(f"Error checking dependencies for {best_suggestion.resource_name}: {dep_check_err}. Blocking remediation as a precaution.")
                    await self.anomaly_event_service.update_event(
                            event.anomaly_id,
                            AnomalyEventUpdate(
                                suggested_remediation_commands=[s.model_dump() for s in valid_suggestions],
                                notes=f"{event.notes}\nAuto-remediation ({best_suggestion.action_type}) blocked due to error during dependency check: {dep_check_err}",
                                status=AnomalyStatus.REMEDIATION_SUGGESTED,
                            )
                        )
                    return

            # Execute the best suggestion
            try:
                # Ensure the command is available and valid
                command_to_execute = getattr(best_suggestion, 'command', None)
                if not command_to_execute:
                    logger.error(f"Cannot execute remediation: Command is missing for action {best_suggestion.action_type}")
                    raise ValueError("Command missing for remediation action")

                logger.info(f"Executing auto-remediation command: {command_to_execute}")
                result = await k8s_executor.execute_remediation_action(best_suggestion) # Pass the action object

                # Create a remediation attempt record
                attempt = RemediationAttempt(
                    command=command_to_execute,
                    action_type=best_suggestion.action_type,
                    parameters=self._deep_serialize(best_suggestion.parameters or {}), # Ensure params are serializable
                    executor="autonomous",
                    timestamp=datetime.utcnow(),
                    success=result.get("success", False),
                    output=result.get("output", ""), # Renamed from 'result' for clarity
                    error=result.get("error", ""),
                    is_proactive=is_predicted,
                )

                # Update the event with the attempt
                new_status = AnomalyStatus.REMEDIATION_APPLIED if attempt.success else AnomalyStatus.REMEDIATION_FAILED
                await self.anomaly_event_service.update_remediation_status(
                    event.anomaly_id,
                    attempt=attempt,
                    suggested_remediation_commands=[s.model_dump() for s in valid_suggestions], # Store suggestions too
                    status=new_status,
                )

                logger.info(f"Auto-remediation {'succeeded' if attempt.success else 'failed'} for event {event.anomaly_id}. Status set to {new_status.value}")

                # Schedule verification if successful and applicable
                if attempt.success and best_suggestion.action_type not in ["describe", "view_logs"]: # Don't verify read-only actions
                    verification_delay = getattr(settings, "REMEDIATION_VERIFICATION_DELAY_SECONDS", 60)
                    logger.info(f"Scheduling remediation verification for event {event.anomaly_id} in {verification_delay} seconds.")
                    loop = asyncio.get_event_loop()
                    # Ensure event is refreshed before passing to verification task
                    refreshed_event = await self.anomaly_event_service.get_event(event.anomaly_id)
                    if refreshed_event:
                         loop.call_later(
                            verification_delay,
                            lambda: asyncio.create_task(self._verify_remediation(refreshed_event)),
                        )
                    else:
                        logger.error(f"Could not refresh event {event.anomaly_id} before scheduling verification.")

            except Exception as e:
                logger.error(f"Error during auto-remediation execution for event {event.anomaly_id}: {e}", exc_info=True)
                # Update event with execution error
                await self.anomaly_event_service.update_event(
                    event.anomaly_id,
                    AnomalyEventUpdate(
                        notes=f"{event.notes}\nAuto-remediation execution error: {str(e)}",
                        status=AnomalyStatus.REMEDIATION_FAILED,
                    )
                )
        else:
            # Autonomous mode, but not qualified for auto-remediation (e.g., not critical)
            logger.info(f"Auto-remediation not applicable for event {event.anomaly_id} (critical={is_critical}, mode={current_mode}, direct_failure={is_direct_failure}). Adding suggestions only.")
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(
                    suggested_remediation_commands=[s.model_dump() for s in valid_suggestions],
                    status=AnomalyStatus.REMEDIATION_SUGGESTED,
                    notes=f"{event.notes}\nAuto-remediation not applicable (critical={is_critical}, mode={current_mode}, direct={is_direct_failure}). Suggestions added.",
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
        if not event:
            logger.error("Cannot verify remediation: AnomalyEvent object is None")
            return

        logger.info(
            f"Verifying remediation for event {event.anomaly_id} ({event.entity_id})"
        )
        verification_notes = f"{event.notes}\nVerification check @ {datetime.utcnow().isoformat()}. "
        final_status = AnomalyStatus.REMEDIATION_FAILED  # Default to failed unless proven otherwise
        verification_checks = []  # Track specific checks performed
        verification_reason = "Default: Remediation could not be verified as successful"

        # Exit early if no remediation attempts were made or the last one failed
        if not event.remediation_attempts:
            verification_notes += "No remediation attempts recorded, verification skipped."
            logger.warning(f"Verification skipped for {event.anomaly_id}: No remediation attempts found.")
            # Keep original status or set to failed if it was applied
            final_status = event.status if event.status != AnomalyStatus.REMEDIATION_APPLIED else AnomalyStatus.REMEDIATION_FAILED
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(status=final_status, notes=verification_notes),
            )
            return

        last_attempt = event.remediation_attempts[-1]
        if not last_attempt.success:
            verification_notes += f"Last remediation attempt failed (Command: {last_attempt.command[:50]}...). Verification skipped."
            logger.warning(f"Verification skipped for {event.anomaly_id}: Last remediation attempt failed.")
            # Status should already be REMEDIATION_FAILED, but ensure it is
            await self.anomaly_event_service.update_event(
                event.anomaly_id,
                AnomalyEventUpdate(status=AnomalyStatus.REMEDIATION_FAILED, notes=verification_notes),
            )
            return

        try:
            command = last_attempt.command
            action_type = last_attempt.action_type # Use action_type from the attempt

            # 1. Fetch current metrics for the entity
            entity_key = f"{event.entity_type}/{event.entity_id}"
            if event.namespace:
                entity_key = f"{event.namespace}/{entity_key}"

            all_current_metrics = {}
            try:
                all_current_metrics = await self._fetch_metrics() # Re-fetch latest metrics
            except Exception as metric_fetch_err:
                logger.error(f"Failed to fetch metrics during verification for {entity_key}: {metric_fetch_err}")
                verification_notes += f"\nError fetching metrics during verification: {metric_fetch_err}. Cannot verify using metrics."
                # Continue without metrics if possible

            current_entity_data = all_current_metrics.get(entity_key, {})
            current_metrics_snapshot = (
                current_entity_data.get("metrics", []) if current_entity_data else []
            )

            # 2. Get detailed K8s resource status
            current_k8s_status = {}
            resource_exists = True  # Track if resource still exists
            try:
                if event.entity_type and event.entity_id:
                    # Ensure namespace is passed correctly
                    current_k8s_status = await k8s_executor.get_resource_status(
                        resource_type=event.entity_type,
                        name=event.entity_id,
                        namespace=event.namespace, # Pass namespace if it exists
                    )
                    verification_checks.append(f"Checked Kubernetes status for {event.entity_type}/{event.entity_id}")
            except Exception as e:
                # Check if "not found" is expected (e.g., after deletion)
                is_delete_action = action_type in ["delete_pod", "delete_deployment", "restart_pod", "restart_deployment"] # Restart involves deletion
                if "not found" in str(e).lower():
                    if is_delete_action:
                        resource_exists = False
                        verification_notes += f"Resource no longer exists (expected after {action_type}). "
                        verification_checks.append("Verified resource deletion/recreation")
                    else:
                         logger.warning(f"Resource {entity_key} not found unexpectedly during verification: {e}")
                         verification_notes += f"Resource not found unexpectedly: {str(e)}. "
                         resource_exists = False # Treat as non-existent
                else:
                    logger.warning(
                        f"Could not fetch K8s status for {entity_key} during verification: {e}"
                    )
                    verification_notes += f"K8s status fetch error: {str(e)}. Verification may be incomplete."
                    # Don't assume resource doesn't exist based on API error

            # 3. Operation-specific verification logic
            is_resolved = False # Flag to mark if specific verification passed

            # Pod operations (restart, delete)
            if action_type in ["restart_pod", "delete_pod"]:
                if not resource_exists and action_type == "delete_pod":
                    is_resolved = True
                    verification_reason = "Pod was successfully deleted"
                elif resource_exists: # Check status of the (potentially new) pod after restart/delete
                    pod_phase = current_k8s_status.get("status", {}).get("phase", "Unknown")
                    container_statuses = current_k8s_status.get("status", {}).get("containerStatuses", [])
                    all_containers_ready = False
                    if container_statuses:
                         all_containers_ready = all(status.get("ready", False) for status in container_statuses)

                    # Check restart count from metrics if available
                    restart_count_low = True # Assume low unless proven otherwise
                    if current_metrics_snapshot:
                        for metric in current_metrics_snapshot:
                            metric_type = metric.get("metric_type", "")
                            if "container_restarts" in metric_type:
                                value = self._extract_metric_value(metric)
                                if value is not None and value > 1: # Allow 1 restart right after remediation
                                    restart_count_low = False
                                    break

                    if pod_phase == "Running" and all_containers_ready and restart_count_low:
                        is_resolved = True
                        verification_reason = "Pod is Running, all containers Ready, and restart count is low."
                    elif pod_phase == "Succeeded": # Also a valid end state
                         is_resolved = True
                         verification_reason = "Pod completed successfully (Phase: Succeeded)."

                    verification_checks.append(f"Checked pod phase: {pod_phase}, Ready: {all_containers_ready}, Restarts Low: {restart_count_low}")

            # Deployment operations (restart, scale)
            elif action_type in ["restart_deployment", "scale_deployment"]:
                if resource_exists:
                    spec_replicas = current_k8s_status.get("spec", {}).get("replicas", 0)
                    status_replicas = current_k8s_status.get("status", {}).get("replicas", 0)
                    available_replicas = current_k8s_status.get("status", {}).get("availableReplicas", 0)
                    ready_replicas = current_k8s_status.get("status", {}).get("readyReplicas", 0)
                    unavailable_replicas = current_k8s_status.get("status", {}).get("unavailableReplicas", 0)

                    # Check if desired state is met
                    if spec_replicas == ready_replicas and ready_replicas == available_replicas and unavailable_replicas == 0:
                        is_resolved = True
                        verification_reason = f"Deployment reached desired state: {ready_replicas}/{spec_replicas} replicas Ready and Available."
                    elif action_type == "scale_deployment":
                         # Check if scaling was successful (even if not fully ready yet)
                         target_replicas_str = last_attempt.parameters.get("replicas", "")
                         try:
                              target_replicas = int(target_replicas_str)
                              if spec_replicas == target_replicas:
                                   is_resolved = True # Assume scaling command worked, readiness check is separate
                                   verification_reason = f"Deployment scaling initiated to {target_replicas} replicas (spec updated)."
                              else:
                                   verification_reason = f"Deployment spec replicas ({spec_replicas}) do not match target ({target_replicas})."
                         except ValueError:
                              verification_reason = f"Could not determine target replicas for scaling verification ('{target_replicas_str}')."

                    verification_checks.append(f"Checked deployment replicas: Ready={ready_replicas}, Available={available_replicas}, Unavailable={unavailable_replicas}, Desired={spec_replicas}")


            # Node operations (cordon, uncordon, drain)
            elif action_type in ["cordon_node", "uncordon_node", "drain_node"]:
                if resource_exists:
                    unschedulable = current_k8s_status.get("spec", {}).get("unschedulable", False)
                    node_conditions = current_k8s_status.get("status", {}).get("conditions", [])
                    is_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in node_conditions)

                    if action_type == "cordon_node" and unschedulable:
                        is_resolved = True
                        verification_reason = "Node successfully cordoned (unschedulable=true)."
                    elif action_type == "uncordon_node" and not unschedulable and is_ready:
                        is_resolved = True
                        verification_reason = "Node successfully uncordoned and is Ready."
                    elif action_type == "drain_node" and unschedulable:
                         # Drain also implies cordon. Check pod count (best effort).
                         try:
                             # Exclude DaemonSet pods for drain verification
                             pod_count = await k8s_executor.count_pods_on_node(event.entity_id, exclude_daemonsets=True)
                             if pod_count == 0: # Expect zero non-daemonset pods
                                 is_resolved = True
                                 verification_reason = f"Node successfully drained (non-DaemonSet pods: {pod_count}) and cordoned."
                             else:
                                  verification_reason = f"Node is cordoned, but drain may be incomplete ({pod_count} non-DaemonSet pods remain)."
                         except Exception as pod_count_err:
                              logger.warning(f"Could not count pods on node {event.entity_id} during drain verification: {pod_count_err}")
                              verification_reason = f"Node is cordoned, but pod count for drain verification failed ({pod_count_err})."


                    verification_checks.append(f"Checked node status: Unschedulable={unschedulable}, Ready={is_ready}")

            # Add more specific checks for other action types (StatefulSet, DaemonSet, adjust_resources) if needed

            # 4. Generic check: Re-evaluate anomaly score using latest metrics
            anomaly_score_improved = False
            current_anomaly_score = event.anomaly_score # Use original score as baseline
            if current_metrics_snapshot:
                try:
                    # Re-predict using only the latest snapshot (simpler check)
                    # We need the historical context ideally, but this is a fallback
                    # Use the same history window as before if possible
                    historical_snapshots = self.entity_metric_history.get(entity_key, [])[:-1] # Get history excluding latest snapshot
                    prediction_result = await anomaly_detector.predict_with_forecast(
                        latest_metrics_snapshot=current_metrics_snapshot,
                        historical_snapshots=historical_snapshots
                    )
                    if prediction_result and isinstance(prediction_result, dict):
                        current_anomaly_score = prediction_result.get("anomaly_score", event.anomaly_score)
                        # Check if score is significantly lower than the original trigger score
                        # Use a relative threshold (e.g., 50% reduction) and absolute (below detection threshold)
                        detection_threshold = getattr(settings, "AUTOENCODER_THRESHOLD", 0.8)
                        if current_anomaly_score < event.anomaly_score * 0.5 or current_anomaly_score < detection_threshold * 0.8:
                            anomaly_score_improved = True
                            verification_checks.append(f"Checked anomaly score: Improved from {event.anomaly_score:.4f} to {current_anomaly_score:.4f}")
                        else:
                             verification_checks.append(f"Checked anomaly score: Still high ({current_anomaly_score:.4f}, original: {event.anomaly_score:.4f})")

                except Exception as score_check_err:
                    logger.warning(f"Error re-evaluating anomaly score for {entity_key}: {score_check_err}")
                    verification_checks.append("Anomaly score re-evaluation failed.")

            # 5. Decide final verification status
            # Trust specific operation verification first
            if is_resolved:
                final_status = AnomalyStatus.RESOLVED
                # Keep the specific verification_reason set above
            elif anomaly_score_improved:
                # If specific check failed/NA, but score improved significantly, consider resolved
                final_status = AnomalyStatus.RESOLVED
                verification_reason = f"Anomaly score significantly improved (to {current_anomaly_score:.4f}). Assuming resolved."
            else:
                # If neither specific checks nor score improvement confirm resolution, keep failed
                final_status = AnomalyStatus.REMEDIATION_FAILED
                verification_reason = f"Specific checks did not confirm resolution, and anomaly score remains high ({current_anomaly_score:.4f})."


            # 6. Use Gemini AI verification as additional input if enabled and relevant
            if self.gemini_service and settings.GEMINI_AUTO_VERIFICATION and final_status != AnomalyStatus.RESOLVED:
                logger.debug(f"Using Gemini for additional verification insight on {event.anomaly_id} (current status: {final_status.value})")
                try:
                    # Prepare context for AI verification
                    verification_context = {
                        "entity": self._deep_serialize({ # Serialize context
                            "entity_id": event.entity_id,
                            "entity_type": event.entity_type,
                            "namespace": event.namespace,
                        }),
                        "remediation": self._deep_serialize({
                            "action": action_type,
                            "command": command,
                            "success": last_attempt.success,
                            "output": last_attempt.output,
                            "error": last_attempt.error,
                        }),
                        "original_issue": self._deep_serialize({
                            "status": str(event.status.value),
                            "anomaly_score": event.anomaly_score,
                            "notes": event.notes,
                        }),
                        "current_state": self._deep_serialize({
                            "metrics_snapshot": current_metrics_snapshot[:10],  # Limit size
                            "resource_exists": resource_exists,
                            "k8s_status": current_k8s_status,
                            "current_anomaly_score": current_anomaly_score,
                        }),
                    }

                    # Get AI verification assessment
                    ai_verification = await self.gemini_service.verify_remediation(verification_context)

                    if ai_verification and "success" in ai_verification:
                        ai_resolved = ai_verification.get("success", False)
                        ai_confidence = ai_verification.get("confidence", 0.0)
                        ai_reasoning = ai_verification.get("reasoning", "No reasoning provided.")

                        verification_notes += f"\nAI Verification: Result={ai_resolved}, Confidence={ai_confidence:.2f}, Reason='{ai_reasoning}'"
                        verification_checks.append("AI verification assessment")

                        # Override rule-based decision only if AI is confident and suggests resolution
                        if ai_resolved and ai_confidence > 0.75 and final_status != AnomalyStatus.RESOLVED:
                            logger.info(f"Overriding rule-based verification for {event.anomaly_id} based on confident AI assessment.")
                            final_status = AnomalyStatus.RESOLVED
                            verification_reason = f"AI assessment indicated resolution with high confidence ({ai_confidence:.2f}): {ai_reasoning}"

                    else:
                         logger.warning(f"AI verification for {event.anomaly_id} did not return a success status or was invalid.")
                         verification_notes += "\nAI Verification: Inconclusive or failed."

                except Exception as e:
                    logger.error(f"Error during AI verification for {event.anomaly_id}: {e}", exc_info=True)
                    verification_notes += f"\nAI verification error: {str(e)}"

            # Add final reason and checks to notes
            verification_notes += f"\nFinal Verification Result: {final_status.value}. Reason: {verification_reason}. "
            verification_notes += f"Checks: {'; '.join(verification_checks)}."

        except Exception as e:
            logger.error(f"CRITICAL Error during verification of event {event.anomaly_id}: {e}", exc_info=True)
            verification_notes += f"\nCRITICAL Verification Error: {str(e)}. Setting status to FAILED."
            final_status = AnomalyStatus.REMEDIATION_FAILED # Ensure failed on major error

        # Update the event with verification results
        await self.anomaly_event_service.update_event(
            event.anomaly_id,
            AnomalyEventUpdate(
                status=final_status,
                notes=verification_notes, # Append verification details
                resolution_time=(
                    datetime.utcnow()
                    if final_status == AnomalyStatus.RESOLVED
                    else event.resolution_time # Keep existing resolution time if already resolved previously
                ),
                # Clear verification time if no longer pending
                verification_time=None if final_status != AnomalyStatus.VERIFICATION_PENDING else event.verification_time,
            ),
        )
        logger.info(
            f"Verification completed for event {event.anomaly_id}: Result={final_status.value}"
        )

    async def _fetch_metrics(self):
        """
        Fetch metrics from Prometheus using the active queries.

        Returns:
            Dict of entity metrics keyed by entity identifier (e.g., "namespace/pod/pod-name")
        """
        logger.info("Fetching metrics from Prometheus...")
        try:
            # Use centralized query module for queries if needed (currently using self.active_promql_queries)
            from app.utils.metric_tagger import MetricTagger # Ensure tagger is imported

            # Log what queries we'll execute for debugging
            queries_to_execute = list(self.active_promql_queries)
            if not queries_to_execute:
                 logger.error("No active PromQL queries found. Cannot fetch metrics.")
                 # Optionally, try to re-initialize or use fallbacks
                 # await self._initialize_queries()
                 # queries_to_execute = list(self.active_promql_queries)
                 # if not queries_to_execute:
                 return {} # Return empty if still no queries

            logger.debug(f"Executing {len(queries_to_execute)} PromQL queries:")
            # for q in queries_to_execute: logger.debug(f"  - {q}") # Reduce verbosity

            # Fetch all metrics
            all_metrics = await prometheus_scraper.fetch_all_metrics(queries_to_execute)

            # Tag metrics using the MetricTagger to add entity info and standardize format
            # This step should ideally happen within fetch_all_metrics or immediately after
            # Assuming fetch_all_metrics already returns tagged data based on its implementation
            # If not, uncomment the line below:
            # all_metrics = MetricTagger.tag_metrics_batch(all_metrics) # Apply tagging if not done internally

            # Count and log metrics fetched
            entity_count = len(all_metrics)
            metrics_count = sum(len(entity_data.get("metrics", [])) for entity_data in all_metrics.values())
            logger.info(f"Fetched {metrics_count} metric series for {entity_count} entities")

            # Log detailed metrics info for a sample entity for debugging (optional)
            if entity_count > 0 and logger.level("DEBUG").no <= logger.level(settings.LOG_LEVEL).no:
                sample_entity_key = next(iter(all_metrics.keys()))
                sample_entity = all_metrics[sample_entity_key]
                sample_metrics = sample_entity.get("metrics", [])
                logger.debug(f"Sample entity '{sample_entity_key}': Type={sample_entity.get('entity_type')}, ID={sample_entity.get('entity_id')}, Namespace={sample_entity.get('namespace')}, Metrics={len(sample_metrics)}")

                # Log details of first few metrics for the sample entity
                for idx, metric in enumerate(sample_metrics[:2]): # Show first 2 metrics only
                    metric_info = {
                        "type": metric.get('metric_type', 'unknown'),
                        "name": metric.get('metric', 'unnamed')[:50], # Limit name length
                        "query_part": metric.get('query', '')[:50], # Limit query length
                        "value_excerpt": None
                    }
                    # Safely extract value/values excerpt
                    if 'value' in metric and metric['value'] is not None:
                         metric_info['value_excerpt'] = str(metric['value'])[:30]
                    elif 'values' in metric and metric['values']:
                         metric_info['value_excerpt'] = f"[{len(metric['values'])} values] last: {str(metric['values'][-1])[:30]}"

                    logger.debug(f"  Metric {idx+1}: {metric_info}")


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
        entity_count = len(all_metrics)
        logger.info(f"Processing metrics for {entity_count} entities...")
        if entity_count == 0:
            return

        # Create processing tasks for each entity
        process_tasks = []
        for entity_key, entity_data in all_metrics.items():
            # Ensure entity_data contains necessary keys ('metrics')
            if "metrics" in entity_data:
                 # Pass the historical window for this entity from self.entity_metric_history
                 metric_window = self.entity_metric_history.get(entity_key, [])
                 process_tasks.append(
                     self._process_entity_metrics(entity_key, entity_data)
                 )
            else:
                 logger.warning(f"Skipping entity {entity_key} due to missing 'metrics' data.")

        # Execute all tasks in parallel
        results = await asyncio.gather(*process_tasks, return_exceptions=True)

        # Log any errors during processing
        errors = [res for res in results if isinstance(res, Exception)]
        if errors:
            logger.error(f"{len(errors)} errors occurred during entity processing:")
            for i, err in enumerate(errors[:5]): # Log first 5 errors
                logger.error(f"  Error {i+1}: {err}")

        logger.info(f"Entity metrics processing complete for {entity_count - len(errors)}/{entity_count} entities.")


    async def _handle_verification(self):
        """
        Handle verification of events in REMEDIATION_APPLIED status.
        """
        logger.info("Checking for events needing remediation verification...")

        try:
            # Get events that were successfully remediated but not yet verified/resolved
            try:
                # Try to determine what format the service expects
                status_enum = AnomalyStatus.REMEDIATION_APPLIED
                status_str = "REMEDIATION_APPLIED"
                
                # Debug status value information to help diagnose the issue
                logger.debug(f"Status enum info: type={type(status_enum).__name__}, "
                            f"value={getattr(status_enum, 'value', status_enum)}, "
                            f"str={str(status_enum)}")
                
                # Safer approach that directly handles the specific error we're seeing
                events_needing_verification = None
                try:
                    # Instead of trying to use the enum directly, convert to string first
                    # This avoids the 'str' object has no attribute 'value' error
                    events_needing_verification = await self.anomaly_event_service.list_events(
                        status=str(status_enum)  # Convert enum to string explicitly
                    )
                except Exception as e:
                    logger.warning(f"Error using str(enum) status value: {e}")
                    
                    # Try another query approach that doesn't require status parameter
                    try:
                        logger.info("Getting all events and filtering manually")
                        all_events = await self.anomaly_event_service.list_events()
                        if all_events:
                            # Filter events manually based on string comparison
                            events_needing_verification = []
                            for event in all_events:
                                event_status = getattr(event, 'status', None)
                                # Try multiple ways to compare status
                                if (str(event_status) == status_str or 
                                    getattr(event_status, 'name', None) == status_str or
                                    getattr(event_status, 'value', None) == status_str):
                                    events_needing_verification.append(event)
                            
                            logger.info(f"Found {len(events_needing_verification)} events through manual filtering")
                        else:
                            logger.info("No events found")
                            events_needing_verification = []
                    except Exception as fallback_err:
                        logger.error(f"Error in fallback approach: {fallback_err}", exc_info=True)
                        events_needing_verification = []

                if not events_needing_verification:
                    logger.debug("No events found in REMEDIATION_APPLIED status requiring verification.")
                    return

                logger.info(f"Found {len(events_needing_verification)} events in REMEDIATION_APPLIED status.")

                # Process verifications in parallel
                verification_tasks = [
                    self._verify_remediation(event) for event in events_needing_verification
                ]
                results = await asyncio.gather(*verification_tasks, return_exceptions=True)

                # Log errors during verification
                errors = [res for res in results if isinstance(res, Exception)]
                if errors:
                    logger.error(f"{len(errors)} errors occurred during verification handling:")
                    for i, err in enumerate(errors[:5]):
                        logger.error(f"  Verification Error {i+1}: {err}")

            except Exception as e:
                logger.warning(f"Could not query events with REMEDIATION_APPLIED status: {e}")
                # Use improved fallback that doesn't rely on list_events with status filter
                try:
                    # Get all events and filter them ourselves
                    all_events = await self.anomaly_event_service.list_events()
                    if not all_events:
                        logger.info("No events found in fallback query")
                        return
                    
                    # Use our helper method to find matching events
                    events_needing_verification = self._filter_events_by_status(
                        all_events, 
                        AnomalyStatus.REMEDIATION_APPLIED,
                        "REMEDIATION_APPLIED"
                    )
                    
                    if events_needing_verification:
                        logger.info(f"Found {len(events_needing_verification)} events needing verification using fallback method")
                        verification_tasks = [
                            self._verify_remediation(event) for event in events_needing_verification
                        ]
                        await asyncio.gather(*verification_tasks, return_exceptions=True)
                    else:
                        logger.info("No events matched REMEDIATION_APPLIED status in fallback approach")
                        
                except Exception as fallback_err:
                    logger.error(f"Fallback verification method also failed: {str(fallback_err)}", exc_info=True)

        except Exception as e:
            logger.error(f"Error during verification handling: {e}", exc_info=True)
    
    def _filter_events_by_status(self, events, status_enum, status_str):
        """
        Helper method to filter events by status with flexible comparison.
        
        Args:
            events: List of events to filter
            status_enum: Status as enum value
            status_str: Status as string
            
        Returns:
            List of events matching the status
        """
        if not events:
            return []
        
        matching_events = []
        for event in events:
            if not hasattr(event, 'status'):
                continue
                
            # Log detailed debug info for the first few events to help diagnosis
            if len(matching_events) < 2:
                event_status = event.status
                logger.debug(f"Event {getattr(event, 'anomaly_id', 'unknown')}: "
                            f"status type={type(event_status).__name__}, "
                            f"status={str(event_status)}, "
                            f"has value attr: {hasattr(event_status, 'value')}")
                if hasattr(event_status, 'value'):
                    logger.debug(f"  status.value={event_status.value}")
            
            # Try multiple comparison approaches to be flexible
            if any([
                # Direct enum comparison
                event.status == status_enum,
                # Compare values if both have value attribute
                hasattr(event.status, 'value') and hasattr(status_enum, 'value') and 
                event.status.value == status_enum.value,
                # String representation comparison
                str(event.status) == status_str,
                # String value comparison if available
                hasattr(event.status, 'value') and str(event.status.value) == status_str,
                # Handle case where status itself is a string
                isinstance(event.status, str) and event.status == status_str
            ]):
                matching_events.append(event)
                logger.debug(f"Matched event: {getattr(event, 'anomaly_id', 'unknown')}")
                
        return matching_events

    async def run_cycle(self):
        """Runs a single cycle of fetch, detect, remediate, verify."""
        cycle_start_time = datetime.utcnow()
        logger.info(f"Starting worker cycle at {cycle_start_time.isoformat()}... Mode: {mode_service.get_mode()}")

        if not self.is_initialized:
            logger.info("Worker not initialized, attempting initialization...")
            await self._initialize_queries()
            if not self.is_initialized:
                logger.error("Worker initialization failed. Skipping cycle.")
                return

        try:
            # 1. Fetch metrics from Prometheus
            fetch_start = datetime.utcnow()
            all_metrics = await self._fetch_metrics()
            logger.info(f"Metric fetching took {(datetime.utcnow() - fetch_start).total_seconds():.2f}s")

            # 2. Process metrics: update history, detect anomalies, trigger analysis/remediation logic
            process_start = datetime.utcnow()
            await self._process_entities(all_metrics)
            logger.info(f"Entity processing took {(datetime.utcnow() - process_start).total_seconds():.2f}s")

            # 3. Handle verification of previously applied remediations
            verify_start = datetime.utcnow()
            await self._handle_verification()
            logger.info(f"Verification handling took {(datetime.utcnow() - verify_start).total_seconds():.2f}s")

            # 4. Cleanup stale entries in metric history
            cleanup_start = datetime.utcnow()
            await self._cleanup_stale_metric_history()
            logger.info(f"History cleanup took {(datetime.utcnow() - cleanup_start).total_seconds():.2f}s")

            # 5. Direct scan for obvious failures (e.g., CrashLoopBackOff)
            scan_start = datetime.utcnow()
            await self._direct_scan_for_failures()
            logger.info(f"Direct failure scan took {(datetime.utcnow() - scan_start).total_seconds():.2f}s")
            
            # ... existing code ...

        except Exception as e:
            logger.error(f"CRITICAL Error encountered in worker cycle: {e}", exc_info=True)

        cycle_duration = (datetime.utcnow() - cycle_start_time).total_seconds()
        logger.info(f"Worker cycle finished in {cycle_duration:.2f} seconds.")


    async def _cleanup_stale_metric_history(self):
        """
        Cleanup stale entries in the entity_metric_history based on last update time.
        """
        current_time = datetime.utcnow()
        # Check if cleanup interval has passed
        if (current_time - self.last_cleanup_time).total_seconds() < self.cleanup_interval_seconds:
            return

        logger.info(f"Running cleanup for stale metric history (older than {self.stale_entity_timeout_seconds}s)...")
        stale_threshold_time = current_time - timedelta(seconds=self.stale_entity_timeout_seconds)
        keys_to_delete = []
        initial_count = len(self.entity_metric_history)

        for entity_key, history_window in self.entity_metric_history.items():
            if not history_window: # Skip empty histories
                keys_to_delete.append(entity_key)
                continue

            # Get the timestamp from the latest metric snapshotin the window
            latest_snapshot = history_window[-1]
            if not latest_snapshot: # Skip if latest snapshot is empty
                keys_to_delete.append(entity_key)
                continue

            # Find a metric with a timestamp in the latest snapshot
            latest_timestamp_str = None
            for metric in latest_snapshot:
                # Prometheus value is [timestamp, value_str]
                if metric and isinstance(metric.get("value"), list) and len(metric["value"]) == 2:
                     try:
                         # Use the timestamp from the metric value directly
                         ts_float = float(metric["value"][0])
                         latest_timestamp_dt = datetime.fromtimestamp(ts_float)
                         # Check if this timestamp is more recent than the current latest
                         if latest_timestamp_str is None or latest_timestamp_dt > datetime.fromisoformat(latest_timestamp_str):
                             latest_timestamp_str = latest_timestamp_dt.isoformat()
                     except (ValueError, TypeError, OSError):
                         continue # Ignore invalid timestamps
                elif metric and metric.get("timestamp"): # Fallback to snapshot timestamp if available
                     latest_timestamp_str = metric.get("timestamp")
                     break # Assume first timestamp is representative

            if latest_timestamp_str:
                try:
                    last_update_time = datetime.fromisoformat(latest_timestamp_str.replace("Z", "+00:00")) # Handle Z timezone
                    if last_update_time < stale_threshold_time:
                        keys_to_delete.append(entity_key)
                except ValueError:
                    logger.warning(f"Invalid timestamp format '{latest_timestamp_str}' for entity {entity_key}, marking for cleanup.")
                    keys_to_delete.append(entity_key)
            else:
                # If no timestamp found in the latest snapshot, mark for cleanup
                logger.warning(f"No valid timestamp found in latest snapshot for entity {entity_key}, markingfor cleanup.")
                keys_to_delete.append(entity_key)

        deleted_count = 0
        for key in keys_to_delete:
            if key in self.entity_metric_history:
                del self.entity_metric_history[key]
                deleted_count += 1
                logger.debug(f"Removed stale entity from history: {key}")

        if deleted_count > 0:
                logger.debug(f"Removed stale entity from history: {key}")

        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} stale entities from metric history. Remaining: {initial_count - deleted_count}")
        else:
            logger.debug("No stale entities found in metric history during cleanup.")

        self.last_cleanup_time = current_time # Update last cleanup time regardless

    async def _direct_scan_for_failures(self):
        """
        Directly scan for existing failures in the cluster by checking resource status.
        This complements metric-based detection by catching obvious hard failures quickly.
        """
        logger.info("Starting direct scan for Kubernetes resource failures...")
        failure_count = 0
        processed_failures = set() # Track "namespace/type/name" to avoid duplicates per cycle

        resource_types_to_scan = {
            "pod": ["Failed", "Pending", "Unknown", "CrashLoopBackOff", "ImagePullBackOff", "Error"], # Include common waiting reasons
            "deployment": ["Degraded", "ProgressDeadlineExceeded", "ReplicaFailure"],
            "node": ["NotReady", "SchedulingDisabled", "Pressure"], # Check for pressure conditions too
            "service": ["NoEndpoints", "EndpointsNotReady"],
            "persistentvolumeclaim": ["Failed", "Pending", "Lost"],
            # Add other relevant types like StatefulSet, DaemonSet if needed
            "statefulset": ["Degraded", "UpdateNotReady"],
            "daemonset": ["Misscheduled", "DesiredNotReady"],
        }

        try:
            for resource_type, statuses_to_check in resource_types_to_scan.items():
                logger.debug(f"Scanning for failures in resource type: {resource_type}")
                problematic_resources = []
                try:
                    # List resources across all namespaces for most types, except Node
                    target_namespace = None if resource_type == "node" else "" # "" means all namespaces
                    problematic_resources = await k8s_executor.list_resources_with_status(
                        resource_type=resource_type,
                        status_filter=statuses_to_check,
                        namespace=target_namespace
                    )
                except Exception as list_err:
                     logger.error(f"Error listing {resource_type} resources during direct scan: {list_err}", exc_info=True)
                     continue # Skip this resource type if listing fails

                if not problematic_resources:
                    logger.debug(f"No resources found with problematic statuses for {resource_type}.")
                    continue

                logger.info(f"Found {len(problematic_resources)} potentially problematic {resource_type}(s). Analyzing...")

                for resource in problematic_resources:
                    try:
                        metadata = resource.get("metadata", {})
                        resource_name = metadata.get("name")
                        resource_namespace = metadata.get("namespace") # Will be None for Node

                        if not resource_name:
                            logger.warning(f"Found a {resource_type} resource with no name, skipping.")
                            continue

                        # Create unique key to avoid processing the same failure multiple times in one cycle
                        failure_key = f"{resource_namespace or '_'}/{resource_type}/{resource_name}"
                        if failure_key in processed_failures:
                            continue

                        status_data = resource.get("status", {})
                        actual_status, reason, message = "Unknown", "DirectScanFailure", f"{resource_type.capitalize()} issue detected"

                        # Extract more specific reason/message based on resource type
                        if resource_type == "pod":
                            actual_status = status_data.get("phase", "Unknown")
                            # Check container statuses for specific reasons
                            container_statuses = status_data.get("containerStatuses", []) or []
                            for cs in container_statuses:
                                if cs.get("state", {}).get("waiting"):
                                    waiting_reason = cs["state"]["waiting"].get("reason")
                                    if waiting_reason in statuses_to_check: # Check if it's a failure reason we care about
                                        reason = waiting_reason
                                        message = cs["state"]["waiting"].get("message", f"Container waiting: {reason}")
                                        actual_status = "Waiting" # More specific than Pod Phase sometimes
                                        break # Take the first container failure reason
                                elif cs.get("state", {}).get("terminated"):
                                     term_reason = cs["state"]["terminated"].get("reason")
                                     if term_reason == "Error" or term_reason == "OOMKilled":
                                          reason = term_reason
                                          message = cs["state"]["terminated"].get("message", f"Container terminated: {reason}")
                                          actual_status = "Terminated"
                                          break
                            # If still unknown, check pod conditions
                            if reason == "DirectScanFailure":
                                conditions = status_data.get("conditions", []) or []
                                for cond in conditions:
                                     if cond.get("status") == "False" and cond.get("type") in ["Ready", "ContainersReady"]:
                                          reason = cond.get("reason", f"{cond.get('type')}NotReady")
                                          message = cond.get("message", f"Pod condition {cond.get('type')} is False")
                                          break
                                     elif cond.get("status") == "True" and cond.get("type") == "PodScheduled" and actual_status == "Pending":
                                          # If scheduled but still pending, likely image pull or volume issue
                                          reason = "StuckPending"
                                          message = "Pod is scheduled but remains in Pending state."

                        elif resource_type == "deployment":
                            actual_status = "Degraded" # Assume based on filter
                            conditions = status_data.get("conditions", []) or []
                            for cond in conditions:
                                if cond.get("status") != "True" and cond.get("type") in ["Available", "Progressing"]:
                                    reason = cond.get("reason", f"{cond.get('type')}Issue")
                                    message = cond.get("message", f"Deployment condition {cond.get('type')} is not True")
                                    break # Take first problematic condition

                        elif resource_type == "node":
                            actual_status = "Problematic"
                            conditions = status_data.get("conditions", []) or []
                            for cond in conditions:
                                if cond.get("type") == "Ready" and cond.get("status") != "True":
                                    reason = cond.get("reason", "NodeNotReady")
                                    message = cond.get("message", "Node Ready condition is not True")
                                    break
                                elif cond.get("status") == "True" and cond.get("type").endswith("Pressure"):
                                     reason = cond.get("reason", f"{cond.get('type')}Active")
                                     message = cond.get("message", f"Node has active {cond.get('type')}")
                                     break

                        elif resource_type == "persistentvolumeclaim":
                             actual_status = status_data.get("phase", "Unknown")
                             reason = f"PVC_{actual_status}"
                             message = f"PersistentVolumeClaim is in {actual_status} phase."
                             conditions = status_data.get("conditions", []) or []
                             # Find specific conditions if available
                             for cond in conditions:
                                  if cond.get("status") == "True" and cond.get("type") == "FileSystemResizePending":
                                       reason = "PVC_ResizePending"
                                       message = cond.get("message", "PVC filesystem resize is pending.")
                                       break
                                  elif cond.get("status") == "False": # Any condition that's false might be an issue
                                       reason = f"PVC_{cond.get('type')}False"
                                       message = cond.get("message", f"PVC Condition {cond.get('type')} is False")
                                       break

                        # Add other resource types as needed

                        # Log and handle the detected failure
                        logger.warning(f"DIRECT FAILURE DETECTED: {failure_key} - Status: {actual_status}, Reason: {reason}")
                        failure_count += 1
                        processed_failures.add(failure_key) # Mark as processed for this cycle

                        await self._handle_direct_failure(
                            entity_type=resource_type,
                            entity_id=resource_name,
                            namespace=resource_namespace,
                            status=actual_status,
                            reason=reason,
                            message=message,
                            resource_data=resource # Pass the whole resource for context
                        )
                    except Exception as e:
                        logger.error(f"Error processing resource {resource_name}: {e}", exc_info=True)
                
                # Log when no problematic resources are found for the type
                if not problematic_resources:
                    logger.debug(f"No resources found with problematic statuses for {resource_type}.")
                    
            # After processing all resource types, log completion
            logger.info("Direct scan completed. No new direct failures found.")

            return failure_count
        except Exception as e:
            logger.error(f"Critical error during direct failure scan: {e}", exc_info=True)
            return 0 # Return 0 failures if the scan itself failed

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
        Handle a directly detected failure by creating an event and triggering remediation.
        """
        # Ensure resource_data is a dict, even if it's None or invalid
        if not isinstance(resource_data, dict):
            logger.error(f"Invalid resource_data format for {entity_type}/{entity_id}: {type(resource_data)}. Using empty dict.")
            resource_data = {} # Fallback to empty dict

        # Sanitize inputs
        status = status or "Unknown"
        reason = reason or "DirectScanFailure"
        message = message or f"Issue detected with {entity_type}/{entity_id}"

        entity_key = f"{entity_type}/{entity_id}"
        if namespace:
            entity_key = f"{namespace}/{entity_key}"

        logger.info(f"Handling direct failure for: {entity_key}")

        try:
            # Check if there's already an active (non-resolved/closed) event for this entity
            active_event = await self.anomaly_event_service.find_active_event(
                entity_id=entity_id, entity_type=entity_type, namespace=namespace
            )

            if active_event:
                logger.info(f"Active event {active_event.anomaly_id} already exists for {entity_key}. Updating notes.")
                # Add a note confirming the direct detection, unless it's already being verified/remediated
                if active_event.status not in [
                    AnomalyStatus.VERIFICATION_PENDING,
                    AnomalyStatus.REMEDIATION_APPLIED,
                    AnomalyStatus.REMEDIATION_ATTEMPTED, # Assuming this is deprecated/internal
                    AnomalyStatus.RESOLVED,
                    AnomalyStatus.CLOSED
                ]:
                    update_notes = f"Direct scan confirmation @ {datetime.utcnow().isoformat()}: Status={status}, Reason={reason} - {message}"
                    new_notes = f"{active_event.notes}\n{update_notes}" if active_event.notes else update_notes
                    # Potentially upgrade status if the direct failure is critical
                    new_status = active_event.status
                    is_critical_failure = status in ["Failed", "CrashLoopBackOff", "Error"] or entity_type == "node"
                    if is_critical_failure and active_event.status != AnomalyStatus.CRITICAL_FAILURE:
                        new_status = AnomalyStatus.CRITICAL_FAILURE
                        logger.warning(f"Escalating event {active_event.anomaly_id} to CRITICAL due to direct failure detection.")

                    await self.anomaly_event_service.update_event(
                        active_event.anomaly_id,
                        AnomalyEventUpdate(notes=new_notes, status=new_status)
                    )
                else:
                    logger.info(f"Event {active_event.anomaly_id} is already in state {active_event.status.value}, not updating further based on direct scan.")
                return # Don't create a new event or re-trigger remediation if active

            # Create new anomaly event if no active one exists
            operation_id = uuid.uuid4().hex[:8] # Short ID for logging correlation
            logger.info(f"[OpID:{operation_id}] No active event found. Creating new anomaly event for direct failure: {entity_key}")

            # Determine event status based on severity
            anomaly_status = AnomalyStatus.FAILURE_DETECTED
            is_critical_failure = status in ["Failed", "CrashLoopBackOff", "Error", "NodeNotReady", "NotReady", "Lost"] or entity_type == "node"
            if is_critical_failure:
                anomaly_status = AnomalyStatus.CRITICAL_FAILURE

            # Prepare event data
            event_data = {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "namespace": namespace,
                "status": anomaly_status,
                "anomaly_score": 1.0, # Assign max score for direct failures
                "anomaly_type": reason, # Use the failure reason as the type
                "notes": f"Direct failure detected @ {datetime.utcnow().isoformat()}: Status={status}, Reason={reason} - {message}",
                "metric_snapshot": [], # No metrics associated with direct scan initially
                "resource_data": self._deep_serialize(resource_data), # Store resource state
                "prediction_data": {"failure_source": "direct_scan"},
                "is_proactive": False,
                "anomaly_id": str(uuid.uuid4()) # Generate ID
            }

            # Create the event
            event = await self.anomaly_event_service.create_event(event_data)

            if not event or not event.anomaly_id:
                 logger.error(f"[OpID:{operation_id}] Failed to create anomaly event for {entity_key} via service.")
                 return # Cannot proceed without an event

            logger.info(f"[OpID:{operation_id}] Created new anomaly event: {event.anomaly_id} ({anomaly_status.value}) for {entity_key}")

            # Generate remediation suggestions (AI or Fallback)
            remediation_actions = []

            # Try Gemini AI suggestions first if enabled
            if self.gemini_service and not self.gemini_service.is_api_key_missing() and getattr(settings, "GEMINI_AUTO_ANALYSIS", True):
                logger.debug(f"[OpID:{operation_id}] Attempting AI remediation suggestion for {event.anomaly_id}")
                try:
                    failure_context = {
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "namespace": namespace,
                        "status": status,
                        "reason": reason,
                        "message": message,
                        "resource_data": self._deep_serialize(resource_data), # Ensure serializable
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                    remediation_res = await self.gemini_service.suggest_remediation(failure_context)

                    if remediation_res and isinstance(remediation_res, dict) and not remediation_res.get("error"):
                         steps = remediation_res.get("steps", [])
                         confidence = remediation_res.get("confidence", 0.6)
                         if steps:
                              logger.info(f"[OpID:{operation_id}] Received {len(steps)} AI remediation suggestions for {event.anomaly_id}")
                              for step in steps:
                                   action = None
                                   if isinstance(step, str):
                                        action = self._create_safe_remediation_action(
                                             action_type="command", resource_type=entity_type, resource_name=entity_id, namespace=namespace,
                                             parameters={"command": step}, confidence=confidence
                                        )
                                   elif isinstance(step, dict) and "action_type" in step:
                                        action = self._create_safe_remediation_action(
                                             action_type=step.get("action_type", "command"),
                                             resource_type=step.get("resource_type", entity_type),
                                             resource_name=step.get("resource_name", entity_id),
                                             namespace=step.get("namespace", namespace),
                                             parameters=step.get("parameters", {}),
                                             confidence=step.get("confidence", confidence)
                                        )
                                   if action:
                                        remediation_actions.append(action)
                         else:
                              logger.warning(f"[OpID:{operation_id}] AI returned suggestions but 'steps' list was empty for {event.anomaly_id}")

                         # Add risks to notes if provided
                         risks = remediation_res.get("risks")
                         if risks:
                              risks_note = "\nAI Identified Risks: " + ", ".join(risks)
                              await self.anomaly_event_service.update_event(
                                   event.anomaly_id, AnomalyEventUpdate(notes=event.notes + risks_note)
                              )
                              event.notes += risks_note # Update local copy

                except Exception as e:
                    logger.error(f"[OpID:{operation_id}] Error getting AI remediation suggestions for {event.anomaly_id}: {e}", exc_info=True)
                    # Continue with fallback logic

            # Generate Fallback suggestions if AI failed or provided none
            if not remediation_actions:
                logger.info(f"[OpID:{operation_id}] No AI suggestions, generating fallback remediation for {event.anomaly_id}")
                fallback_action = None
                # Pod specific fallbacks
                if entity_type == "pod":
                    if reason == "CrashLoopBackOff":
                        fallback_action = self._create_safe_remediation_action(
                            action_type="restart_pod", resource_type="pod", resource_name=entity_id, namespace=event.namespace,
                            parameters={"pod_name": entity_id, "namespace": event.namespace or "default"}, confidence=0.7
                        )
                    elif reason in ["ImagePullBackOff", "ErrImagePull"]:
                         # Describe first, then maybe suggest checking image/secret
                         fallback_action = self._create_safe_remediation_action(
                            action_type="describe", resource_type="pod", resource_name=entity_id, namespace=event.namespace,
                            parameters={}, confidence=0.6
                         )
                    elif status == "Pending" or reason == "StuckPending":
                         fallback_action = self._create_safe_remediation_action(
                            action_type="describe", resource_type="pod", resource_name=entity_id, namespace=namespace,
                            parameters={}, confidence=0.6
                         )
                    elif reason == "OOMKilled":
                        # Suggest describing, then potentially resource adjustment
                        fallback_action = self._create_safe_remediation_action(
                            action_type="describe", resource_type="pod", resource_name=entity_id, namespace=namespace, confidence=0.7
                        )
                    # Generic pod failure
                    elif fallback_action is None:
                         fallback_action = self._create_safe_remediation_action(
                            action_type="restart_pod", resource_type="pod", resource_name=entity_id, namespace=namespace, confidence=0.6 # Lower confidence general restart
                        )
                # Node specific fallback
                elif entity_type == "node" and is_critical_failure:
                    fallback_action = self._create_safe_remediation_action(
                        action_type="cordon_node", resource_type="node", resource_name=entity_id, namespace=None,
                        parameters={"name": entity_id}, confidence=0.7
                    )
                # Generic fallback for any other type: describe
                if fallback_action is None:
                     fallback_action = self._create_safe_remediation_action(
                        action_type="describe", resource_type=entity_type, resource_name=entity_id, namespace=namespace, confidence=0.5
                    )

                if fallback_action:
                    remediation_actions.append(fallback_action)
                    logger.info(f"[OpID:{operation_id}] Added fallback suggestion: {fallback_action.action_type} for {event.anomaly_id}")

            # Update event with suggestions before deciding remediation
            if remediation_actions:
                 try:
                      update_op_id = uuid.uuid4().hex[:8]
                      logger.debug(f"[OpID:{update_op_id}] Updating event {event.anomaly_id} with {len(remediation_actions)} suggestions.")
                      await self.anomaly_event_service.update_event(
                           event.anomaly_id,
                           AnomalyEventUpdate(
                                suggested_remediation_commands=[ra.model_dump() for ra in remediation_actions],
                                status=AnomalyStatus.REMEDIATION_SUGGESTED if event.status != AnomalyStatus.CRITICAL_FAILURE else event.status # Don't downgrade critical status
                           ),
                      )
                      # Refresh local event object after update
                      event = await self.anomaly_event_service.get_event(event.anomaly_id)
                      if not event:
                           logger.error(f"[OpID:{update_op_id}] Failed to refresh event {event.anomaly_id} after updating suggestions.")
                           return # Cannot proceed if event is lost
                 except Exception as e:
                      logger.error(f"[OpID:{update_op_id}] Error updating event {event.anomaly_id} with suggestions: {e}", exc_info=True)
                      # Proceed with remediation decision anyway, but log the error

            # Make remediation decision based on mode, suggestions, and criticality
            if remediation_actions:
                try:
                    decision_op_id = uuid.uuid4().hex[:8]
                    logger.info(f"[OpID:{decision_op_id}] Deciding remediation for direct failure event: {event.anomaly_id}")
                    await self._decide_remediation(
                        event=event, # Pass the potentially updated event
                        ai_suggestions=remediation_actions, # Pass all suggestions
                        is_critical=is_critical_failure,
                        is_predicted=False,
                        is_direct_failure=True, # Mark as direct failure
                    )
                    logger.info(f"[OpID:{decision_op_id}] Completed remediation decision for event: {event.anomaly_id}")
                except Exception as remediation_error:
                    logger.error(
                        f"[OpID:{decision_op_id}] Error during remediation decision for {entity_key}: {remediation_error}",
                        exc_info=True
                    )
                    # Update event with error note
                    error_op_id = uuid.uuid4().hex[:8]
                    logger.info(f"[OpID:{error_op_id}] Adding remediation decision error note to event: {event.anomaly_id}")
                    await self.anomaly_event_service.update_event(
                        event.anomaly_id,
                        AnomalyEventUpdate(
                            notes=f"{event.notes}\nError during remediation decision: {str(remediation_error)}",
                            status=AnomalyStatus.REMEDIATION_FAILED # Mark as failed if decision logic breaks
                        ),
                    )
            else:
                 logger.warning(f"[OpID:{operation_id}] No remediation actions generated for direct failure {event.anomaly_id}. No remediation attempted.")
                 # Update status to indicate no action could be taken
                 await self.anomaly_event_service.update_event(
                      event.anomaly_id,
                      AnomalyEventUpdate(
                           status=AnomalyStatus.REMEDIATION_FAILED, # Or SUGGESTION_FAILED
                           notes=f"{event.notes}\nNo remediation actions could be generated."
                      )
                 )

        except Exception as e:
            logger.error(
                f"CRITICAL Error handling direct failure for {entity_key}: {e}",
                exc_info=True,
            )

    async def run(self):
        """The main continuous loop for the worker.""" # FIX: Closed docstring
        logger.info("AutonomousWorker starting run loop...")

        # Log startup summary before first cycle
        await self._log_startup_summary()

        while True:
            await self.run_cycle()
            sleep_interval = settings.WORKER_SLEEP_INTERVAL_SECONDS
            logger.debug(
                f"Worker sleeping for {sleep_interval} seconds..."
            )
            await asyncio.sleep(sleep_interval)

    # _scan_for_anomalies method seems redundant given _process_entities and _predict_and_handle_anomaly
    # If needed, it should be updated to use the current structure. For now, commenting out.
    # async def _scan_for_anomalies(self, metrics_snapshot_list=None): ...

    async def _discover_metric_metadata(self, metric_name, start_time=None, timeout=5):
        """
        Discovers metadata about a metric from Prometheus to better understand its structure and labels.
        Note: This method is currently not used in the main worker loop but kept for potential future use.

        Args:
            metric_name: The name of the metric to discover metadata for
            start_time: Optional start time for timeout calculation
            timeout: Maximum seconds to spend on this operation

        Returns:
            Dictionary with metric metadata including type, help text, and sample labels
        """
        # Add timeout logic if start_time is provided
        if start_time and (datetime.utcnow() - start_time).total_seconds() > timeout:
             logger.warning(f"Timeout discovering metadata for {metric_name}")
             return None # Indicate timeout

        metadata = {
            "type": None,
            "help": None, # Prometheus metadata endpoint could provide this, but not implemented here
            "base_labels": [],
            "sample_labels": {},
            "sample_value": None,
            "metric_name": metric_name # Store the original name
        }

        try:
            # Basic query validation (e.g., count)
            validation_query = f"count({metric_name})"
            validation = await prometheus_scraper.validate_query(validation_query)
            if not validation["valid"]:
                logger.debug(f"Metric '{metric_name}' seems invalid or non-existent based on count query.")
                return None # Return None if metric likely doesn't exist

            # Get a sample of the metric to understand its structure and labels
            # Use instant query and limit to get structure quickly
            sample_query = f"{metric_name}" # Instant query for labels/value
            samples = await prometheus_scraper.query_prometheus(sample_query, instant=True)

            if samples and samples.get("status") == "success" and samples.get("data", {}).get("result"):
                # Get the first result sample
                sample_result = samples["data"]["result"][0]
                metadata["sample_labels"] = sample_result.get("metric", {})
                metadata["base_labels"] = list(metadata["sample_labels"].keys())
                # Value is [timestamp, value_string]
                if "value" in sample_result and len(sample_result["value"]) == 2:
                     metadata["sample_value"] = sample_result["value"][1]
                     # Keep as string initially

                     # Attempt to convert to float if possible
                     try: metadata["sample_value"] = float(metadata["sample_value"])
                     except (ValueError, TypeError): pass
                elif "values" in sample_result and isinstance(sample_result["values"], list) and len(sample_result["values"]) > 0:
                     # Handle range vector case, take the last value
                     metadata["sample_value"] = sample_result["values"][-1][1] # [ts, value_str]
                     try: metadata["sample_value"] = float(metadata["sample_value"])
                     except (ValueError, TypeError): pass

            else:
                 logger.debug(f"No samples returned for metric '{metric_name}' during metadata discovery.")
                 # We know it's valid from count, but has no current value. Keep basic metadata.
                 metadata["type"] = "gauge" # Assume gauge if no samples

            # Determine metric type based on naming conventions (heuristic)
            if (metric_name.endswith("_total") or metric_name.endswith("_sum") or metric_name.endswith("_count")):
                metadata["type"] = "counter"
            elif metric_name.endswith("_bucket"):
                metadata["type"] = "histogram_bucket" # More specific
            elif metric_name.endswith("_seconds"): # Matches histogram sum/count often
                 # Initialize available_metrics as empty list if not provided
                 available_metrics = getattr(self, 'available_metrics', [])
                 if f"{metric_name}_count" in available_metrics or f"{metric_name}_sum" in available_metrics: # Crude check
                     metadata["type"] = "histogram_or_summary"
                 else:
                     metadata["type"] = "gauge" # Assume gauge if not clearly counter/histogram
            elif metric_name.endswith("_bytes"):
                metadata["type"] = "gauge" # Usually gauges (e.g., memory usage) unless _total
            elif metric_name.startswith("kube_") and "_status_" in metric_name:
                metadata["type"] = "status_gauge"
            elif metric_name.startswith("kube_") and "_info" in metric_name:
                 metadata["type"] = "info_gauge"
            else:
                metadata["type"] = "gauge"  # Default assumption

            return metadata
        except Exception as e:
            logger.warning(f"Error discovering metadata for {metric_name}: {e}", exc_info=True)
            return None # Return None on error

    async def _build_optimal_query(
        self, metric_name, metadata, metric_type_context, rate_interval="5m"
    ):
        """
        Builds an potentially optimal query for a metric based on its metadata and intended use context.
        Note: This method is currently not used in the main worker loop but kept for potential future use.


        Args:
            metric_name: The name of the metric
            metadata: Metadata about the metric from _discover_metric_metadata
            metric_type_context: The kind of information we want (e.g., cpu_usage, memory_usage)
            rate_interval: Rate interval string (e.g., "5m") for rate() functions

        Returns:
            A tuple of (query_string, entity_type) where entity_type is pod, node, etc. or (None, None)
        """
        query = None
        entity_type = "unknown"
        if not metadata:
            logger.debug(f"Cannot build query for {metric_name}: metadata is missing.")
            return None, None

        labels = metadata.get("base_labels", [])
        metric_category = metadata.get("type") # e.g., counter, gauge

        # Identify the primary entity type based on common labels
        if "pod" in labels and "namespace" in labels:
            entity_type = "pod"
            # Container specific?
            if "container" in labels and container != "POD" and container != "":
                 entity_type = "container"
        elif "node" in labels:
            entity_type = "node"
        elif "deployment" in labels and "namespace" in labels:
            entity_type = "deployment"
        elif "statefulset" in labels and "namespace" in labels:
            entity_type = "statefulset"
        elif "daemonset" in labels and "namespace" in labels:
             entity_type = "daemonset"
        elif "job_name" in labels and "namespace" in labels:
            entity_type = "job" # Kubernetes job
        elif "instance" in labels: # Generic instance label
            entity_type = "instance"

        # Aggregation labels based on entity type
        agg_labels = ""
        if entity_type == "container":
             agg_labels = "namespace, pod, container"
        elif entity_type == "pod":
             agg_labels = "namespace, pod"
        elif entity_type in ["deployment", "statefulset", "daemonset", "job"]:
             agg_labels = f"namespace, {entity_type}" # Use the specific type
        elif entity_type == "node":
            agg_labels = "node"
        elif entity_type == "instance":
            agg_labels = "instance"

        # Build query based on metric category and context
        if metric_category == "counter":
            # Always apply rate or increase to counters for meaningful analysis
            query = f"sum(rate({metric_name}[{rate_interval}])) by ({agg_labels})" if agg_labels else f"sum(rate({metric_name}[{rate_interval}]))"
        elif metric_category == "gauge":
            # Aggregate gauges, usually sum makes sense, but avg might be better for ratios
            # Context might help decide sum vs avg, defaulting to sum
            query = f"sum({metric_name}) by ({agg_labels})" if agg_labels else f"sum({metric_name})"
        elif metric_category == "status_gauge":
            # Status gauges often represent state (0 or 1). Summing might be okay, or just raw values.
            # Example: kube_pod_status_phase might need specific phase filtering
            if metric_name == "kube_pod_status_phase":
                 # Query for running pods specifically
                 query = f"sum by (namespace, pod) ({metric_name}{{phase='Running'}} == 1)"
                 entity_type = "pod" # Override entity type based on query structure
            elif metric_name == "kube_deployment_status_replicas_available":
                  query = f"sum by (namespace, deployment) ({metric_name})"
                  entity_type = "deployment"
            else:
                 # Generic status: just sum by detected entity
                 query = f"sum({metric_name}) by ({agg_labels})" if agg_labels else f"sum({metric_name})"

        elif metric_category == "histogram_or_summary" or metric_category == "histogram_bucket":
            # Histograms are complex. A common use is calculating quantiles or rates of counts.
            # Example: Calculate p95 latency from histogram buckets
            if metric_name.endswith("_bucket"):
                 base_name = metric_name.replace("_bucket", "")
                 # Check if sum and count exist
                 if f"{base_name}_sum" in available_metrics and f"{base_name}_count" in available_metrics:
                      query = f"histogram_quantile(0.95, sum(rate({metric_name}[{rate_interval}])) by (le, {agg_labels}))" if agg_labels else f"histogram_quantile(0.95, sum(rate({metric_name}[{rate_interval}])) by (le))"
                 else:
                      logger.debug(f"Cannot build histogram query for {metric_name}: missing sum/count.")
                      query = None # Cannot form a good query
            else:
                 # Might be a summary (_sum, _count)
                 query = f"sum(rate({metric_name}[{rate_interval}])) by ({agg_labels})" if agg_labels else f"sum(rate({metric_name}[{rate_interval}]))" # Treat like counter rate


        # If we couldn't determine a good query, return None
        if query is None:
             logger.debug(f"Could not determine optimal query for {metric_name} (Category: {metric_category}, Context: {metric_type_context})")
             return None, None

        logger.debug(f"Built query for {metric_name}: '{query}' (Entity: {entity_type})")
        return query, entity_type

    async def _test_and_rank_queries(self, candidate_queries, metric_type):
        """
        Tests multiple candidate queries and ranks them by quality and suitability.
        Note: This method is currently not used in the main worker loop but kept for potential future use.

        Args:
            candidate_queries: List of queries to test
            metric_type: Type of metric we're testing for (used for scoring context)

        Returns:
            The best query string, or None if none were suitable
        """
        if not candidate_queries:
            return None

        # Set a timeout for the entire ranking process
        ranking_start = datetime.utcnow()
        ranking_timeout = 10  # seconds
        query_scores = []

        logger.debug(f"Testing and ranking {len(candidate_queries)} candidate queries for {metric_type}...")

        for query in candidate_queries:
            # Check timeout
            if (datetime.utcnow() - ranking_start).total_seconds() > ranking_timeout:
                logger.warning(
                    f"Query ranking timed out after {ranking_timeout} seconds."
                )
                break

            query_lower = query.lower() # FIX: Define query_lower here
            score = 0
            logger.debug(f"Testing query: {query[:80]}...")
            validation_result = await prometheus_scraper.validate_query(query)

            if not validation_result["valid"]:
                logger.debug(f"  Invalid syntax. Score: 0")
                continue  # Skip invalid queries

            # Fetch sample data to evaluate query quality (just one sample)
            try:
                 sample_data = await prometheus_scraper.fetch_metric_data_for_label_check(
                    query, limit=1
                 )
            except Exception as fetch_err:
                 logger.debug(f"  Error fetching sample data: {fetch_err}. Score: 0")
                 continue # Skip if fetch fails

            if not sample_data:
                logger.debug(f"  Returns no data. Score: 0")
                continue  # Skip queries that return no data

            # Use the existing scoring logic
            score = self._score_query(query, sample_data, metric_type)

            # Add to list of candidates with scores
            query_scores.append((query, score))
            logger.debug(f"  Query '{query[:50]}...' scored {score}")

        # Sort by score descending
        query_scores.sort(key=lambda x: x[1], reverse=True)

        # Return the highest scoring query if we have any with a positive score
        if query_scores and query_scores[0][1] > 0:
            best_query, best_score = query_scores[0]
            logger.info(
                f"Selected best query for {metric_type}: '{best_query}' (score: {best_score})"
            )
            return best_query
        else:
             logger.warning(f"No suitable query found for {metric_type} after testing {len(candidate_queries)} candidates.")
             return None

    async def format_metrics_for_ai(self, entity_id, entity_type, namespace, current_metrics, historical_metrics=None):
        """
        Format metrics in a structured way for AI analysis with clear context.

        Args:
            entity_id: ID of the entity (pod, node)
            entity_type: Type of the entity
            namespace: Kubernetes namespace
            current_metrics: Current metrics snapshot (list of dicts)
            historical_metrics: List of historical metrics snapshots (list of lists of dicts)

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

            # Helper to format a single snapshot
            def format_snapshot(snapshot):
                formatted_metrics = []
                if not isinstance(snapshot, list):
                     logger.warning(f"Invalid snapshot format encountered: {type(snapshot)}")
                     return []
                for metric in snapshot:
                    if not isinstance(metric, dict):
                         logger.warning(f"Invalid metric format in snapshot: {type(metric)}")
                         continue

                    metric_name = metric.get("metric", "unknown")
                    labels = metric.get("labels", {})
                    value_info = {"value": None, "timestamp": None}

                    # Extract value and timestamp (handle Prometheus format [ts, val_str])
                    prom_value = metric.get("value")
                    if isinstance(prom_value, (list, tuple)) and len(prom_value) == 2:
                        try:
                            # Value is the second element, attempt conversion to float
                            value_info["timestamp"] = datetime.fromtimestamp(float(prom_value[0])).isoformat()
                            value_info["value"] = prom_value[1] # Keep as string initially

                            # Attempt to convert to float if possible
                            try: value_info["value"] = float(value_info["value"])
                            except (ValueError, TypeError): pass
                        except (ValueError, TypeError, OSError) as ts_err:
                            logger.debug(f"Could not parse timestamp {prom_value[0]} for {metric_name}: {ts_err}")
                            value_info["timestamp"] = str(prom_value[0]) # Use raw timestamp string
                            value_info["value"] = prom_value[1]

                    # Fallback if 'value' is not in Prometheus format
                    elif prom_value is not None:
                        value_info["value"] = prom_value
                        value_info["timestamp"] = metric.get("timestamp") # Use snapshot timestamp if available

                    formatted_metrics.append({
                        "metric_name": metric_name,
                        "metric_type": metric.get("metric_type", "unknown"), # Include inferred type
                        "query": metric.get("query", ""), # Include query if available
                        "labels": labels,
                        "current_value": value_info["value"],
                        "timestamp": value_info["timestamp"]
                    })
                return formatted_metrics

            # Process current metrics (the anomalous ones)
            formatted_current = format_snapshot(current_metrics)

            # Format historical metrics if available (limit history)
            historical_data = []
            if historical_metrics and isinstance(historical_metrics, list):
                # Limit to last N historical snapshots (e.g., 10)
                history_limit = 10
                historical_subset = historical_metrics[-history_limit:]

                for idx, snapshot in enumerate(historical_subset):
                    snapshot_time_str = "unknown"
                    # Try to get a timestamp from the first metric in the snapshot
                    if snapshot and isinstance(snapshot, list) and snapshot[0] and isinstance(snapshot[0].get("value"), list) and len(snapshot[0]["value"]) == 2:
                         try: snapshot_time_str = datetime.fromtimestamp(float(snapshot[0]["value"][0])).isoformat()
                         except: pass

                    historical_data.append({
                        "snapshot_index": -(len(historical_subset) - idx), # e.g., -1 for most recent history, -10 for oldest
                        "timestamp_approx": snapshot_time_str,
                        "metrics": format_snapshot(snapshot)
                    })

            # Fetch additional K8s metadata if possible (best effort)
            k8s_metadata = {}
            if entity_type and entity_id:
                try:
                    k8s_metadata = await k8s_executor.get_resource_status(
                         resource_type=entity_type,
                         name=entity_id,
                         namespace=namespace
                    )
                except Exception as e:
                    logger.warning(f"Could not fetch K8s metadata for {namespace}/{entity_type}/{entity_id}: {e}")

            # Get latest anomaly score and prediction data if available
            entity_key = f"{entity_type}/{entity_id}"
            if namespace: entity_key = f"{namespace}/{entity_key}"
            latest_score = getattr(self, 'latest_anomaly_scores', {}).get(entity_key)
            latest_prediction = getattr(self, 'latest_predictions', {}).get(entity_key)


            # Bundle everything together, ensuring serialization
            return self._deep_serialize({
                "entity_info": entity_info,
                "current_metrics_snapshot": formatted_current,
                "historical_metrics_snapshots": historical_data,
                "kubernetes_metadata": k8s_metadata, # Include K8s state
                "latest_anomaly_score": latest_score,
                "latest_prediction_data": latest_prediction
            })

        except Exception as e:
            logger.error(f"Error formatting metrics for AI analysis ({entity_id}): {e}", exc_info=True)
            # Return at least basic info if something fails, ensure serializable
            return self._deep_serialize({
                "entity_info": {
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "namespace": namespace
                },
                "current_metrics_snapshot": current_metrics, # Pass raw on error
                "error": f"Failed to format metrics: {str(e)}"
            })

    async def _generate_cluster_overview(self, all_metrics):
        """Generate and log a summary of the current cluster state based on metrics and events."""
        # This method is currently a placeholder and not actively used.
        # Implementation would involve aggregating metrics, summarizing active events, etc.
        logger.debug("Cluster overview generation is currently not implemented.")
        return

    async def _log_startup_summary(self):
        """
        Generate and log a summary of the application and its configuration at startup.
        """
        try:
            logger.info("=" * 80)
            logger.info(" KUBEWISE AUTONOMOUS WORKER - STARTUP SUMMARY")
            logger.info("=" * 80)

            # Application info
            import platform
            from app import __version__ # Assuming version is defined in __init__.py

            logger.info(f"Timestamp: {datetime.utcnow().isoformat()} UTC")
            logger.info(f"Version: {__version__ if '__version__' in locals() else 'Unknown'}")
            logger.info(f"OS: {platform.system()} {platform.release()}")
            logger.info(f"Python: {platform.python_version()}")

            # Core Configuration
            logger.info("-" * 30 + " Configuration " + "-" * 30)
            logger.info(f"Mode: {mode_service.get_mode()}")
            logger.info(f"Log Level: {settings.LOG_LEVEL}")
            logger.info(f"Worker Interval: {settings.WORKER_SLEEP_INTERVAL_SECONDS}s")
            logger.info(f"Prometheus URL: {settings.PROMETHEUS_URL}")
            logger.info(f"Prometheus Scrape Interval: {settings.PROMETHEUS_SCRAPE_INTERVAL_SECONDS}s")
            logger.info(f"Anomaly Metric Window: {settings.ANOMALY_METRIC_WINDOW_SECONDS}s")
            logger.info(f"History Limit (Snapshots): {self.history_limit}")
            logger.info(f"Stale History Timeout: {self.stale_entity_timeout_seconds}s")

            # Anomaly Detection
            logger.info("-" * 30 + " Anomaly Detection " + "-" * 26)
            model_loaded = hasattr(anomaly_detector, 'model') and anomaly_detector.model is not None
            scaler_loaded = hasattr(anomaly_detector, 'scaler') and anomaly_detector.scaler is not None
            logger.info(f"Model Loaded: {model_loaded}")
            logger.info(f"Scaler Loaded: {scaler_loaded}")
            logger.info(f"Detection Threshold: {settings.AUTOENCODER_THRESHOLD}")
            logger.info(f"Feature Metrics Count: {len(getattr(settings, 'FEATURE_METRICS', []))}")

            # AI Integration (Gemini)
            logger.info("-" * 30 + " AI Assistance (Gemini) " + "-" * 23)
            gemini_enabled = self.gemini_service is not None
            api_key_missing = self.gemini_service.is_api_key_missing() if gemini_enabled else True
            logger.info(f"Gemini Service Enabled: {gemini_enabled}")
            logger.info(f"API Key Present: {not api_key_missing}")
            if gemini_enabled and not api_key_missing:
                 logger.info(f"Auto Analysis Enabled: {settings.GEMINI_AUTO_ANALYSIS}")
                 logger.info(f"Auto Verification Enabled: {settings.GEMINI_AUTO_VERIFICATION}")
            elif gemini_enabled and api_key_missing:
                 logger.warning("Gemini enabled, but API key is missing or invalid.")

            # Remediation Settings
            logger.info("-" * 30 + " Remediation Settings " + "-" * 25)
            verification_delay = getattr(settings, "REMEDIATION_VERIFICATION_DELAY_SECONDS", 60)
            logger.info(f"Verification Delay: {verification_delay}s")

            logger.info("=" * 80)

        except Exception as e:
            logger.error(f"Error generating startup summary: {e}", exc_info=True)

    def _extract_metric_value(self, metric: Dict[str, Any]) -> Optional[float]:
        """
        Helper method to extract the latest numeric value from a Prometheus metric dictionary.
        Handles vector format [timestamp, value_string].

        Args:
            metric: The metric dictionary (result from Prometheus query)

        Returns:
            The numeric value as float, or None if extraction fails.
        """
        if not isinstance(metric, dict):
            return None

        value_data = metric.get("value") # Prometheus instant query format
        if value_data and isinstance(value_data, (list, tuple)) and len(value_data) == 2:
            try:
                # Value is the second element, attempt conversion to float
                return float(value_data[1])
            except (ValueError, TypeError):
                logger.debug(f"Could not convert metric value '{value_data[1]}' to float for {metric.get('metric', {}).get('__name__')}")
                return None

        # Handle range query format ('values' key with list of [ts, val]) - get latest
        values_data = metric.get("values")
        if values_data and isinstance(values_data, list) and len(values_data) > 0:
             latest_point = values_data[-1]
             if isinstance(latest_point, (list, tuple)) and len(latest_point) == 2:
                  try:
                       return float(latest_point[1])
                  except (ValueError, TypeError):
                       logger.debug(f"Could not convert latest range value '{latest_point[1]}' to float for {metric.get('metric', {}).get('__name__')}")
                       return None

        # Fallback for non-standard formats or direct value (less common)
        raw_value = metric.get("raw_value") # Check if this custom key exists
        if raw_value is not None:
             try: return float(raw_value)
             except (ValueError, TypeError): return None

        sample_value = metric.get("sample_value") # Check if this custom key exists
        if sample_value is not None:
             try: return float(sample_value)
             except (ValueError, TypeError): return None

        return None # No extractable numeric value found

    def _determine_anomaly_type(self, metrics: List[Dict[str, Any]]) -> str:
        """
        Determine a potential root cause category based on metric values.
        This is a heuristic approach.

        Args:
            metrics: List of metric dictionaries for the entity

        Returns:
            str: A short description of the likely anomaly type (e.g., "high_cpu")
        """
        # Define thresholds (adjust as needed)
        thresholds = {
            "cpu_usage": 0.80,  # 80% CPU utilization (assuming metric represents ratio 0-1)
            "memory_usage": 0.85, # 85% Memory utilization (assuming ratio or requires limit context)
            "restarts": 2, # More than 2 restarts (assuming a rate or recent count)
            "network_io": 50 * 1024 * 1024, # 50 MB/s (assuming bytes/sec)
            # Add thresholds for disk, latency, error rates etc. if available
        }
        # Track dominant issue
        dominant_issue = "unknown"
        max_severity = -1 # Track how far past threshold

        try:
            for metric in metrics:
                metric_type = metric.get("metric_type", "unknown")
                value = self._extract_metric_value(metric) # Use helper to get numeric value

                if value is None: continue # Skip metrics without numeric value

                severity = -1 # Reset severity for this metric

                if "cpu_usage" in metric_type:
                    if value > thresholds["cpu_usage"]:
                         severity = value / thresholds["cpu_usage"]
                         if severity > max_severity:
                              max_severity = severity
                              dominant_issue = "high_cpu"
                elif "memory" in metric_type: # Needs context (limits) for percentage
                     # Placeholder: If value is large (e.g., > 1 GiB), flag it. Needs improvement.
                     # A better check would be memory_usage_bytes / memory_limit_bytes
                     # Assuming value is ratio for now:
                    if value > thresholds["memory_usage"]:
                         severity = value / thresholds["memory_usage"]
                         if severity > max_severity:
                              max_severity = severity
                              dominant_issue = "high_memory"
                elif "restart" in metric_type:
                     if value > thresholds["restarts"]:
                         severity = value / thresholds["restarts"] # Relative severity
                         if severity > max_severity:
                              max_severity = severity
                              dominant_issue = "high_restarts"
                elif "network_receive" in metric_type or "network_transmit" in metric_type:
                    if value > thresholds["network_io"]:
                        severity = value / thresholds["network_io"]
                        if severity > max_severity:
                            max_severity = severity
                            dominant_issue = "high_network"

                # Add checks for other metric types (disk, latency, errors) here

            # If no specific threshold was breached, but anomaly score is high,
            # it might be a complex issue or data quality problem.
            if dominant_issue == "unknown":
                 # Check for specific failure states if available in metrics
                 for metric in metrics:
                     if "kube_pod_container_status_waiting_reason" in metric.get("metric",""):
                         if self._extract_metric_value(metric) == 1: # If waiting reason is active
                             reason = metric.get("labels", {}).get("reason", "")
                             if reason: return f"container_waiting_{reason.lower()}"
                     if "kube_pod_status_phase" in metric.get("metric",""):
                         phase = metric.get("labels", {}).get("phase", "")
                         if phase in ["Failed", "Unknown"] and self._extract_metric_value(metric) == 1:
                             return f"pod_phase_{phase.lower()}"

            return dominant_issue

        except Exception as e:
            logger.debug(f"Error determining anomaly type: {e}")
            return "determination_error"

    def _create_safe_remediation_action(
        self,
        action_type: str,
        resource_type: str,
        resource_name: str,
        namespace: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        confidence: float = 0.5
    ) -> RemediationAction:
        """
        Creates a RemediationAction object, ensuring necessary parameters are present
        and the command is generated safely.

        Args:
            action_type: Type of action (e.g., 'describe', 'restart_pod'). Must match a template key.
            resource_type: Type of resource (e.g., 'pod', 'deployment').
            resource_name: Name of the resource.
            namespace: Namespace of the resource (optional, required for namespaced resources).
            parameters: Additional parameters for the command template (optional).
            confidence: Confidence score for this action (0.0-1.0).

        Returns:
            A safely constructed RemediationAction object.
        """
        if parameters is None:
            parameters = {}

        # Ensure essential parameters for the specific action type are included
        # These often overlap with resource_type/name/namespace but might need explicit keys
        if action_type in k8s_executor.command_templates:
            template = k8s_executor.command_templates[action_type]
            # Add required keys if not present in parameters, using provided args
            if "{name}" in template and "name" not in parameters:
                parameters["name"] = resource_name
            if "{pod_name}" in template and "pod_name" not in parameters:
                 parameters["pod_name"] = resource_name # Assuming name is pod_name for pod actions
            if "{deployment_name}" in template and "deployment_name" not in parameters:
                parameters["deployment_name"] = resource_name
            if "{node_name}" in template and "node_name" not in parameters:
                 parameters["node_name"] = resource_name
            # Add namespace if required by template and provided
            if ("{namespace}" in template or "-n {namespace}" in template or "--namespace {namespace}" in template) and "namespace" not in parameters and namespace:
                parameters["namespace"] = namespace
            # Add resource type if needed
            if "{resource_type}" in template and "resource_type" not in parameters:
                parameters["resource_type"] = resource_type

            # Generate the command using the template and parameters
            command = k8s_executor.format_command_from_template(action_type, parameters)

        else:
            logger.warning(f"Action type '{action_type}' not found in command templates. Cannot generate command.")
            command = f"# Error: Unknown action_type {action_type}"
            confidence = 0.0

        # Create the RemediationAction object
        return RemediationAction(
            action_type=action_type,
            resource_type=resource_type,
            resource_name=resource_name,
            namespace=namespace,
            parameters=parameters,
            confidence=confidence,
            command=command  # Ensure command is always set, even if it's an error message
        )
