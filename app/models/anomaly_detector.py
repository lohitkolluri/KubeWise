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
        self.EXPECTED_NUM_FEATURES = 8  # Number of features the model expects
        self._load_model()

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
                logger.info(f"Loaded existing anomaly detection model and scaler from {self.best_model_path}")

                # Verify model has expected features
                if hasattr(self.model, 'n_features_in_'):
                    logger.info(f"Model expects {self.model.n_features_in_} features")
                    self.EXPECTED_NUM_FEATURES = self.model.n_features_in_
            else:
                logger.info("No existing model/scaler file found. Model needs training.")
        except KeyError as e:
            logger.error(f"Error loading model/scaler: File format mismatch ({e}). Needs retraining.")
            self.model = None
            self.scaler = StandardScaler()
        except Exception as e:
            logger.error(f"Error loading model/scaler: {e}")
            self.model = None
            self.scaler = StandardScaler()

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
            # Save both model and scaler in a dictionary to a single file
            saved_state = {'model': self.model, 'scaler': self.scaler}
            joblib.dump(saved_state, self.best_model_path)
            logger.info(f"Model and scaler saved to {self.best_model_path}")
        except Exception as e:
            logger.error(f"Error saving model and scaler: {e}")
            raise

    def _engineer_features_aggregate(self, data_snapshots: List[List[Dict[str, Any]]]) -> np.ndarray:
        """
        Engineers a single, fixed-length feature vector for each entity snapshot.
        Aggregates features across all metrics within a snapshot.

        Args:
            data_snapshots: A list where each element is a snapshot for one entity.
                            Each snapshot is a list of metric dicts:
                            [ # Snapshot 1 (for entity A)
                              [{"query": "q1", "values": [[ts,v],...]}, {"query": "q2", "values": [[ts,v],...]}],
                              # Snapshot 2 (for entity B)
                              [{"query": "q1", "values": [[ts,v],...]}, {"query": "q3", "values": [[ts,v],...]}],
                              ...
                            ]

        Returns:
            numpy array where each row is the aggregated feature vector for a snapshot.
        """
        all_feature_vectors = []

        for snapshot in data_snapshots:
            # --- Feature Extraction Logic ---
            # Default values for expected features
            cpu_usage_stats = [0.0] * 3  # mean, last, rate (default)
            memory_usage_stats = [0.0] * 2  # mean, last (default)
            restarts_total = 0.0  # last value (default)
            unavailable_replicas = 0.0
            failed_pods = 0.0

            for metric_data in snapshot:
                query = metric_data.get("query", "").lower()  # Normalize query string
                values = metric_data.get("values", [])
                if not values:
                    continue

                metric_values = []
                try:
                    metric_values = [float(val[1]) for val in values if isinstance(val, list) and len(val) == 2]
                except (ValueError, TypeError):
                    logger.warning(f"Skipping metric due to invalid values: {query}")
                    continue

                if not metric_values:
                    continue

                # --- Example Aggregation ---
                if 'container_cpu_usage_seconds_total' in query:
                    cpu_usage_stats[0] = np.mean(metric_values)
                    cpu_usage_stats[1] = metric_values[-1]
                    if len(metric_values) > 1:
                        rate = (metric_values[-1] - metric_values[0]) / max(1, len(metric_values))
                        cpu_usage_stats[2] = rate
                elif 'container_memory_working_set_bytes' in query:
                    memory_usage_stats[0] = np.mean(metric_values)
                    memory_usage_stats[1] = metric_values[-1]
                elif 'kube_pod_container_status_restarts_total' in query:
                    restarts_total = metric_values[-1]  # Just the latest count
                elif 'kube_deployment_status_replicas_unavailable' in query:
                    unavailable_replicas = max(unavailable_replicas, metric_values[-1])
                elif 'kube_pod_status_phase{phase="failed"}' in query:
                    failed_pods = max(failed_pods, metric_values[-1])

            # --- Combine extracted features into a single vector ---
            # This order MUST be consistent between training and prediction
            entity_feature_vector = []
            entity_feature_vector.extend(cpu_usage_stats)      # 3 features
            entity_feature_vector.extend(memory_usage_stats)   # 2 features
            entity_feature_vector.append(restarts_total)       # 1 feature
            entity_feature_vector.append(unavailable_replicas) # 1 feature
            entity_feature_vector.append(failed_pods)          # 1 feature

            # --- Padding / Length Consistency ---
            current_len = len(entity_feature_vector)
            if current_len < self.EXPECTED_NUM_FEATURES:
                logger.warning(f"Padding feature vector: expected {self.EXPECTED_NUM_FEATURES}, got {current_len}. Using 0.0 for padding.")
                entity_feature_vector.extend([0.0] * (self.EXPECTED_NUM_FEATURES - current_len))
            elif current_len > self.EXPECTED_NUM_FEATURES:
                logger.warning(f"Truncating feature vector: expected {self.EXPECTED_NUM_FEATURES}, got {current_len}.")
                entity_feature_vector = entity_feature_vector[:self.EXPECTED_NUM_FEATURES]

            all_feature_vectors.append(entity_feature_vector)

        if not all_feature_vectors:
            logger.warning("No features could be engineered from any snapshot.")
            # Return empty array matching expected dimensions for safety
            return np.empty((0, self.EXPECTED_NUM_FEATURES))

        return np.array(all_feature_vectors)

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

            # Train model
            self.model = IsolationForest(
                contamination='auto',  # Often works well
                random_state=42
                # You might want to tune other parameters like n_estimators, max_samples
            )
            self.model.fit(X_scaled)
            logger.info("IsolationForest model trained.")

            # Update expected features count
            self.EXPECTED_NUM_FEATURES = self.model.n_features_in_
            logger.info(f"Model trained with {self.EXPECTED_NUM_FEATURES} features")

            # Save model and scaler
            self.save_model()

        except Exception as e:
            logger.error(f"Error training model: {e}", exc_info=True)
            # Reset model/scaler on training error
            self.model = None
            self.scaler = StandardScaler()
            raise

    async def predict(self, latest_metrics_snapshot: List[Dict[str, Any]]) -> Tuple[bool, float]:
        """
        Predict if the given entity snapshot contains anomalies.

        Args:
            latest_metrics_snapshot: List of metric results for ONE entity

        Returns:
            Tuple containing:
                - Boolean indicating if an anomaly was detected
                - Float representing the anomaly score (lower is more anomalous)
        """
        try:
            if self.model is None:
                logger.warning("Model is not loaded or trained. Cannot predict. Returning default (False, 0.0).")
                return False, 0.0

            # Check if scaler is fitted
            if not hasattr(self.scaler, 'mean_'):
                logger.warning("Scaler is not fitted. Cannot predict. Returning default (False, 0.0).")
                return False, 0.0

            if not latest_metrics_snapshot:
                logger.warning("Empty metric snapshot provided for prediction. Returning default (False, 0.0).")
                return False, 0.0

            # Engineer features for the single entity snapshot
            # We wrap the snapshot in a list because _engineer_features_aggregate expects a list of snapshots
            X = self._engineer_features_aggregate([latest_metrics_snapshot])

            if X.size == 0:
                logger.warning("No features were engineered from the snapshot. Returning default (False, 0.0).")
                return False, 0.0

            # Check feature count consistency BEFORE scaling/predicting
            expected_features = self.model.n_features_in_
            if X.shape[1] != expected_features:
                logger.error(f"Feature mismatch: Engineered features ({X.shape[1]}) != Model expected features ({expected_features}). Check _engineer_features_aggregate logic or retrain model.")
                # Return default to prevent crashing
                return False, 0.0

            # Scale features using the scaler
            try:
                X_scaled = self.scaler.transform(X)  # Use transform, NOT fit_transform
            except NotFittedError:
                logger.error("Scaler is not fitted, cannot transform features. Model needs retraining or loading failed.")
                return False, 0.0
            except ValueError as e:
                logger.error(f"Error scaling features: {e}. Check feature consistency.")
                return False, 0.0

            # Get anomaly scores (negative scores indicate anomalies)
            scores = self.model.score_samples(X_scaled)
            anomaly_score = float(scores[0])  # Get the score for the single sample

            # Determine if anomaly (using threshold from settings)
            is_anomaly = anomaly_score < settings.ANOMALY_SCORE_THRESHOLD

            logger.debug(f"Prediction successful: is_anomaly={is_anomaly}, score={anomaly_score:.4f}")
            return is_anomaly, anomaly_score

        except Exception as e:
            logger.error(f"Unexpected error during prediction: {e}", exc_info=True)
            # Return a default value on error
            return False, 0.0

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
                "expected_features": self.EXPECTED_NUM_FEATURES,
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


# Create a global instance of the AnomalyDetector
anomaly_detector = AnomalyDetector()
logger.info("Global AnomalyDetector instance created")
