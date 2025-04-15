from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sklearn.exceptions import NotFittedError
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
import numpy as np
import joblib
import os
import json
from datetime import datetime
from typing import List, Dict, Any, Tuple
from loguru import logger

from app.core.config import settings
from app.services.gemini_service import GeminiService


class AnomalyDetector:
    """
    Anomaly detection using Isolation Forest algorithm with continuous training and Gemini-based evaluation.

    This class handles the detection of anomalies in Kubernetes metrics using a machine learning approach.
    It provides methods for training the model, making predictions, and evaluating model performance.
    """

    def __init__(self):
        """Initialize the anomaly detector with continuous training and evaluation."""
        self.model = None
        self.gemini_service = GeminiService()
        self.best_metrics = {
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1_score": 0.0
        }
        self.scaler = StandardScaler()
        self.best_model_path = settings.ANOMALY_MODEL_PATH
        self.feature_names = []  # Will be populated during feature engineering
        self._load_model()

        # If model needs retraining, mark it for retraining
        if not self.model:
            logger.warning("Model needs retraining. Will retrain on next available data.")
            self.needs_retraining = True
        else:
            self.needs_retraining = False

    def _load_model(self):
        """
        Load the trained model and scaler if they exist.

        This method attempts to load a previously trained model and scaler from disk.
        If loading fails, it initializes a new scaler and logs appropriate messages.
        """
        try:
            if os.path.exists(self.best_model_path):
                # Load both model and scaler from a single file
                saved_state = joblib.load(self.best_model_path)
                self.model = saved_state.get('model')
                self.scaler = saved_state.get('scaler', StandardScaler())
                self.feature_names = saved_state.get('feature_names', [])
                logger.info(f"Loaded existing anomaly detection model and scaler from {self.best_model_path}")

                if self.model and hasattr(self.model, 'n_features_in_'):
                    logger.info(f"Model expects {self.model.n_features_in_} features")
                    if len(self.feature_names) != self.model.n_features_in_:
                        logger.warning("Feature names count doesn't match model's expected features. Model needs retraining.")
                        self.model = None
                        self.needs_retraining = True
            else:
                logger.info("No existing model/scaler file found. Model needs training.")
                self.model = None
                self.scaler = StandardScaler()
                self.feature_names = []
                self.needs_retraining = True
        except KeyError as e:
            logger.error(f"Error loading model/scaler: File format mismatch ({e}). Needs retraining.")
            self.model = None
            self.scaler = StandardScaler()
            self.feature_names = []
            self.needs_retraining = True
        except Exception as e:
            logger.error(f"Error loading model/scaler: {e}")
            self.model = None
            self.scaler = StandardScaler()
            self.feature_names = []
            self.needs_retraining = True

    def save_model(self) -> None:
        """
        Save the current model and scaler to disk.

        This method saves both the trained model and fitted scaler to a single file.
        It validates that both components are properly initialized before saving.
        """
        if not self.model:
            logger.warning("Cannot save untrained model")
            return

        # Check if scaler is fitted
        try:
            if not hasattr(self.scaler, 'mean_'):
                logger.warning("Scaler is not fitted. Cannot save.")
                return
        except Exception as e:
            logger.error(f"Error checking scaler: {e}")
            return

        os.makedirs(os.path.dirname(self.best_model_path), exist_ok=True)

        try:
            # Save model, scaler, and feature names in a dictionary to a single file
            saved_state = {
                'model': self.model,
                'scaler': self.scaler,
                'feature_names': self.feature_names
            }
            joblib.dump(saved_state, self.best_model_path)
            logger.info(f"Model, scaler, and feature names saved to {self.best_model_path}")
        except Exception as e:
            logger.error(f"Error saving model and scaler: {e}")
            raise

    def _engineer_features_aggregate(self, data_snapshots: List[List[Dict[str, Any]]]) -> np.ndarray:
        """
        Engineers a single, fixed-length feature vector for each entity snapshot.
        Aggregates features across all metrics and adds temporal features.
        """
        all_feature_vectors = []
        feature_dict = {}  # Template for feature names

        # Define metric processors with their feature extraction functions
        metric_processors = {
            "cpu": {
                "patterns": ["cpu_usage", "cpu_seconds", "cpu_utilization"],
                "processor": self._process_cpu_metrics
            },
            "memory": {
                "patterns": ["memory_usage", "memory_working_set", "memory_utilization"],
                "processor": self._process_memory_metrics
            },
            "network": {
                "patterns": ["network_io", "network_bytes", "network_packets"],
                "processor": self._process_network_metrics
            },
            "disk": {
                "patterns": ["disk_io", "disk_bytes", "disk_utilization"],
                "processor": self._process_disk_metrics
            }
        }

        # First pass: determine all possible features
        for snapshot in data_snapshots:
            # Group metrics by type using pattern matching
            metrics_by_type = {metric_type: [] for metric_type in metric_processors}
            for metric_data in snapshot:
                query = metric_data.get("query", "").lower()
                for metric_type, processor_info in metric_processors.items():
                    if any(pattern in query for pattern in processor_info["patterns"]):
                        metrics_by_type[metric_type].append(metric_data)

            # Process each metric type
            for metric_type, metrics_list in metrics_by_type.items():
                if metrics_list:
                    processor_func = metric_processors[metric_type]["processor"]
                    processor_func(metrics_list, feature_dict)
                else:
                    # Add default values for missing metrics
                    self._add_default_features(metric_type, feature_dict)

            # Add temporal features across the entire snapshot
            self._add_temporal_features(snapshot, feature_dict)

        # Set feature names if not already set
        if not self.feature_names:
            self.feature_names = sorted(feature_dict.keys())
            logger.info(f"Determined {len(self.feature_names)} features: {self.feature_names}")

        # Second pass: create feature vectors
        for snapshot in data_snapshots:
            feature_dict = {}

            # Group metrics by type using pattern matching
            metrics_by_type = {metric_type: [] for metric_type in metric_processors}
            for metric_data in snapshot:
                query = metric_data.get("query", "").lower()
                for metric_type, processor_info in metric_processors.items():
                    if any(pattern in query for pattern in processor_info["patterns"]):
                        metrics_by_type[metric_type].append(metric_data)

            # Process each metric type
            for metric_type, metrics_list in metrics_by_type.items():
                if metrics_list:
                    processor_func = metric_processors[metric_type]["processor"]
                    processor_func(metrics_list, feature_dict)
                else:
                    # Add default values for missing metrics
                    self._add_default_features(metric_type, feature_dict)

            # Add temporal features across the entire snapshot
            self._add_temporal_features(snapshot, feature_dict)

            # Convert feature dictionary to vector
            feature_vector = []
            for feature_name in self.feature_names:
                feature_vector.append(feature_dict.get(feature_name, 0.0))

            all_feature_vectors.append(feature_vector)

        return np.array(all_feature_vectors)

    def _add_default_features(self, metric_type: str, feature_dict: dict):
        """Add default features for missing metrics."""
        defaults = {
            "cpu": {
                "cpu_mean": 0.0,
                "cpu_max": 0.0,
                "cpu_last": 0.0,
                "cpu_rate": 0.0,
                "cpu_std": 0.0,
                "cpu_trend": 0.0
            },
            "memory": {
                "memory_mean": 0.0,
                "memory_max": 0.0,
                "memory_last": 0.0,
                "memory_rate": 0.0,
                "memory_std": 0.0,
                "memory_trend": 0.0
            },
            "network": {
                "network_in_mean": 0.0,
                "network_out_mean": 0.0,
                "network_in_rate": 0.0,
                "network_out_rate": 0.0
            },
            "disk": {
                "disk_read_mean": 0.0,
                "disk_write_mean": 0.0,
                "disk_read_rate": 0.0,
                "disk_write_rate": 0.0
            }
        }
        feature_dict.update(defaults.get(metric_type, {}))

    def _add_temporal_features(self, snapshot: List[Dict[str, Any]], feature_dict: dict):
        """Add temporal features across the entire metric snapshot."""
        try:
            # Extract timestamps and values for all metrics
            all_timestamps = []
            all_values = []

            for metric_data in snapshot:
                values = metric_data.get("values", [])
                if values:
                    timestamps = [float(val[0]) for val in values if isinstance(val, list) and len(val) == 2]
                    metric_values = [float(val[1]) for val in values if isinstance(val, list) and len(val) == 2]
                    if timestamps and metric_values:
                        all_timestamps.extend(timestamps)
                        all_values.extend(metric_values)

            if all_timestamps and all_values:
                # Sort by timestamp
                sorted_data = sorted(zip(all_timestamps, all_values))
                timestamps, values = zip(*sorted_data)

                # Calculate temporal features with numerical stability checks
                if len(values) > 1:
                    # Rate of change with zero division protection
                    time_diff = timestamps[-1] - timestamps[0]
                    if time_diff > 0:
                        feature_dict["overall_rate"] = (values[-1] - values[0]) / time_diff
                    else:
                        feature_dict["overall_rate"] = 0.0

                    # Standard deviation with numerical stability
                    if len(values) > 2:
                        # Handle potential NaN values before calculating std
                        clean_values = [v for v in values if not (np.isnan(v) or np.isinf(v))]
                        if clean_values:
                            feature_dict["overall_std"] = np.std(clean_values)
                        else:
                            feature_dict["overall_std"] = 0.0
                    else:
                        feature_dict["overall_std"] = 0.0

                    # Trend analysis with numerical stability
                    try:
                        # Handle potential NaN values before polyfit
                        valid_indices = [i for i, v in enumerate(values) if not (np.isnan(v) or np.isinf(v))]
                        if len(valid_indices) > 1:
                            valid_x = np.array(valid_indices)
                            valid_y = np.array([values[i] for i in valid_indices])
                            trend = np.polyfit(valid_x, valid_y, 1)[0]
                            feature_dict["overall_trend"] = trend
                        else:
                            feature_dict["overall_trend"] = 0.0
                    except:
                        feature_dict["overall_trend"] = 0.0

                    # Moving average features with window size check
                    window_size = min(5, len(values))
                    if window_size > 1:
                        try:
                            # Handle potential NaN values before convolution
                            clean_values = np.array([v if not (np.isnan(v) or np.isinf(v)) else 0.0 for v in values])
                            moving_avg = np.convolve(clean_values, np.ones(window_size)/window_size, mode='valid')
                            feature_dict["moving_avg_mean"] = np.mean(moving_avg) if len(moving_avg) > 0 else 0.0
                            feature_dict["moving_avg_std"] = np.std(moving_avg) if len(moving_avg) > 1 else 0.0
                        except:
                            feature_dict["moving_avg_mean"] = 0.0
                            feature_dict["moving_avg_std"] = 0.0
                    else:
                        feature_dict["moving_avg_mean"] = float(values[0]) if not (np.isnan(values[0]) or np.isinf(values[0])) else 0.0
                        feature_dict["moving_avg_std"] = 0.0

                    # Volatility features with zero division protection
                    if len(values) > 2:
                        try:
                            # Handle potential NaN or infinity values
                            clean_values = np.array([v if not (np.isnan(v) or np.isinf(v)) else 0.0 for v in values])
                            if len(clean_values) > 1:
                                returns = np.diff(clean_values)
                                non_zero_values = clean_values[:-1]
                                # Make sure not to divide by zero
                                non_zero_mask = non_zero_values != 0
                                if np.any(non_zero_mask):
                                    # Use numpy's safe division that handles division by zero
                                    safe_returns = np.zeros_like(returns, dtype=float)
                                    safe_returns[non_zero_mask] = np.divide(
                                        returns[non_zero_mask],
                                        non_zero_values[non_zero_mask],
                                        out=np.zeros_like(returns[non_zero_mask], dtype=float),
                                        where=non_zero_values[non_zero_mask] != 0
                                    )
                                    feature_dict["volatility"] = np.std(safe_returns) if len(safe_returns) > 0 else 0.0
                                else:
                                    feature_dict["volatility"] = 0.0
                            else:
                                feature_dict["volatility"] = 0.0
                        except Exception as e:
                            logger.debug(f"Error calculating volatility: {e}")
                            feature_dict["volatility"] = 0.0
                    else:
                        feature_dict["volatility"] = 0.0

                    # Seasonality detection with length check
                    if len(values) > 10:
                        try:
                            # Handle potential NaN values
                            clean_values = np.array([v if not (np.isnan(v) or np.isinf(v)) else 0.0 for v in values])
                            mean_val = np.mean(clean_values)
                            centered_values = clean_values - mean_val
                            # Check for all zeros to avoid invalid correlation
                            if np.any(centered_values != 0):
                                autocorr = np.correlate(centered_values, centered_values, mode='full')
                                autocorr = autocorr[len(autocorr)//2:]
                                feature_dict["seasonality_strength"] = np.max(autocorr[1:10]) if len(autocorr) > 10 else 0.0
                            else:
                                feature_dict["seasonality_strength"] = 0.0
                        except:
                            feature_dict["seasonality_strength"] = 0.0
                    else:
                        feature_dict["seasonality_strength"] = 0.0
                else:
                    # Single value case
                    safe_value = float(values[0]) if values and not (np.isnan(values[0]) or np.isinf(values[0])) else 0.0
                    feature_dict.update({
                        "overall_rate": 0.0,
                        "overall_std": 0.0,
                        "overall_trend": 0.0,
                        "moving_avg_mean": safe_value,
                        "moving_avg_std": 0.0,
                        "volatility": 0.0,
                        "seasonality_strength": 0.0
                    })
            else:
                # No values case
                feature_dict.update({
                    "overall_rate": 0.0,
                    "overall_std": 0.0,
                    "overall_trend": 0.0,
                    "moving_avg_mean": 0.0,
                    "moving_avg_std": 0.0,
                    "volatility": 0.0,
                    "seasonality_strength": 0.0
                })
        except Exception as e:
            logger.error(f"Error calculating temporal features: {e}")
            # Add default temporal features
            feature_dict.update({
                "overall_rate": 0.0,
                "overall_std": 0.0,
                "overall_trend": 0.0,
                "moving_avg_mean": 0.0,
                "moving_avg_std": 0.0,
                "volatility": 0.0,
                "seasonality_strength": 0.0
            })

    def _process_cpu_metrics(self, metrics_list, feature_dict):
        """Process CPU metrics to extract multi-dimensional features."""
        values_list = []
        timestamps = []

        for metric_data in metrics_list:
            try:
                values = [float(val[1]) for val in metric_data.get("values", [])
                         if isinstance(val, list) and len(val) == 2]
                ts = [float(val[0]) for val in metric_data.get("values", [])
                      if isinstance(val, list) and len(val) == 2]
                if values and ts:
                    values_list.extend(values)
                    timestamps.extend(ts)
            except (ValueError, TypeError):
                continue

        if values_list and timestamps:
            # Sort by timestamp
            sorted_data = sorted(zip(timestamps, values_list))
            timestamps, values_list = zip(*sorted_data)

            # Basic statistics
            feature_dict["cpu_mean"] = np.mean(values_list)
            feature_dict["cpu_max"] = np.max(values_list)
            feature_dict["cpu_last"] = values_list[-1]

            # Temporal features
            if len(values_list) > 1:
                # Rate of change
                feature_dict["cpu_rate"] = (values_list[-1] - values_list[0]) / (timestamps[-1] - timestamps[0])
                # Volatility
                feature_dict["cpu_std"] = np.std(values_list)
                # Trend detection
                feature_dict["cpu_trend"] = np.polyfit(range(len(values_list)), values_list, 1)[0]

                # Additional temporal features
                window_size = min(5, len(values_list))
                moving_avg = np.convolve(values_list, np.ones(window_size)/window_size, mode='valid')
                feature_dict["cpu_moving_avg"] = np.mean(moving_avg) if len(moving_avg) > 0 else 0.0

                # CPU utilization patterns
                feature_dict["cpu_utilization_high"] = np.mean([1 if v > 0.8 else 0 for v in values_list])
                feature_dict["cpu_utilization_low"] = np.mean([1 if v < 0.2 else 0 for v in values_list])

    def _process_memory_metrics(self, metrics_list, feature_dict):
        """Process memory metrics to extract multi-dimensional features."""
        values_list = []
        timestamps = []

        for metric_data in metrics_list:
            try:
                values = [float(val[1]) for val in metric_data.get("values", [])
                         if isinstance(val, list) and len(val) == 2]
                ts = [float(val[0]) for val in metric_data.get("values", [])
                      if isinstance(val, list) and len(val) == 2]
                if values and ts:
                    values_list.extend(values)
                    timestamps.extend(ts)
            except (ValueError, TypeError):
                continue

        if values_list and timestamps:
            # Sort by timestamp
            sorted_data = sorted(zip(timestamps, values_list))
            timestamps, values_list = zip(*sorted_data)

            # Basic statistics
            feature_dict["memory_mean"] = np.mean(values_list)
            feature_dict["memory_max"] = np.max(values_list)
            feature_dict["memory_last"] = values_list[-1]

            # Temporal features
            if len(values_list) > 1:
                # Rate of change
                feature_dict["memory_rate"] = (values_list[-1] - values_list[0]) / (timestamps[-1] - timestamps[0])
                # Volatility
                feature_dict["memory_std"] = np.std(values_list)
                # Trend detection
                feature_dict["memory_trend"] = np.polyfit(range(len(values_list)), values_list, 1)[0]

                # Additional temporal features
                window_size = min(5, len(values_list))
                moving_avg = np.convolve(values_list, np.ones(window_size)/window_size, mode='valid')
                feature_dict["memory_moving_avg"] = np.mean(moving_avg) if len(moving_avg) > 0 else 0.0

                # Memory utilization patterns
                feature_dict["memory_utilization_high"] = np.mean([1 if v > 0.8 else 0 for v in values_list])
                feature_dict["memory_utilization_low"] = np.mean([1 if v < 0.2 else 0 for v in values_list])

    def _process_network_metrics(self, metrics_list, feature_dict):
        """Process network metrics to extract multi-dimensional features."""
        in_values = []
        out_values = []
        timestamps = []

        for metric_data in metrics_list:
            try:
                values = [float(val[1]) for val in metric_data.get("values", [])
                         if isinstance(val, list) and len(val) == 2]
                ts = [float(val[0]) for val in metric_data.get("values", [])
                      if isinstance(val, list) and len(val) == 2]
                if values and ts:
                    # Determine if this is incoming or outgoing traffic
                    query = metric_data.get("query", "").lower()
                    if "receive" in query or "rx" in query:
                        in_values.extend(values)
                    elif "transmit" in query or "tx" in query:
                        out_values.extend(values)
                    timestamps.extend(ts)
            except (ValueError, TypeError):
                continue

        if timestamps:
            # Process incoming traffic
            if in_values:
                sorted_data = sorted(zip(timestamps[:len(in_values)], in_values))
                ts, values = zip(*sorted_data)
                feature_dict["network_in_mean"] = np.mean(values)
                feature_dict["network_in_max"] = np.max(values)
                feature_dict["network_in_last"] = values[-1]
                if len(values) > 1:
                    feature_dict["network_in_rate"] = (values[-1] - values[0]) / (ts[-1] - ts[0])
                    feature_dict["network_in_std"] = np.std(values)
                    feature_dict["network_in_trend"] = np.polyfit(range(len(values)), values, 1)[0]

            # Process outgoing traffic
            if out_values:
                sorted_data = sorted(zip(timestamps[:len(out_values)], out_values))
                ts, values = zip(*sorted_data)
                feature_dict["network_out_mean"] = np.mean(values)
                feature_dict["network_out_max"] = np.max(values)
                feature_dict["network_out_last"] = values[-1]
                if len(values) > 1:
                    feature_dict["network_out_rate"] = (values[-1] - values[0]) / (ts[-1] - ts[0])
                    feature_dict["network_out_std"] = np.std(values)
                    feature_dict["network_out_trend"] = np.polyfit(range(len(values)), values, 1)[0]

    def _process_disk_metrics(self, metrics_list, feature_dict):
        """Process disk metrics to extract multi-dimensional features."""
        read_values = []
        write_values = []
        timestamps = []

        for metric_data in metrics_list:
            try:
                values = [float(val[1]) for val in metric_data.get("values", [])
                         if isinstance(val, list) and len(val) == 2]
                ts = [float(val[0]) for val in metric_data.get("values", [])
                      if isinstance(val, list) and len(val) == 2]
                if values and ts:
                    # Determine if this is read or write operations
                    query = metric_data.get("query", "").lower()
                    if "read" in query:
                        read_values.extend(values)
                    elif "write" in query:
                        write_values.extend(values)
                    timestamps.extend(ts)
            except (ValueError, TypeError):
                continue

        if timestamps:
            # Process read operations
            if read_values:
                sorted_data = sorted(zip(timestamps[:len(read_values)], read_values))
                ts, values = zip(*sorted_data)
                feature_dict["disk_read_mean"] = np.mean(values)
                feature_dict["disk_read_max"] = np.max(values)
                feature_dict["disk_read_last"] = values[-1]
                if len(values) > 1:
                    feature_dict["disk_read_rate"] = (values[-1] - values[0]) / (ts[-1] - ts[0])
                    feature_dict["disk_read_std"] = np.std(values)
                    feature_dict["disk_read_trend"] = np.polyfit(range(len(values)), values, 1)[0]

            # Process write operations
            if write_values:
                sorted_data = sorted(zip(timestamps[:len(write_values)], write_values))
                ts, values = zip(*sorted_data)
                feature_dict["disk_write_mean"] = np.mean(values)
                feature_dict["disk_write_max"] = np.max(values)
                feature_dict["disk_write_last"] = values[-1]
                if len(values) > 1:
                    feature_dict["disk_write_rate"] = (values[-1] - values[0]) / (ts[-1] - ts[0])
                    feature_dict["disk_write_std"] = np.std(values)
                    feature_dict["disk_write_trend"] = np.polyfit(range(len(values)), values, 1)[0]

    async def train_with_evaluation(self, data: List[Dict[str, Any]]):
        """
        Train the anomaly detection model with evaluation.

        Args:
            data: List of metrics data structured appropriately for feature engineering

        Raises:
            Exception: If training fails due to data issues or other errors
        """
        try:
            if not data:
                logger.error("Cannot train model: No training data provided.")
                return

            logger.info(f"Starting model training with {len(data)} data points.")

            # Engineer features - wrap data in a list since _engineer_features_aggregate
            # expects a list of snapshots (where each snapshot is a list of metrics)
            X = self._engineer_features_aggregate(data)

            if X.size == 0:
                logger.error("Cannot train model: Feature engineering yielded no features.")
                return

            logger.info(f"Engineered features shape: {X.shape}")

            # Fit and transform the scaler
            self.scaler = StandardScaler()  # Re-initialize scaler for new training
            X_scaled = self.scaler.fit_transform(X)
            logger.info("Scaler fitted and data transformed.")

            # Handle NaN values by replacing them with zeros
            X_scaled = np.nan_to_num(X_scaled, nan=0.0)

            # Train model with an extremely low contamination value to reduce false positives
            # This makes the model extremely selective about what it classifies as anomalous
            contamination = 0.005  # Super conservative threshold, reduced from 0.01
            self.model = IsolationForest(
                contamination=contamination,
                random_state=42,
                n_estimators=200,  # Increased from 150 for even better stability
                max_samples='auto'  # Allow model to use the optimal number of samples
            )
            self.model.fit(X_scaled)
            logger.info(f"IsolationForest model trained with contamination={contamination}.")

            # Save model and scaler
            self.save_model()

        except Exception as e:
            logger.error(f"Error training model: {e}", exc_info=True)
            # Reset model/scaler on training error
            self.model = None
            self.scaler = StandardScaler()
            raise

    async def retrain_if_needed(self, data: List[Dict[str, Any]]) -> bool:
        """
        Retrain the model if needed and return whether retraining was performed.

        Args:
            data: List of metrics data for training

        Returns:
            bool: True if retraining was performed, False otherwise
        """
        if not self.needs_retraining:
            return False

        try:
            logger.info("Starting model retraining due to feature mismatch or missing model")
            await self.train_with_evaluation(data)
            self.needs_retraining = False
            logger.info("Model retraining completed successfully")
            return True
        except Exception as e:
            logger.error(f"Error during model retraining: {e}")
            return False

    async def predict(self, latest_metrics_snapshot: List[Dict[str, Any]]) -> Tuple[bool, float, bool]:
        """
        Predict if the given entity snapshot contains anomalies or failures.

        Args:
            latest_metrics_snapshot: List of metric results for ONE entity

        Returns:
            Tuple containing:
                - Boolean indicating if an anomaly was detected
                - Float representing the anomaly score (lower is more anomalous)
                - Boolean indicating if this is a critical failure
        """
        try:
            if not self.model:
                logger.warning("Model not trained. Cannot make predictions.")
                return False, 0.0, False

            if not latest_metrics_snapshot:
                logger.warning("No metrics provided for prediction.")
                return False, 0.0, False

            # Engineer features for the snapshot
            X = self._engineer_features_aggregate([latest_metrics_snapshot])

            if X.size == 0:
                logger.warning("No features could be extracted from the metrics.")
                return False, 0.0, False

            # Verify feature count matches model expectations
            if hasattr(self.model, 'n_features_in_') and X.shape[1] != self.model.n_features_in_:
                logger.warning(f"Feature count mismatch: model expects {self.model.n_features_in_} features, got {X.shape[1]}. Model needs retraining.")
                self.needs_retraining = True
                return False, 0.0, False

            # Handle NaN values before scaling
            X = np.nan_to_num(X, nan=0.0)

            # Scale the features
            X_scaled = self.scaler.transform(X)

            # Handle any NaNs that might appear after scaling
            X_scaled = np.nan_to_num(X_scaled, nan=0.0)

            # Get anomaly scores (lower scores indicate more anomalous)
            scores = self.model.score_samples(X_scaled)
            anomaly_score = float(scores[0])  # Convert to float for JSON serialization

            # Log feature values for debugging
            logger.debug(f"Feature values for prediction: {dict(zip(self.feature_names, X[0]))}")
            logger.debug(f"Scaled feature values: {dict(zip(self.feature_names, X_scaled[0]))}")
            logger.debug(f"Anomaly score: {anomaly_score:.3f}")

            # Determine if this is an anomaly based on the score
            # Based on the logs, normal scores are around -0.572, while potential anomalies are in the range of -0.69 to -0.72
            # Setting an extremely conservative threshold to minimize false positives
            is_anomaly = anomaly_score < -0.75  # Much more conservative threshold

            # Use a stricter threshold for determining if an anomaly is critical
            # This helps reduce false positives while maintaining detection of real issues
            is_critical = False
            if is_anomaly:
                # Only proceed with criticality check if it's an anomaly in the first place
                # Extract feature values from the processed data
                critical_points = 0

                # Find indices of critical features
                cpu_high_index = -1
                memory_high_index = -1
                overall_rate_index = -1
                volatility_index = -1
                cpu_trend_index = -1
                memory_trend_index = -1

                for i, feature_name in enumerate(self.feature_names):
                    if feature_name == "cpu_utilization_high":
                        cpu_high_index = i
                    elif feature_name == "memory_utilization_high":
                        memory_high_index = i
                    elif feature_name == "overall_rate":
                        overall_rate_index = i
                    elif feature_name == "volatility":
                        volatility_index = i
                    elif feature_name == "cpu_trend":
                        cpu_trend_index = i
                    elif feature_name == "memory_trend":
                        memory_trend_index = i

                # Only extremely anomalous scores get high points
                if anomaly_score < -0.85:  # Significantly more negative to be considered critical
                    critical_points += 3
                elif anomaly_score < -0.80:  # Very conservative threshold
                    critical_points += 2
                elif anomaly_score < -0.75:  # Conservative threshold
                    critical_points += 1

                # Much stricter CPU utilization thresholds
                if cpu_high_index >= 0 and cpu_high_index < X.shape[1]:
                    if X[0, cpu_high_index] > 0.95:  # Increased threshold to 95%
                        critical_points += 2
                    elif X[0, cpu_high_index] > 0.90:  # Increased threshold to 90%
                        critical_points += 1

                # Much stricter memory utilization thresholds
                if memory_high_index >= 0 and memory_high_index < X.shape[1]:
                    if X[0, memory_high_index] > 0.95:  # Increased threshold to 95%
                        critical_points += 2
                    elif X[0, memory_high_index] > 0.90:  # Increased threshold to 90%
                        critical_points += 1

                # Much stricter rate of change thresholds
                if overall_rate_index >= 0 and overall_rate_index < X.shape[1]:
                    if abs(X[0, overall_rate_index]) > 3.0:  # Significant rate of change
                        critical_points += 2
                    elif abs(X[0, overall_rate_index]) > 2.5:  # High rate of change
                        critical_points += 1

                # Stricter volatility threshold
                if volatility_index >= 0 and volatility_index < X.shape[1]:
                    if X[0, volatility_index] > 0.90:  # Increased threshold
                        critical_points += 1

                # Upward trends in CPU/memory usage add criticality (1 point each)
                if cpu_trend_index >= 0 and cpu_trend_index < X.shape[1]:
                    if X[0, cpu_trend_index] > 0.15:  # Increased threshold
                        critical_points += 1

                if memory_trend_index >= 0 and memory_trend_index < X.shape[1]:
                    if X[0, memory_trend_index] > 0.15:  # Increased threshold
                        critical_points += 1

                # Need at least 5 points to consider it critical (increased from 4)
                # This significantly reduces false positives by requiring more evidence
                is_critical = critical_points >= 5

            logger.info(f"Prediction for entity: anomaly={is_anomaly}, score={anomaly_score:.3f}, critical={is_critical}")
            return is_anomaly, anomaly_score, is_critical

        except NotFittedError:
            logger.error("Model or scaler not fitted. Cannot make predictions.")
            self.needs_retraining = True
            return False, 0.0, False
        except Exception as e:
            logger.error(f"Error making prediction: {e}", exc_info=True)
            return False, 0.0, False

    def get_model_parameters(self) -> Dict[str, Any]:
        """
        Get the current model parameters.

        Returns:
            Dictionary containing model parameters including status and configuration
        """
        try:
            if not self.model:
                return {"status": "not_trained"}

            return {
                "status": "trained",
                "model_type": "IsolationForest",
                "contamination": self.model.contamination,
                "n_estimators": self.model.n_estimators,
                "max_samples": self.model.max_samples,
                "expected_features": len(self.feature_names),
                "scaler_fitted": hasattr(self.scaler, 'mean_'),
                "random_state": self.model.random_state
            }

        except Exception as e:
            logger.error(f"Error getting model parameters: {e}")
            raise

    async def evaluate_model_with_gemini(self, metrics: dict) -> bool:
        """
        Use Gemini to evaluate if the model should be kept based on its metrics.

        Args:
            metrics: Dictionary of model performance metrics

        Returns:
            Boolean indicating if the new model should be kept
        """
        try:
            # Format metrics for Gemini analysis
            metrics_str = json.dumps(metrics, indent=2)
            prompt = f"""
            Compare these model metrics with the current best metrics:
            Current Model: {metrics_str}
            Best Model: {json.dumps(self.best_metrics, indent=2)}

            Consider:
            1. Overall performance improvement
            2. Balance between precision and recall
            3. F1 score improvement
             4. Any significant improvements in specific metrics

            Should we keep this model? Respond with only 'yes' or 'no'.
            """

            response = await self.gemini_service.model.generate_content_async(prompt)
            decision = response.text.strip().lower()

            if decision == "yes":
                logger.info("Gemini recommended keeping the new model")
                return True
            else:
                logger.info("Gemini recommended keeping the existing model")
                return False
        except Exception as e:
            logger.error(f"Error in model evaluation: {str(e)}")
            return False

    async def forecast_metrics(self, entity_metrics: List[Dict[str, Any]], forecast_minutes: int = 60) -> Dict[str, Any]:
        """
        Forecast future metric values based on historical trends.

        This method provides true predictive capability by forecasting metrics into the future
        and estimating when they might cross critical thresholds.

        Args:
            entity_metrics: Historical metrics for an entity
            forecast_minutes: How far into the future (in minutes) to forecast

        Returns:
            Dictionary containing forecast results including:
                - forecasted_values: Dict of metric_name -> list of forecasted values
                - threshold_crossings: Dict of metric_name -> estimated minutes until crossing threshold
                - forecast_confidence: Confidence score of the forecast (0-1)
                - risk_assessment: Overall risk assessment based on forecasts
        """
        try:
            if not entity_metrics:
                logger.warning("No metrics provided for forecasting.")
                return {
                    "success": False,
                    "error": "No metrics provided for forecasting"
                }

            # Extract time series data for forecastable metrics
            metric_series = {}
            timestamps = {}
            forecast_results = {
                "forecasted_values": {},
                "threshold_crossings": {},
                "forecast_confidence": 0.0,
                "risk_assessment": "low"
            }

            # Extract available time series from different metric types
            for metric_data in entity_metrics:
                values = metric_data.get("values", [])
                if not values or len(values) < 5:  # Need enough data points for forecasting
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
                ts = [float(val[0]) for val in values if isinstance(val, list) and len(val) == 2]
                vals = [float(val[1]) for val in values if isinstance(val, list) and len(val) == 2]

                if len(ts) >= 5 and len(vals) >= 5:  # Only forecast if we have enough data
                    metric_series[metric_name] = vals
                    timestamps[metric_name] = ts

            # Forecast each metric separately
            overall_confidence = []
            risk_scores = []

            for metric_name, values in metric_series.items():
                ts = timestamps[metric_name]
                forecast = self._forecast_single_metric(values, ts, forecast_minutes)

                if forecast:
                    forecast_results["forecasted_values"][metric_name] = forecast["values"]

                    # Determine if/when the metric will cross a threshold
                    if metric_name == "cpu_usage":
                        threshold = 0.9  # 90% CPU usage
                        threshold_cross = self._detect_threshold_crossing(forecast["values"], threshold)
                    elif metric_name == "memory_usage":
                        threshold = 0.9  # 90% memory usage
                        threshold_cross = self._detect_threshold_crossing(forecast["values"], threshold)
                    elif metric_name == "disk_usage":
                        threshold = 0.85  # 85% disk usage
                        threshold_cross = self._detect_threshold_crossing(forecast["values"], threshold)
                    else:
                        threshold = None
                        threshold_cross = None

                    if threshold_cross:
                        forecast_results["threshold_crossings"][metric_name] = {
                            "threshold": threshold,
                            "minutes_until_crossing": threshold_cross["minutes"],
                            "confidence": threshold_cross["confidence"]
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
                forecast_results["forecast_confidence"] = sum(overall_confidence) / len(overall_confidence)

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

            return {
                "success": True,
                "forecast": forecast_results
            }

        except Exception as e:
            logger.error(f"Error forecasting metrics: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Forecasting error: {str(e)}"
            }

    def _forecast_single_metric(self, values: List[float], timestamps: List[float],
                                forecast_minutes: int) -> Dict[str, Any]:
        """
        Forecast a single metric time series.

        Uses simple linear regression for forecasting with exponential smoothing.
        For more complex patterns, this could be extended with ARIMA or other methods.

        Args:
            values: Historical values for the metric
            timestamps: Timestamps corresponding to the values
            forecast_minutes: Minutes to forecast into the future

        Returns:
            Dictionary with forecast values and confidence
        """
        try:
            # Clean data (remove NaN, Inf)
            clean_indices = [i for i, v in enumerate(values) if not (np.isnan(v) or np.isinf(v))]
            if not clean_indices or len(clean_indices) < 3:
                return None

            clean_values = [values[i] for i in clean_indices]
            clean_timestamps = [timestamps[i] for i in clean_indices]

            # Determine time interval between samples
            time_diffs = np.diff(clean_timestamps)
            avg_interval = np.mean(time_diffs) if len(time_diffs) > 0 else 60  # Default to 60 seconds

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
            fit_error = np.mean(np.abs(historical_fit - y) / (y + 1e-8))  # Add small epsilon to avoid div by 0
            confidence = max(0, min(1, 1 - fit_error))  # Higher error = lower confidence

            return {
                "values": linear_forecast.tolist(),
                "confidence": confidence
            }

        except Exception as e:
            logger.error(f"Error in single metric forecast: {e}")
            return None

    def _detect_threshold_crossing(self, forecasted_values: List[float], threshold: float) -> Dict[str, Any]:
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
                    minutes_until_crossing = i + 1  # Simplification, assuming each step is a minute

                    # Calculate confidence based on how close to threshold
                    # Values much higher than threshold = higher confidence in the crossing
                    confidence = min(1.0, max(0.5, (value - threshold) / threshold))

                    return {
                        "minutes": minutes_until_crossing,
                        "confidence": confidence
                    }

            # No threshold crossing detected
            return None

        except Exception as e:
            logger.error(f"Error detecting threshold crossing: {e}")
            return None

    async def predict_with_forecast(self, latest_metrics_snapshot: List[Dict[str, Any]],
                                    historical_snapshots: List[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Enhanced prediction that combines current anomaly detection with future forecasting.

        This method provides a more comprehensive assessment by both detecting current anomalies
        and predicting potential future issues.

        Args:
            latest_metrics_snapshot: Current metrics for anomaly detection
            historical_snapshots: Optional list of historical metric snapshots for better forecasting

        Returns:
            Dictionary containing:
                - is_anomaly: Whether current state is anomalous
                - anomaly_score: Current anomaly score
                - is_critical: Whether current state is critical
                - forecast_available: Whether forecast was possible
                - forecast: Forecast results if available
                - predicted_failures: List of predicted future failures with estimated time
                - recommendation: Suggested action based on current and forecasted state
        """
        try:
            # Get current anomaly state
            is_anomaly, anomaly_score, is_critical = await self.predict(latest_metrics_snapshot)

            result = {
                "is_anomaly": is_anomaly,
                "anomaly_score": anomaly_score,
                "is_critical": is_critical,
                "forecast_available": False,
                "prediction_timestamp": datetime.utcnow().isoformat()
            }

            # Combine historical snapshots with latest for better forecasting
            metric_history = []
            if historical_snapshots:
                for snapshot in historical_snapshots:
                    metric_history.extend(snapshot)

            metric_history.extend(latest_metrics_snapshot)

            # Only attempt forecasting if we have enough data
            if len(metric_history) >= 5:
                forecast_result = await self.forecast_metrics(metric_history)

                if (forecast_result and forecast_result.get("success", False)):
                    result["forecast_available"] = True
                    result["forecast"] = forecast_result["forecast"]

                    # Extract predictions about future failures
                    predicted_failures = []
                    for metric_name, crossing in forecast_result["forecast"].get("threshold_crossings", {}).items():
                        predicted_failures.append({
                            "metric": metric_name,
                            "threshold": crossing["threshold"],
                            "minutes_until_failure": crossing["minutes_until_crossing"],
                            "confidence": crossing["confidence"]
                        })

                    result["predicted_failures"] = predicted_failures

                    # Generate recommendation based on current state and forecast
                    if is_critical:
                        result["recommendation"] = "immediate_action"
                    elif is_anomaly or (predicted_failures and any(p["minutes_until_failure"] < 30 for p in predicted_failures)):
                        result["recommendation"] = "proactive_action"
                    elif predicted_failures:
                        result["recommendation"] = "monitor_closely"
                    else:
                        result["recommendation"] = "no_action_needed"

            return result

        except Exception as e:
            logger.error(f"Error in enhanced prediction: {e}", exc_info=True)
            return {
                "is_anomaly": is_anomaly if 'is_anomaly' in locals() else False,
                "anomaly_score": anomaly_score if 'anomaly_score' in locals() else 0.0,
                "is_critical": is_critical if 'is_critical' in locals() else False,
                "forecast_available": False,
                "error": str(e)
            }


# Create a global instance of the AnomalyDetector
anomaly_detector = AnomalyDetector()
logger.info("Global AnomalyDetector instance created")
