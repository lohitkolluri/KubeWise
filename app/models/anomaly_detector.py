import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np # Keep numpy import first
from loguru import logger

from app.core.config import settings
from app.services.gemini_service import GeminiService

# TensorFlow is optional, used for the autoencoder model
try:
    # Set TensorFlow logging level BEFORE importing
    import os
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'  # 0=all, 1=no INFO, 2=no WARNING, 3=no ERROR

    # Check for Apple Silicon - TensorFlow might have different import paths
    import platform
    is_apple_silicon = platform.system() == 'Darwin' and platform.machine().startswith('arm')

    # Try importing TensorFlow first
    import tensorflow as tf
    logger.info(f"TensorFlow successfully imported. Version: {tf.__version__}")

    # Then try to import Keras from TensorFlow or standalone based on what's available
    try:
        # First try the standard path
        from tensorflow import keras
        from tensorflow.keras.models import load_model
        logger.info("Using Keras from tensorflow.keras")
    except ImportError:
        # If that fails, try standalone Keras (which TF 2.16.1+ might use)
        import keras
        from keras.models import load_model
        logger.info("Using standalone Keras installation")

    # Verify TensorFlow loaded properly
    physical_devices = tf.config.list_physical_devices()
    logger.info(f"Available TensorFlow devices: {physical_devices}")

    TF_AVAILABLE = True

except ImportError as e:
    logger.warning(f"Failed to import TensorFlow or its components: {e}. Running with limited ML functionality.")
    tf = None
    load_model = None  # Ensure load_model is None if tf fails
    TF_AVAILABLE = False
except Exception as e:
    logger.warning(f"Error initializing TensorFlow: {e}. Running with limited ML functionality.")
    tf = None
    load_model = None  # Ensure load_model is None if tf fails
    TF_AVAILABLE = False


class AnomalyDetector:
    """
    Anomaly detection using a Keras Autoencoder model for unsupervised anomaly detection.

    This class handles the detection of anomalies in Kubernetes metrics using a deep learning approach.
    It provides methods for making predictions and evaluating model performance based on reconstruction error.
    """

    def __init__(self):
        """Initialize the anomaly detector with the pre-trained autoencoder model."""
        # Paths to model and scaler
        self.model_path = settings.AUTOENCODER_MODEL_PATH
        self.scaler_path = settings.AUTOENCODER_SCALER_PATH
        self.threshold = settings.AUTOENCODER_THRESHOLD

        # Initialize with None, will be loaded in _load_model_and_scaler
        self.model = None
        self.scaler = None

        # Define features that the model was trained on - EXACTLY MATCHING TRAINING DATA
        self.feature_columns = [
            "cpu_usage_seconds_total",  # Rate
            "memory_working_set_bytes",  # Absolute value
            "network_receive_bytes_total",  # Rate
            "network_transmit_bytes_total",  # Rate
            "pod_status_phase",  # Categorical/Binary (Running=1, else=0)
            "container_restarts_rate",  # Rate of container restarts
        ]
        self.num_features_per_metric = 5  # Each metric has 5 engineered features
        self.total_features = len(self.feature_columns) * self.num_features_per_metric

        # Load model and scaler
        self._load_model_and_scaler()

        # Autoencoders typically don't require frequent retraining like IF might
        self.needs_retraining = False

        # Initialize Gemini service for potential analysis
        self.gemini_service = GeminiService(
            api_key=settings.GEMINI_API_KEY, model_type=settings.GEMINI_MODEL
        )

    def _load_model_and_scaler(self):
        """
        Load the trained Keras model and scaler.
        """
        # First check if TensorFlow is available
        if not TF_AVAILABLE or load_model is None: # Check load_model too
            logger.warning(
                "TensorFlow is not available or failed to import. Cannot load Keras model."
            )
            self.model = None
            self.scaler = None
            return

        # Ensure the paths are absolute for reliable file checks
        model_path = os.path.abspath(self.model_path)
        scaler_path = os.path.abspath(self.scaler_path)

        logger.info(f"AUTOENCODER_MODEL_PATH: {model_path}")
        logger.info(f"AUTOENCODER_SCALER_PATH: {scaler_path}")

        # Check if model path exists and try alternative formats if needed
        if not os.path.exists(model_path):
            logger.warning(f"Model file not found at: {model_path}")

            # Try alternative format (.keras if .h5 was specified or vice versa)
            alt_path = None
            if model_path.endswith('.h5'):
                alt_path = model_path.replace('.h5', '.keras')
            elif model_path.endswith('.keras'):
                alt_path = model_path.replace('.keras', '.h5')

            if alt_path and os.path.exists(alt_path):
                logger.info(f"Found alternative model format at: {alt_path}")
                model_path = alt_path
            else:
                logger.error("No valid model file found in any format (.h5 or .keras)")
                self.model = None
                # Continue to try loading the scaler

        # Now try to load the model using the appropriate path (if found)
        if self.model is None and os.path.exists(model_path): # Only load if not already failed
            try:
                logger.info(f"Attempting to load Autoencoder model from: {model_path}")
                # Use the imported load_model (from tensorflow.keras)
                self.model = load_model(model_path)
                logger.info(f"Autoencoder model loaded successfully from {model_path}")

                # Verify model was loaded correctly and log structure
                if self.model is None:
                    logger.error("Model loaded as None despite no exceptions")
                else:
                    try:
                        summary_lines = []
                        self.model.summary(print_fn=lambda x: summary_lines.append(x))
                        logger.debug(f"Model summary: {summary_lines[0]} (and {len(summary_lines)-1} more lines)")
                        logger.info(f"Model input shape: {self.model.input_shape}")
                        logger.info(f"Model output shape: {self.model.output_shape}")
                    except Exception as e:
                        logger.warning(f"Could not log model summary: {e}")

            except Exception as e:
                logger.error(f"Failed to load Autoencoder model: {e}", exc_info=True)
                self.model = None

        # Load scaler with similar checks
        if not os.path.exists(scaler_path):
            logger.error(f"Scaler file not found at: {scaler_path}")
            self.scaler = None
            # Don't return here, log overall status below
        else:
            try:
                logger.info(f"Attempting to load scaler from: {scaler_path}")
                self.scaler = joblib.load(scaler_path)
                logger.info("Scaler loaded successfully")

                # Log basic scaler info
                if hasattr(self.scaler, 'n_features_in_'):
                    logger.info(f"Scaler was fitted on {self.scaler.n_features_in_} features")
                if hasattr(self.scaler, 'scale_'):
                    logger.info(f"Scaler has valid scale_ attribute of shape {self.scaler.scale_.shape}")

            except Exception as e:
                logger.error(f"Failed to load scaler: {e}", exc_info=True)
                self.scaler = None

        # Log overall status
        if self.model is not None and self.scaler is not None:
            logger.success("Both model and scaler successfully loaded. ML-based detection is ready.")
        else:
            logger.warning("Failed to load model and/or scaler. Will rely on fallback detection.")
            if self.model is None:
                logger.warning("- Autoencoder model could not be loaded.")
            if self.scaler is None:
                logger.warning("- Scaler could not be loaded.")

    async def _get_alternative_metrics_for_pod(
        self, pod_name: str, namespace: str
    ) -> Optional[Dict[str, List[float]]]:
        """
        When container-level metrics aren't available, try to find alternative metrics
        for a specific pod by checking other available metrics in Prometheus.

        Args:
            pod_name: The name of the pod to find metrics for
            namespace: The namespace the pod is in

        Returns:
            Optional dict of metrics with their values, or None if alternatives can't be found
        """
        try:
            from app.utils.prometheus_scraper import prometheus_scraper

            # Define a set of alternative metrics that might be available
            alternative_metrics = {
                # For CPU - try process metrics, node metrics, or app-specific metrics
                "cpu_usage_seconds_total": [
                    f'process_cpu_seconds_total{{pod="{pod_name}"}}',
                    f'process_cpu_seconds_total{{instance=~".*{pod_name}.*"}}',
                    'node_cpu_seconds_total{mode="user"}',  # Node-level fallback
                    "rate(process_cpu_seconds_total[5m])",  # General process CPU usage
                ],
                # For memory - try process metrics, node metrics
                "memory_working_set_bytes": [
                    f'process_resident_memory_bytes{{pod="{pod_name}"}}',
                    f'process_resident_memory_bytes{{instance=~".*{pod_name}.*"}}',
                    f'go_memstats_alloc_bytes{{instance=~".*{pod_name}.*"}}',  # For Go applications
                    "node_memory_MemAvailable_bytes",  # Node-level fallback
                ],
                # For network receive - try node network metrics
                "network_receive_bytes_total": [
                    'node_network_receive_bytes_total{device="eth0"}',
                    f'net_conntrack_dialer_conn_attempted_total_total{{instance=~".*{pod_name}.*"}}',
                    f'http_request_size_bytes_sum{{instance=~".*{pod_name}.*"}}',  # HTTP-specific
                ],
                # For network transmit - try node network metrics
                "network_transmit_bytes_total": [
                    'node_network_transmit_bytes_total{device="eth0"}',
                    f'net_conntrack_dialer_conn_close_total{{instance=~".*{pod_name}.*"}}',
                    f'http_response_size_bytes_sum{{instance=~".*{pod_name}.*"}}',  # HTTP-specific
                ],
                # For pod status - try kube-state-metrics
                "pod_status_phase": [
                    f'kube_pod_status_phase{{pod="{pod_name}", phase="Running"}}',
                    f'kube_pod_status_phase{{pod="{pod_name}"}}',
                ],
                # For container restarts - try kube-state-metrics
                "container_restarts_rate": [
                    f'rate(kube_pod_container_status_restarts_total{{pod="{pod_name}"}}[5m])',
                    "rate(kube_pod_container_status_restarts_total[5m])",
                ],
            }

            # Try to get values for each metric type
            results = {}
            for metric_type, queries in alternative_metrics.items():
                for query in queries:
                    try:
                        # Query Prometheus
                        data = (
                            await prometheus_scraper.fetch_metric_data_for_label_check(
                                query, limit=5
                            )
                        )

                        if data and len(data) > 0:
                            # Extract values
                            values = []
                            for item in data:
                                value = item.get("value")
                                if isinstance(value, (int, float)):
                                    values.append(float(value))
                                elif isinstance(value, list) and len(value) == 2:
                                    try:
                                        values.append(float(value[1]))
                                    except (ValueError, TypeError):
                                        continue

                            if values:
                                results[metric_type] = values
                                logger.info(
                                    f"Found alternative metrics for {pod_name} using {query}"
                                )
                                break
                    except Exception as e:
                        logger.debug(f"Error getting alternative metric {query}: {e}")

            # If we found any alternatives, return them
            if results:
                logger.info(
                    f"Found {len(results)} alternative metrics for pod {pod_name}"
                )
                return results

            return None
        except Exception as e:
            logger.error(
                "Error getting alternative metrics for pod {}: {}", pod_name, e
            )
            return None

    async def _engineer_features_with_history(
        self,
        current_snapshot: List[Dict[str, Any]],
        historical_snapshots: Optional[List[List[Dict[str, Any]]]] = None
    ) -> Optional[np.ndarray]:
        """
        Engineers features from current snapshot plus historical data when available.
        This provides more accurate time-series features aligned with model training data.

        Args:
            current_snapshot: List of current metric dictionaries from Prometheus
            historical_snapshots: Optional list of previous metric snapshots for this entity

        Returns:
            Optional[np.ndarray]: Feature vector or None if engineering fails
        """
        try:
            # Basic validation
            if not current_snapshot:
                logger.warning("Empty snapshot provided to feature engineering")
                return None

            # First identify the entity
            entity_id = "unknown"
            entity_type = "unknown"
            namespace = None

            # Extract entity info
            for metric_entry in current_snapshot:
                metric_data = metric_entry.get("labels", {})
                if "pod" in metric_data and "namespace" in metric_data:
                    entity_id = metric_data["pod"]
                    entity_type = "pod"
                    namespace = metric_data["namespace"]
                    break
                elif "node" in metric_data:
                    entity_id = metric_data["node"]
                    entity_type = "node"
                    break

            # Skip processing for unknown entities
            if entity_id == "unknown" or entity_type == "unknown":
                logger.debug(f"Skipping feature engineering for unknown entity")
                return None

            logger.debug(f"Engineering features for {entity_type}/{entity_id}")

            # Initialize metric collection structures
            time_series_by_metric = {
                metric_type: [] for metric_type in self.feature_columns
            }

            # Process current snapshot first - log what's available
            logger.debug(f"Processing metrics snapshot with {len(current_snapshot)} entries for {entity_id}")

            # DEBUG: Log the raw metrics received for the entity
            logger.debug(f"===== INPUT METRICS for {entity_id} =====")
            for i, entry in enumerate(current_snapshot[:3]):  # Log first 3 for brevity
                logger.debug(f"Metric {i+1}: {entry.get('metric_type', 'unknown type')}")
                logger.debug(f"  Query: {entry.get('query', '')[:100]}")
                logger.debug(f"  Metric name: {entry.get('metric', '')}")
                logger.debug(f"  Has metric_type: {'Yes' if 'metric_type' in entry else 'No'}")
                if 'value' in entry:
                    logger.debug(f"  Has value: Yes - {entry.get('value')}")
                if 'values' in entry and entry.get('values'):
                    logger.debug(f"  Has values array: Yes, length={len(entry.get('values', []))}")
                logger.debug(f"  Labels: {entry.get('labels', {})}")
            logger.debug("===== END INPUT METRICS SAMPLE =====")

            # Extract metrics using our generalized method
            self._extract_metrics_from_snapshot(current_snapshot, time_series_by_metric)

            # Process historical snapshots if available
            if historical_snapshots:
                for snapshot in historical_snapshots:
                    self._extract_metrics_from_snapshot(snapshot, time_series_by_metric)

            # Log what metrics were successfully extracted
            logger.debug(f"===== EXTRACTED METRICS for {entity_id} =====")
            for metric_type, values in time_series_by_metric.items():
                logger.debug(f"Metric {metric_type}: {len(values)} values extracted")
                if values and len(values) > 0:
                    logger.debug(f"  Sample values: {values[:2]}")
            logger.debug("===== END EXTRACTED METRICS =====")

            # Validate if all required metrics are present
            missing_metrics = []
            for metric_type in self.feature_columns:
                if not time_series_by_metric[metric_type]:
                    missing_metrics.append(metric_type)

            # Try to fetch missing metrics using standard queries
            if missing_metrics:
                logger.info(f"Attempting to fetch missing metrics using standard queries: {missing_metrics}")
                try:
                    # Import the centralized queries module
                    from app.core.queries import STANDARD_QUERIES
                    from app.utils.prometheus_scraper import prometheus_scraper

                    # For each missing metric, use the standard query
                    for metric in missing_metrics.copy():  # Use copy to safely modify while iterating
                        # Get the standard query for this metric
                        query = STANDARD_QUERIES.get(metric)

                        if not query:
                            logger.warning(f"No standard query defined for metric: {metric}")
                            continue

                        # Execute the standard query
                        result = await prometheus_scraper.fetch_metric_data_for_label_check(query)

                        if result and len(result) > 0:
                            # Extract values from the result
                            values = []
                            for item in result:
                                value = item.get("value")
                                if isinstance(value, (int, float)):
                                    values.append(float(value))
                                elif isinstance(value, str) and value.replace(".", "", 1).isdigit():
                                    values.append(float(value))
                                elif isinstance(value, list) and len(value) > 1:
                                    try:
                                        if isinstance(value[1], (int, float)):
                                            values.append(float(value[1]))
                                        elif isinstance(value[1], str) and value[1].replace(".", "", 1).isdigit():
                                            values.append(float(value[1]))
                                    except (ValueError, TypeError):
                                        continue

                            # Store the values if we found any
                            if values:
                                time_series_by_metric[metric] = values

                                logger.info(f"✅ Successfully fetched {len(values)} values for {metric} via standard query")
                                missing_metrics.remove(metric)
                            else:
                                logger.warning(f"No valid values found in standard query for {metric}")
                        else:
                            logger.warning(f"Standard query returned no results for {metric}")
                except ImportError as e:
                    logger.error(f"Error importing standard queries: {e}")
                except Exception as e:
                    logger.error(f"Error using standard queries: {e}")

            # After all attempts, check if we still have missing metrics
            if missing_metrics:
                # Calculate percentage of missing metrics
                missing_ratio = len(missing_metrics) / len(self.feature_columns)
                if missing_ratio > 0.5:  # If more than 50% of metrics are missing
                    logger.error(f"Critical metrics missing for {entity_id}: {missing_metrics} ({missing_ratio:.1%} of required metrics)")
                    logger.error(f"Cannot perform reliable prediction without these metrics")
                    return None
                else:
                    # Log warning but continue with zeros for the missing metrics
                    logger.warning(f"Some metrics missing for {entity_id}: {missing_metrics}, using zeros (may reduce accuracy)")

            # Engineer features from time series data
            feature_vector = []

            # Generate the standard 5 features per metric type
            for metric_type in self.feature_columns:
                values = time_series_by_metric[metric_type]

                if not values:
                    # No data for this metric, use zeros
                    feature_vector.extend([0.0, 0.0, 0.0, 0.0, 0.0])
                    logger.debug(f"No data for {metric_type}, using zeros")
                    continue

                # Convert to numpy array for vector operations
                values_np = np.array(values)

                # 1. Mean
                feature_vector.append(np.mean(values_np))

                # 2. Max
                feature_vector.append(np.max(values_np))

                # 3. Min
                feature_vector.append(np.min(values_np))

                # 4. Standard deviation (use 0.0 if only one value)
                feature_vector.append(np.std(values_np) if len(values_np) > 1 else 0.0)

                # 5. Trend slope - calculated using linear regression
                if len(values_np) > 1:
                    try:
                        # Use polyfit for linear regression trend calculation
                        x = np.arange(len(values_np))
                        coeffs = np.polyfit(x, values_np, 1)
                        feature_vector.append(coeffs[0])  # The first coefficient is the slope
                    except Exception as e:
                        logger.warning(f"Error calculating trend for {metric_type}: {e}")
                        feature_vector.append(0.0)
                else:
                    feature_vector.append(0.0)

                # Log feature details for debugging
                idx = len(feature_vector) - 5
                logger.debug(
                    f"Features for {metric_type}: mean={feature_vector[idx]:.4f}, "
                    f"max={feature_vector[idx+1]:.4f}, min={feature_vector[idx+2]:.4f}, "
                    f"std={feature_vector[idx+3]:.4f}, slope={feature_vector[idx+4]:.4f}"
                )

            # Handle NaN or Inf values
            feature_vector = np.nan_to_num(
                feature_vector, nan=0.0, posinf=1e9, neginf=-1e9
            )

            # Ensure feature vector has exactly 30 dimensions (6 metrics × 5 features each)
            if len(feature_vector) != 30:
                logger.warning(f"Feature vector length mismatch! Expected 30, got {len(feature_vector)}")

                # Pad with zeros or truncate if needed
                if len(feature_vector) < 30:
                    feature_vector = np.pad(feature_vector, (0, 30 - len(feature_vector)))
                else:
                    feature_vector = feature_vector[:30]

            # Convert to numpy array and reshape for model input
            feature_vector = np.array(feature_vector, dtype=np.float64).reshape(1, -1)
            logger.debug(f"Feature vector shape: {feature_vector.shape}")
            return feature_vector

        except Exception as e:
            logger.error(f"Error during feature engineering with history: {e}", exc_info=True)
            return None

    def _extract_metrics_from_snapshot(
        self,
        snapshot: List[Dict[str, Any]],
        time_series_by_metric: Dict[str, List[float]]
    ):
        """
        Extract metrics from a Prometheus snapshot and store in the time series structure.

        Args:
            snapshot: List of metric dictionaries from Prometheus
            time_series_by_metric: Dictionary to store extracted metric values by type
        """
        for metric_data in snapshot:
            metric = metric_data.get("metric", "")
            query = metric_data.get("query", "")
            metric_type = metric_data.get("metric_type", "")

            # Skip entries with no metric name or query
            if not metric and not query:
                continue

            # Extract all numeric values
            numeric_values = []
            value = metric_data.get("value")
            values = metric_data.get("values", [])

            # Handle single value case
            if value is not None:
                if isinstance(value, list) and len(value) == 2:
                    try:
                        if isinstance(value[1], (int, float)):
                            numeric_values.append(float(value[1]))
                        elif (
                            isinstance(value[1], str)
                            and value[1].replace(".", "", 1).replace("e-", "", 1).isdigit()
                        ):
                            numeric_values.append(float(value[1]))
                    except (ValueError, TypeError):
                        continue
                elif isinstance(value, (int, float)):
                    numeric_values.append(float(value))

            # Handle multiple values case
            for v in values:
                if isinstance(v, list) and len(v) == 2:
                    try:
                        if isinstance(v[1], (int, float)):
                            numeric_values.append(float(v[1]))
                        elif (
                            isinstance(v[1], str)
                            and v[1].replace(".", "", 1).replace("e-", "", 1).isdigit()
                        ):
                            numeric_values.append(float(v[1]))
                    except (ValueError, TypeError):
                        continue
                elif isinstance(v, (int, float)):
                    numeric_values.append(float(v))

            if not numeric_values:
                continue

            # IMPROVED METRIC MATCHING LOGIC - Check both the query string, metric name, and metric_type
            # This handles how data comes from both the test script and Prometheus API

            # First check if metric_type gives us a clear match
            if metric_type:
                # Handle disk metrics specifically
                if metric_type in ["disk_usage_bytes", "filesystem_available", "filesystem_size", "disk_writes_bytes", "disk_reads_bytes", "disk_metric"]:
                    # Check if we need to normalize filesystem metrics (calculate usage percentage)
                    if metric_type in ["filesystem_available", "filesystem_size"] and "node_filesystem" in metric:
                        # Try to get corresponding available/size metric to calculate usage %
                        # This is a simplification - in a full implementation, we'd match the exact instance
                        logger.debug(f"Found filesystem metric: {metric_type}")
                    elif metric_type == "disk_usage_bytes":
                        logger.debug(f"Found disk usage metric: {metric}")

                    # For now, just capture the raw values - could be enhanced to calculate usage %
                    if any(feature.startswith("disk_") for feature in self.feature_columns):
                        # If we have a disk feature in the model, use that
                        disk_features = [f for f in self.feature_columns if f.startswith("disk_")]
                        if disk_features:
                            time_series_by_metric[disk_features[0]].extend(numeric_values)
                            logger.debug(f"Mapped disk metric to {disk_features[0]}")
                else:
                    # For other standard metrics, try to map directly
                    for feature in self.feature_columns:
                        if feature in metric_type:
                            time_series_by_metric[feature].extend(numeric_values)
                            logger.debug(f"Mapped directly from metric_type: {metric_type} to {feature}")
                            break

            # CPU Usage
            elif "container_cpu_usage_seconds_total" in query or "cpu_usage_seconds_total" in metric:
                time_series_by_metric["cpu_usage_seconds_total"].extend(numeric_values)
                logger.debug(f"Mapped CPU metric from query/metric: {query or metric}")

            # Memory Usage
            elif "container_memory_working_set_bytes" in query or "memory_working_set_bytes" in metric:
                time_series_by_metric["memory_working_set_bytes"].extend(numeric_values)
                logger.debug(f"Mapped memory metric from query/metric: {query or metric}")

            # Network Receive
            elif "container_network_receive_bytes_total" in query or "network_receive_bytes_total" in metric:
                time_series_by_metric["network_receive_bytes_total"].extend(numeric_values)
                logger.debug(f"Mapped network receive metric from query/metric: {query or metric}")

            # Network Transmit
            elif "container_network_transmit_bytes_total" in query or "network_transmit_bytes_total" in metric:
                time_series_by_metric["network_transmit_bytes_total"].extend(numeric_values)
                logger.debug(f"Mapped network transmit metric from query/metric: {query or metric}")

            # Pod Status
            elif "kube_pod_status_phase" in query or "pod_status_phase" in metric:
                # For pod status, we need special handling
                if "phase=\"Running\"" in query or metric_data.get("labels", {}).get("phase") == "Running":
                    phase_values = [1.0] * len(numeric_values)
                else:
                    phase_values = [0.0] * len(numeric_values)
                time_series_by_metric["pod_status_phase"].extend(phase_values)
                logger.debug(f"Mapped pod status metric from query/metric: {query or metric}")

            # Container Restarts
            elif "kube_pod_container_status_restarts_total" in query or "container_restarts_rate" in metric or "rate(kube_pod_container_status_restarts_total" in query:
                # Store fetched values for the metric
                time_series_by_metric["container_restarts_rate"].extend(numeric_values)
                logger.debug(f"Mapped container restarts metric from query/metric: {query or metric}")

            elif "node_filesystem_" in query or "node_filesystem_" in metric:
                # Handle filesystem metrics
                if any(feature.startswith("disk_") for feature in self.feature_columns):
                    disk_features = [f for f in self.feature_columns if f.startswith("disk_")]
                    if disk_features:
                        time_series_by_metric[disk_features[0]].extend(numeric_values)
                        logger.debug(f"Mapped filesystem metric to {disk_features[0]}")
                # Also log these for fallback detection
                logger.debug(f"Captured filesystem metric: {metric or query}")

            # Fall back to labeling system as a last resort
            else:
                for feature in self.feature_columns:
                    if feature in query or feature in metric or feature in str(metric_data.get("labels", {})):
                        time_series_by_metric[feature].extend(numeric_values)
                        logger.debug(f"Feature matched via fallback: {feature}")
                        break

    async def predict_with_forecast(
        self,
        latest_metrics_snapshot: List[Dict[str, Any]],
        historical_snapshots: Optional[List[List[Dict[str, Any]]]] = None
    ) -> Dict[str, Any]:
        """
        Predict anomalies with enhanced forecasting capabilities.
        Uses both the current snapshot and historical data when available.
        This consolidated method combines anomaly detection with future forecasting.

        Args:
            latest_metrics_snapshot: Latest metrics from Prometheus
            historical_snapshots: Optional historical snapshots for better feature engineering

        Returns:
            Dict containing anomaly prediction results and forecasting data including:
            - is_anomaly: Whether the current state is anomalous
            - anomaly_score: The anomaly score (higher = more anomalous)
            - is_critical: Whether the anomaly is critical
            - forecast_available: Whether forecasting data is available
            - recommendation: Recommended action based on anomaly and forecast
            - forecast: Detailed forecast data if available
            - predicted_failures: List of predicted failures with timing info
            - prediction_timestamp: When the prediction was made
        """
        # First check if TensorFlow and model are available
        if not TF_AVAILABLE or self.model is None or self.scaler is None:
            logger.warning("TensorFlow model unavailable. Using fallback detection.")
            is_anomaly, anomaly_score, is_critical = self._fallback_prediction(latest_metrics_snapshot)
            return {
                "is_anomaly": is_anomaly,
                "anomaly_score": anomaly_score,
                "is_critical": is_critical,
                "forecast_available": False,
                "recommendation": "monitor" if not is_anomaly else "investigate",
                "prediction_timestamp": datetime.utcnow().isoformat(),
            }

        try:
            # Determine entity ID for logging
            entity_id = next(
                (
                    m.get("labels", {}).get("pod", m.get("labels", {}).get("node", "unknown"))
                    for m in latest_metrics_snapshot if "labels" in m
                ),
                "unknown",
            )
            # Use the feature engineering function with historical data
            features = await self._engineer_features_with_history(
                latest_metrics_snapshot, historical_snapshots
            )

            if features is None:
                logger.warning("Feature engineering failed, using fallback detection.")
                is_anomaly, anomaly_score, is_critical = self._fallback_prediction(latest_metrics_snapshot)
                return {
                    "is_anomaly": is_anomaly,
                    "anomaly_score": anomaly_score,
                    "is_critical": is_critical,
                    "forecast_available": False,
                    "recommendation": "monitor" if not is_anomaly else "investigate",
                    "prediction_timestamp": datetime.utcnow().isoformat(),
                }

            # SAFEGUARD: Check how many metrics actually have values vs. zeros
            non_zero_metrics = np.count_nonzero(features)
            total_metrics = features.size
            metrics_present_ratio = non_zero_metrics / total_metrics if total_metrics > 0 else 0

            # If too many metrics are missing (zeros), use fallback detection
            # Lower threshold from 0.3 (30%) to 0.1 (10%) to be more forgiving with sparse metrics
            if metrics_present_ratio < 0.1:  # Less than 10% of metrics are actual values
                logger.warning(
                    f"Only {metrics_present_ratio:.2%} of metrics for {entity_id} are non-zero. "
                    "Using fallback detection to avoid false positives."
                )
                is_anomaly, anomaly_score, is_critical = self._fallback_prediction(latest_metrics_snapshot)
                return {
                    "is_anomaly": is_anomaly,
                    "anomaly_score": anomaly_score,
                    "is_critical": is_critical,
                    "forecast_available": False,
                    "recommendation": "monitor" if not is_anomaly else "investigate",
                    "prediction_timestamp": datetime.utcnow().isoformat(),
                }

            # Log all feature values for debugging - helps identify extreme values
            feature_vector = features.flatten()
            logger.debug(f"Raw feature vector for {entity_id}:")
            for i, val in enumerate(feature_vector):
                if abs(val) > 1000:  # Only log suspicious values to reduce noise
                    metric_idx = i // self.num_features_per_metric
                    feature_type = i % self.num_features_per_metric
                    feature_names = ["mean", "max", "min", "std", "slope"]
                    logger.debug(f"  Feature {i}: {self.feature_columns[metric_idx]}.{feature_names[feature_type]} = {val}")

            # NEW STEP: Log-transform features to compress extreme values
            try:
                normalized_features = np.sign(features) * np.log1p(np.abs(features))
            except Exception as e:
                logger.error(f"Error applying log transform for {entity_id}: {e}")
                normalized_features = features.copy()

            # Apply standard pre-scaling normalization
            try:
                normalized_features = self._apply_pre_scaling_normalization(normalized_features, entity_id)
            except Exception as e:
                logger.error(f"Error in pre-scaling normalization: {e}")
                # Continue with log-transformed features

            # Log normalized features for debugging
            logger.debug(f"Normalized feature vector (before scaler) for {entity_id}:")
            for i, val in enumerate(normalized_features.flatten()):
                if abs(val) > 10:  # Only log suspicious values after normalization
                    metric_idx = i // self.num_features_per_metric
                    feature_type = i % self.num_features_per_metric
                    feature_names = ["mean", "max", "min", "std", "slope"]
                    logger.debug(f"  Normalized: {self.feature_columns[metric_idx]}.{feature_names[feature_type]} = {val}")

            # Apply scaler transformation with safety checks
            try:
                scaled_features = self.scaler.transform(normalized_features)

                # NEW SAFETY CHECK: Check for extreme values after scaling
                max_val = np.max(np.abs(scaled_features))
                if max_val > 1000:
                    logger.warning(f"Extreme value after scaling: {max_val}. Applying emergency normalization.")
                    # Emergency normalization: Scale everything down by dividing by the max value
                    scale_factor = max_val / 10.0  # Aim to get values around 10 at most
                    scaled_features = scaled_features / scale_factor
                    # Set a minimum scale factor to prevent division by very small numbers
                    if scale_factor < 0.0001:
                        logger.warning("Scale factor too small, using minimum scale factor")
                        scaled_features = scaled_features * 0.0001
            except Exception as e:
                logger.error(f"Error during scaling: {e}. Using normalized features directly.")
                # If scaler fails, use our normalized features directly
                scaled_features = normalized_features

            # Replace NaN/Inf values if any appeared during scaling
            if np.isnan(scaled_features).any() or np.isinf(scaled_features).any():
                logger.warning("NaN or Inf values detected after scaling. Replacing with safe values.")
                scaled_features = np.nan_to_num(scaled_features, nan=0.0, posinf=10.0, neginf=-10.0)

            # Get reconstruction from model with safety checks
            try:
                reconstruction = self.model.predict(scaled_features, verbose=0)

                # Safety check on model output
                if np.isnan(reconstruction).any() or np.isinf(reconstruction).any():
                    logger.warning("Model produced NaN/Inf values. Replacing with zeros.")
                    reconstruction = np.nan_to_num(reconstruction, nan=0.0, posinf=10.0, neginf=-10.0)

                # Check for extreme values in reconstruction
                max_recon = np.max(np.abs(reconstruction))
                if max_recon > 1000:
                    logger.warning(f"Extreme value in reconstruction: {max_recon}. Normalizing.")
                    scale_factor = max_recon / 10.0
                    reconstruction = reconstruction / scale_factor
            except Exception as e:
                logger.error(f"Error during model prediction: {e}. Using zeros for reconstruction.")
                # If model fails, use zeros for reconstruction (will cause high MSE but at least it's controlled)
                reconstruction = np.zeros_like(scaled_features)

            # Calculate MSE safely - 3 layers of protection:
            try:
                # 1. Clip the difference to prevent extreme squared values
                diff = np.clip(scaled_features - reconstruction, -10.0, 10.0)

                # 2. Calculate mean square error with safety bounds
                squared_diff = np.power(diff, 2)

                # 3. Manual check for extreme values before taking mean
                if np.max(squared_diff) > 1000:
                    logger.warning(f"Extreme squared diff detected: {np.max(squared_diff)}. Limiting.")
                    squared_diff = np.clip(squared_diff, 0.0, 100.0)

                mse = float(np.mean(squared_diff))

                # Final safety check - if MSE is still extreme, force a reasonable value
                if mse > 100.0 or np.isnan(mse) or np.isinf(mse):
                    logger.warning(f"MSE is extreme after all safeguards: {mse}. Setting to 50.0")
                    mse = 50.0  # Use a high but reasonable value
            except Exception as e:
                logger.error(f"Error calculating MSE: {e}. Using fallback value.")
                mse = 20.0  # Fallback MSE value that will trigger anomaly but not be extreme

            # Determine if it's an anomaly based on the threshold
            is_anomaly = mse > self.threshold
            anomaly_score = float(mse)

            # Determine criticality based on how far above threshold
            is_critical = False
            if is_anomaly:
                threshold_ratio = mse / (self.threshold + 1e-9)  # Avoid division by zero
                if threshold_ratio > 5.0:  # If MSE is > 5 times the threshold
                    is_critical = True

            # --- Create base result ---
            result = {
                "is_anomaly": is_anomaly,
                "anomaly_score": anomaly_score,
                "is_critical": is_critical,
                "predicted_failures": [],
                "forecast_available": False,
                "recommendation": "monitor" if not is_anomaly else "investigate",
                "prediction_timestamp": datetime.utcnow().isoformat(),
            }

            # --- Attempt forecasting if we have historical data ---
            forecast_data = {}
            predicted_failures = []
            has_forecast = False

            if historical_snapshots and len(historical_snapshots) >= 2:
                try:
                    # Combine historical snapshots with latest for better forecasting
                    metric_history = []
                    for snapshot in historical_snapshots:
                        metric_history.extend(snapshot)
                    metric_history.extend(latest_metrics_snapshot)

                    # Only forecast if we have sufficient historical data
                    forecast_result = await self.forecast_metrics(metric_history)

                    if forecast_result.get("success", False):
                        forecast_data = forecast_result.get("forecast", {})
                        has_forecast = True

                        # Extract predicted failures from threshold crossings
                        threshold_crossings = forecast_data.get("threshold_crossings", {})
                        for metric_name, crossing in threshold_crossings.items():
                            predicted_failures.append({
                                "metric": metric_name,
                                "threshold": crossing.get("threshold"),
                                "minutes_until_failure": crossing.get("minutes_until_crossing"),
                                "confidence": crossing.get("confidence", 0.5)
                            })

                        # Determine recommendation based on forecast
                        risk = forecast_data.get("risk_assessment", "low")
                        if risk == "critical":
                            recommendation = "urgent_action"
                        elif risk == "high":
                            recommendation = "remediate"
                        elif risk == "medium" or is_anomaly:
                            recommendation = "investigate"
                        else:
                            recommendation = "monitor"

                        # Generate more detailed recommendation based on current state and forecast
                        if is_critical:
                            recommendation = "immediate_action"
                        elif is_anomaly or (
                            predicted_failures
                            and any(
                                p["minutes_until_failure"] < 30 and p["confidence"] > 0.6
                                for p in predicted_failures
                            )
                        ):
                            recommendation = "proactive_action"
                        elif predicted_failures:
                            recommendation = "monitor_closely"
                except Exception as e:
                    logger.error(f"Error during forecasting: {e}", exc_info=True)
                    has_forecast = False
                    recommendation = "investigate" if is_anomaly else "monitor"
            else:
                recommendation = "investigate" if is_anomaly else "monitor"
                if is_critical:
                    recommendation = "remediate"

            # Set recommendation in result
            result["recommendation"] = recommendation
            result["forecast_available"] = has_forecast

            # Add forecast data if available
            if has_forecast:
                result["forecast"] = forecast_data
                result["predicted_failures"] = predicted_failures

            return result

        except Exception as e:
            logger.error(f"Error during enhanced prediction: {e}", exc_info=True)
            # Fall back to simpler detection on error
            is_anomaly, anomaly_score, is_critical = self._fallback_prediction(latest_metrics_snapshot)
            return {
                "is_anomaly": is_anomaly,
                "anomaly_score": anomaly_score,
                "is_critical": is_critical,
                "forecast_available": False,
                "recommendation": "monitor" if not is_anomaly else "investigate",
                "prediction_timestamp": datetime.utcnow().isoformat(),
            }

    async def predict(
        self, latest_metrics_snapshot: List[Dict[str, Any]]
    ) -> Optional[Tuple[bool, float, bool]]:
        """
        Predict if the given metrics snapshot contains anomalies.
        This is an updated version that uses the enhanced feature engineering.

        Args:
            latest_metrics_snapshot: List of metric results from Prometheus

        Returns:
            Optional[Tuple[bool, float, bool]]: Tuple containing anomaly detection results, or None if prediction not possible
        """
        # Check TF availability FIRST
        if not TF_AVAILABLE:
            # Use fallback if TF didn't import correctly
            logger.warning("TensorFlow model unavailable. Using fallback detection.")
            return self._fallback_prediction(latest_metrics_snapshot)

        # Check if model/scaler loaded correctly
        if self.model is None or self.scaler is None:
            logger.warning("Anomaly model or scaler not loaded. Using fallback detection.")
            return self._fallback_prediction(latest_metrics_snapshot)

        try:
            # Use the enhanced feature engineering function (without history for backwards compatibility)
            features = await self._engineer_features_with_history(latest_metrics_snapshot)

            if features is None:
                # Get entity ID for better error logging
                entity_id = next(
                    (
                        m.get("labels", {}).get(
                            "pod", m.get("labels", {}).get("node", "unknown")
                        )
                        for m in latest_metrics_snapshot
                        if "labels" in m
                    ),
                    "unknown",
                )
                logger.error(
                    f"Could not engineer features for {entity_id}. Skipping anomaly detection."
                )
                # Let's stick to fallback for consistency if features fail
                return self._fallback_prediction(latest_metrics_snapshot)

            # Get entity ID for logging
            entity_id = next(
                (
                    m.get("labels", {}).get(
                        "pod", m.get("labels", {}).get("node", "unknown")
                    )
                    for m in latest_metrics_snapshot
                    if "labels" in m
                ),
                "unknown",
            )

            # Check how many metrics were actually present vs. using default values
            non_zero_metrics = np.count_nonzero(features)
            total_metrics = features.size
            metrics_present_ratio = non_zero_metrics / total_metrics if total_metrics > 0 else 0

            # If more than 90% of metrics are default values (zeros), use fallback detection
            # Adjust threshold if needed based on testing
            if metrics_present_ratio < 0.1:  # Less than 10% of metrics are actual values
                logger.warning(
                    f"Only {metrics_present_ratio:.2%} of metrics for {entity_id} are non-zero. "
                    "Using fallback detection to avoid false positives."
                )
                return self._fallback_prediction(latest_metrics_snapshot)

            # Check for NaNs/Infs before scaling
            if np.isnan(features).any() or np.isinf(features).any():
                logger.warning("NaNs or Infs detected in features before scaling. Replacing with 0.")
                features = np.nan_to_num(features, nan=0.0, posinf=1e9, neginf=-1e9)

            # Apply pre-scaling normalization (same as in predict_with_forecast)
            normalized_features = self._apply_pre_scaling_normalization(features, entity_id)

            # Now apply the model's scaler
            scaled_features = self.scaler.transform(normalized_features)

            # Check for NaNs/Infs after scaling
            if np.isnan(scaled_features).any() or np.isinf(scaled_features).any():
                logger.warning("NaNs or Infs detected after scaling. Replacing with 0.")
                scaled_features = np.nan_to_num(scaled_features, nan=0.0, posinf=1e9, neginf=-1e9)

            # Get reconstruction from autoencoder
            # Use direct predict since we've verified model is loaded correctly
            reconstruction = self.model.predict(scaled_features, verbose=0)

            # Calculate reconstruction error (MSE)
            mse = np.mean(np.power(scaled_features - reconstruction, 2), axis=1)[0]

            # SAFEGUARD: Cap extremely high anomaly scores which are likely due to missing metrics
            if mse > 100.0:
                logger.warning(
                    f"Extremely high anomaly score detected ({mse:.4f}). Capping to 100.0 to avoid false positives."
                )
                mse = 100.0  # Cap to a reasonable maximum

            # Determine if it's an anomaly based on the threshold
            is_anomaly = mse > self.threshold

            # Higher MSE = more anomalous
            anomaly_score = float(mse)

            # Determine criticality based on how far above threshold
            is_critical = False
            if is_anomaly:
                # Make criticality check more robust
                threshold_ratio = mse / (self.threshold + 1e-9)  # Avoid division by zero
                if threshold_ratio > 5.0:  # If MSE is > 5 times the threshold
                    is_critical = True

            # Log model output for debugging
            logger.debug(f"Model prediction MSE for {entity_id}: {mse:.6f}")

            logger.debug(
                f"Anomaly Prediction - MSE: {mse:.6f}, Threshold: {self.threshold:.6f}, "
                f"Is Anomaly: {is_anomaly}, Is Critical: {is_critical}"
            )
            return is_anomaly, anomaly_score, is_critical

        except Exception as e:
            logger.error(f"Error during prediction: {e}", exc_info=True)
            # Return fallback on any prediction error
            return self._fallback_prediction(latest_metrics_snapshot)

    def _fallback_prediction(
        self, latest_metrics_snapshot: List[Dict[str, Any]]
    ) -> Tuple[bool, float, bool]:
        """
        Fallback prediction method when TensorFlow model is unavailable.
        Uses simple threshold-based detection on raw metrics.

        Args:
            latest_metrics_snapshot: List of metric results from Prometheus

        Returns:
            Tuple containing anomaly detection results
        """
        logger.info("Using fallback prediction method based on threshold rules")

        # Initialize default values
        is_anomaly = False
        anomaly_score = 0.0
        is_critical = False

        # Extract CPU and memory metrics
        cpu_values = []
        memory_values = []
        disk_values = []

        for metric_entry in latest_metrics_snapshot:
            query = metric_entry.get("query", "").lower()
            values = metric_entry.get("values", [])

            if not values:
                continue

            metric_values = [
                float(val[1])
                for val in values
                if isinstance(val, list) and len(val) == 2
            ]

            if "cpu" in query:
                cpu_values.extend(metric_values)
            elif "memory" in query:
                memory_values.extend(metric_values)
            elif "disk" in query or "fs_" in query:
                disk_values.extend(metric_values)

        # Simple threshold-based detection
        if cpu_values:
            max_cpu = max(cpu_values)
            if max_cpu > 0.95:  # 95% CPU usage
                is_anomaly = True
                is_critical = True
                anomaly_score = max(anomaly_score, max_cpu)
            elif max_cpu > 0.85:  # 85% CPU usage
                is_anomaly = True
                anomaly_score = max(anomaly_score, max_cpu)

        if memory_values:
            max_memory = max(memory_values)
            if max_memory > 0.9:  # 90% memory usage
                is_anomaly = True
                is_critical = True
                anomaly_score = max(anomaly_score, max_memory)
            elif max_memory > 0.8:  # 80% memory usage
                is_anomaly = True
                anomaly_score = max(anomaly_score, max_memory)

        if disk_values:
            max_disk = max(disk_values)
            if max_disk > 0.95:  # 95% disk usage
                is_anomaly = True
                is_critical = True
                anomaly_score = max(anomaly_score, max_disk)
            elif max_disk > 0.85:  # 85% disk usage
                is_anomaly = True
                anomaly_score = max(anomaly_score, max_disk)

        # If no specific anomaly was detected, return defaults
        logger.info(
            f"Fallback prediction result: Anomaly={is_anomaly}, Score={anomaly_score:.4f}, Critical={is_critical}"
        )
        return is_anomaly, anomaly_score, is_critical

    async def forecast_metrics(
        self, entity_metrics: List[Dict[str, Any]], forecast_minutes: int = 60
    ) -> Dict[str, Any]:
        """
        Forecast future metric values based on historical trends.

        Args:
            entity_metrics: Historical metrics for an entity
            forecast_minutes: How far into the future (in minutes) to forecast

        Returns:
            Dictionary containing forecast results
        """
        try:
            if not entity_metrics:
                logger.warning("No metrics provided for forecasting.")
                return {
                    "success": False,
                    "error": "No metrics provided for forecasting",
                }

            # Extract time series data for forecastable metrics
            metric_series = {}
            timestamps = {}
            forecast_results = {
                "forecasted_values": {},
                "threshold_crossings": {},
                "forecast_confidence": 0.0,
                "risk_assessment": "low",
            }

            # Extract available time series from different metric types
            for metric_data in entity_metrics:
                values = metric_data.get("values", [])
                if (
                    not values or len(values) < 5
                ):  # Need enough data points for forecasting
                    continue

                metric_name = metric_data.get("name", "unknown")
                if not metric_name:
                    query = metric_data.get("query", "")
                    if "cpu" in query.lower():
                        metric_name = "cpu_usage"
                    elif "memory" in query.lower():
                        metric_name = "memory_usage"
                    elif "disk" in query.lower():
                        metric_name = "disk_usage"
                    elif "network" in query.lower():
                        metric_name = "network_usage"
                    else:
                        metric_name = "unknown"

                # Extract timestamps and values
                ts = [
                    float(val[0])
                    for val in values
                    if isinstance(val, list) and len(val) == 2
                ]
                vals = [
                    float(val[1])
                    for val in values
                    if isinstance(val, list) and len(val) == 2
                ]

                if (
                    len(ts) >= 5 and len(vals) >= 5
                ):  # Only forecast if we have enough data
                    metric_series[metric_name] = vals
                    timestamps[metric_name] = ts

            # Forecast each metric separately
            overall_confidence = []
            risk_scores = []

            for metric_name, values in metric_series.items():
                ts = timestamps[metric_name]
                forecast = self._forecast_single_metric(values, ts, forecast_minutes)

                if forecast:
                    forecast_results["forecasted_values"][metric_name] = forecast[
                        "values"
                    ]

                    # Determine if/when the metric will cross a threshold
                    if metric_name == "cpu_usage":
                        threshold = 0.9  # 90% CPU usage
                        threshold_cross = self._detect_threshold_crossing(
                            forecast["values"], threshold
                        )
                    elif metric_name == "memory_usage":
                        threshold = 0.9  # 90% memory usage
                        threshold_cross = self._detect_threshold_crossing(
                            forecast["values"], threshold
                        )
                    elif metric_name == "disk_usage":
                        threshold = 0.85  # 85% disk usage
                        threshold_cross = self._detect_threshold_crossing(
                            forecast["values"], threshold
                        )
                    else:
                        threshold = None
                        threshold_cross = None

                    if threshold_cross:
                        forecast_results["threshold_crossings"][metric_name] = {
                            "threshold": threshold,
                            "minutes_until_crossing": threshold_cross["minutes"],
                            "confidence": threshold_cross["confidence"],
                        }

                        # Calculate risk based on how soon the threshold will be crossed
                        if threshold_cross["minutes"] < 15:  # Critical if < 15 minutes
                            risk_scores.append(1.0)
                        elif threshold_cross["minutes"] < 30:  # High if < 30 minutes
                            risk_scores.append(0.8)
                        elif threshold_cross["minutes"] < 60:  # Medium if < 60 minutes
                            risk_scores.append(0.5)
                        else:  # Low otherwise
                            risk_scores.append(0.2)

                    overall_confidence.append(forecast["confidence"])

            # Calculate overall forecast confidence and risk
            if overall_confidence:
                forecast_results["forecast_confidence"] = sum(overall_confidence) / len(
                    overall_confidence
                )

            # Determine overall risk assessment
            if risk_scores:
                avg_risk = sum(risk_scores) / len(risk_scores)
                if avg_risk > 0.8:
                    forecast_results["risk_assessment"] = "critical"
                elif avg_risk > 0.6:
                    forecast_results["risk_assessment"] = "high"
                elif avg_risk > 0.3:
                    forecast_results["risk_assessment"] = "medium"
                else:
                    forecast_results["risk_assessment"] = "low"

            return {"success": True, "forecast": forecast_results}

        except Exception as e:
            logger.error("Error forecasting metrics: {}", e, exc_info=True)
            return {"success": False, "error": f"Forecasting error: {str(e)}"}

    def _forecast_single_metric(
        self, values: List[float], timestamps: List[float], forecast_minutes: int
    ) -> Dict[str, Any]:
        """
        Forecast a single metric time series.

        Args:
            values: Historical values for the metric
            timestamps: Timestamps corresponding to the values
            forecast_minutes: Minutes to forecast into the future

        Returns:
            Dictionary with forecast values and confidence
        """
        try:
            # Clean data (remove NaN, Inf)
            clean_indices = [
                i for i, v in enumerate(values) if not (np.isnan(v) or np.isinf(v))
            ]
            if not clean_indices or len(clean_indices) < 3:
                return None

            clean_values = [values[i] for i in clean_indices]
            clean_timestamps = [timestamps[i] for i in clean_indices]

            # Determine time interval between samples
            time_diffs = np.diff(clean_timestamps)
            avg_interval = (
                np.mean(time_diffs) if len(time_diffs) > 0 else 60
            )  # Default to 60 seconds

            # Convert to numpy arrays for vectorized operations
            x = np.array(range(len(clean_values)))
            y = np.array(clean_values)

            # Fit linear regression model for the trend
            slope, intercept = np.polyfit(x, y, 1)

            # Calculate forecast steps based on time interval
            steps = int((forecast_minutes * 60) / avg_interval)
            forecast_x = np.array(range(len(clean_values), len(clean_values) + steps))

            # Simple linear forecast
            linear_forecast = slope * forecast_x + intercept

            # Apply bounds - don't allow negative values for usage metrics
            linear_forecast = np.maximum(linear_forecast, 0)

            # Determine confidence based on historical fit
            historical_fit = slope * x + intercept
            fit_error = np.mean(
                np.abs(historical_fit - y) / (y + 1e-8)
            )  # Add small epsilon to avoid div by 0
            confidence = max(
                0, min(1, 1 - fit_error)
            )  # Higher error = lower confidence

            return {"values": linear_forecast.tolist(), "confidence": confidence}

        except Exception as e:
            logger.error("Error in single metric forecast: {}", e)
            return None

    def _detect_threshold_crossing(
        self, forecasted_values: List[float], threshold: float
    ) -> Dict[str, Any]:
        """
        Detect if and when a forecasted metric will cross a threshold.

        Args:
            forecasted_values: List of forecasted values
            threshold: The threshold to check for crossing

        Returns:
            Dictionary with information about the threshold crossing, or None if no crossing
        """
        try:
            # Find the first index where the value exceeds the threshold
            for i, value in enumerate(forecasted_values):
                if value > threshold:
                    # Calculate minutes until threshold crossing based on index
                    minutes_until_crossing = (
                        i + 1
                    )  # Simplification, assuming each step is a minute

                    # Calculate confidence based on how close to threshold
                    # Values much higher than threshold = higher confidence in the crossing
                    confidence = min(1.0, max(0.5, (value - threshold) / threshold))

                    return {"minutes": minutes_until_crossing, "confidence": confidence}

            # No threshold crossing detected
            return None

        except Exception as e:
            logger.error("Error detecting threshold crossing: {}", e)
            return None

    def get_model_parameters(self) -> Dict[str, Any]:
        """
        Get the current model parameters.

        Returns:
            Dictionary containing model parameters
        """
        try:
            if not self.model:
                return {"status": "not_loaded"}

            return {
                "status": "loaded",
                "model_type": "Keras Autoencoder",
                "threshold": self.threshold,
                "expected_features": self.total_features,
                "feature_columns": self.feature_columns,
                "scaler_fitted": self.scaler is not None,
            }

        except Exception as e:
            logger.error("Error getting model parameters: {}", e)
            return {"status": "error", "error": str(e)}

    async def check_compatibility(self, sample_data: List[Dict[str, Any]]) -> bool:
        """
        Check if the provided sample data is compatible with the anomaly detection model.

        Args:
            sample_data: Sample metric data to check for compatibility

        Returns:
            Boolean indicating whether the data is compatible with the model
        """
        try:
            # Check if we have any data
            if not sample_data:
                logger.warning("No sample data provided for compatibility check")
                return False

            # Try to engineer features from the sample data
            features = self._engineer_features_aggregate(sample_data)

            if features is None:
                logger.warning("Could not engineer features from sample data")
                return False

            # Check if the engineered features match the expected shape
            if features.shape[1] != self.total_features:
                logger.warning(
                    f"Feature count mismatch: expected {self.total_features}, got {features.shape[1]}"
                )
                return False

            # Log feature vector details for debugging
            logger.debug(f"Feature vector shape: {features.shape}")
            logger.debug(
                f"Feature vector: {features.flatten()[:10]}..."
            )  # Only show first 10 elements

            # Check if any feature has all zeros (indicates that metric might be missing)
            feature_vector = features.flatten()
            feature_chunks = [
                feature_vector[i : i + self.num_features_per_metric]
                for i in range(0, len(feature_vector), self.num_features_per_metric)
            ]

            for i, chunk in enumerate(feature_chunks):
                if np.all(chunk == 0):
                    logger.warning(
                        f"Feature group {i} ({self.feature_columns[i]}) has all zeros - metric may be missing"
                    )
                    # Don't fail compatibility just for this, as we have fallbacks

            # If model and scaler are available, test if they can process the features
            if self.scaler:
                try:
                    scaled_features = self.scaler.transform(features)
                    # Check for NaN or Inf values after scaling
                    if (
                        np.isnan(scaled_features).any()
                        or np.isinf(scaled_features).any()
                    ):
                        logger.warning("Scaled features contain NaN or Inf values")
                        scaled_features = np.nan_to_num(
                            scaled_features, nan=0.0, posinf=1e9, neginf=-1e9
                        )

                    # All good with scaler
                    logger.debug("Features compatible with scaler")
                except Exception as e:
                    logger.warning(f"Error scaling features: {e}")
                    return False

            # Check if model can process the features
            if TF_AVAILABLE and self.model:
                try:
                    # Get scaled features
                    scaled_features = (
                        self.scaler.transform(features) if self.scaler else features
                    )
                    scaled_features = np.nan_to_num(
                        scaled_features, nan=0.0, posinf=1e9, neginf=-1e9
                    )

                    # Make prediction with model
                    reconstruction = self.model.predict(scaled_features, verbose=0)

                    # Calculate MSE
                    mse = np.mean(
                        np.power(scaled_features - reconstruction, 2), axis=1
                    )[0]
                    logger.info(
                        f"Compatibility test MSE: {mse:.6f} (threshold: {self.threshold:.6f})"
                    )

                    # Successfully processed by model
                    return True
                except Exception as e:
                    logger.warning(f"Error during model prediction: {e}")
                    return False

            # If we can generate features but model/scaler aren't available,
            # consider compatible for fallback prediction
            logger.info("Features generated successfully, using fallback compatibility")
            return True

        except Exception as e:
            logger.error("Error checking data compatibility: {}", e)
            return False

    async def retrain_if_needed(
        self, training_data: List[List[Dict[str, Any]]]
    ) -> bool:
        """
        Retrain the model if needed with new training data.

        Args:
            training_data: List of metric snapshots to train on

        Returns:
            Boolean indicating whether retraining was successful
        """
        if not self.needs_retraining:
            logger.info("Model retraining not needed at this time")
            return False

        if not TF_AVAILABLE:
            logger.warning("Cannot retrain model: TensorFlow not available")
            return False

        if len(training_data) < 10:
            logger.warning(f"Insufficient training data: {len(training_data)} snapshots (minimum 10 required)")
            return False

        try:
            logger.info(f"Starting model retraining with {len(training_data)} data points")

            # Flatten training data if it's nested
            flattened_data = []
            for snapshot in training_data:
                if isinstance(snapshot, list):
                    flattened_data.extend(snapshot)
                else:
                    flattened_data.append(snapshot)

            # Engineer features for training
            features = await self._engineer_features_with_history(flattened_data)

            if features is None or features.shape[0] < 10:
                logger.error("Failed to engineer enough features for retraining")
                return False

            # Update the scaler with new data
            self.scaler.fit(features)

            # Save the updated scaler
            joblib.dump(self.scaler, self.scaler_path)

            # Scale the features for model training
            scaled_features = self.scaler.transform(features)
            scaled_features = np.nan_to_num(scaled_features, nan=0.0, posinf=1e9, neginf=-1e9)

            # Build a new autoencoder model (similar architecture as current)
            input_dim = self.total_features

            # Define the architecture
            input_layer = tf.keras.layers.Input(shape=(input_dim,))

            # Encoder layers
            encoded = tf.keras.layers.Dense(24, activation='relu')(input_layer)
            encoded = tf.keras.layers.Dense(12, activation='relu')(encoded)
            encoded = tf.keras.layers.Dense(6, activation='relu')(encoded)

            # Decoder layers
            decoded = tf.keras.layers.Dense(12, activation='relu')(encoded)
            decoded = tf.keras.layers.Dense(24, activation='relu')(decoded)
            output_layer = tf.keras.layers.Dense(input_dim, activation='linear')(decoded)

            # Create and compile the model
            new_model = tf.keras.models.Model(inputs=input_layer, outputs=output_layer)
            new_model.compile(optimizer='adam', loss='mse')

            # Train the model
            new_model.fit(
                scaled_features,
                scaled_features,
                epochs=50,
                batch_size=32,
                shuffle=True,
                validation_split=0.2,
                verbose=1
            )

            # Save the trained model
            new_model.save(self.model_path)

            # Update the current model
            self.model = new_model

            # Reset the retraining flag
            self.needs_retraining = False

            logger.success("Model retrained and saved successfully")
            return True

        except Exception as e:
            logger.error(f"Error during model retraining: {e}", exc_info=True)
            return False

    def _apply_pre_scaling_normalization(self, features: np.ndarray, entity_id: str) -> np.ndarray:
        """
        Apply a preliminary normalization to features before sending to the model scaler.
        This function normalizes metrics by their typical expected ranges to prevent extreme scale differences.

        Args:
            features: The engineered feature vector (shape: 1, n_features)
            entity_id: The entity ID for logging purposes

        Returns:
            Normalized feature vector ready for scaling
        """
        try:
            # Make a copy to avoid modifying the original
            normalized = features.copy()
            feature_array = normalized.flatten()

            # Get the feature groups (groups of 5 features per metric type)
            n_groups = len(self.feature_columns)
            feature_groups = np.split(feature_array, n_groups)

            # Define reasonable bounds for different metric types
            # Each entry has: [max_reasonable_value, is_rate_or_percent]
            # Rate/percent metrics are capped at 1.0, absolute metrics have specific caps
            metric_bounds = {
                "cpu_usage_seconds_total": [5.0, True],  # Max 5 cores, typically < 1.0
                "memory_working_set_bytes": [8e9, False],  # Max 8GB
                "network_receive_bytes_total": [100e6, True],  # Max 100 MB/s
                "network_transmit_bytes_total": [100e6, True],  # Max 100 MB/s
                "pod_status_phase": [1.0, True],  # Binary 0 or 1
                "container_restarts_rate": [1.0, True],  # Now treating as a rate (per second)
            }

            # Normalize each feature group based on its metric type
            for i, metric_type in enumerate(self.feature_columns):
                group = feature_groups[i]
                bounds = metric_bounds.get(metric_type, [100.0, False])  # Default if not in map
                max_value = bounds[0]
                is_rate_or_percent = bounds[1]

                # Check for extremely large values
                if np.max(np.abs(group)) > max_value * 10:
                    # Log the extreme values
                    logger.warning(f"Extreme values detected for {metric_type} in {entity_id}: {np.max(np.abs(group)):.2f}")

                    # For rate metrics (usually between 0-1), cap at 1.0
                    if is_rate_or_percent:
                        # Cap at max_value (usually 1.0) but preserve sign
                        group = np.clip(group, -max_value, max_value)
                    else:
                        # For absolute metrics, normalize to the expected range
                        # Divide by 10x max expected value to bring into reasonable range
                        scale_factor = max_value * 10
                        group = group / scale_factor
                        logger.debug(f"Scaled {metric_type} by factor {scale_factor}")

                # Update the feature group in our array
                feature_groups[i] = group

            # Recombine the normalized groups
            normalized_flat = np.concatenate(feature_groups)
            return normalized_flat.reshape(1, -1)  # Return in original shape (1, n_features)

        except Exception as e:
            logger.error(f"Error during pre-scaling normalization: {e}")
            # Return original features if normalization fails
            return features


# Create a global instance of the AnomalyDetector
anomaly_detector = AnomalyDetector()
logger.info("Global AnomalyDetector instance created")
