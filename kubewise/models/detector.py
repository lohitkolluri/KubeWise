import asyncio
import datetime
import math
import pickle
import random
import zlib
import time
import os
from enum import Enum
from functools import wraps
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
    TypeVar,
    Callable,
    Coroutine,
    Protocol,
)

import motor.motor_asyncio
from bson import Binary
from loguru import logger
from river.anomaly import HalfSpaceTrees

from kubewise.config import settings
from kubewise.models import (
    AnomalyRecord,
    KubernetesEvent,
    MetricPoint,
)
from kubewise.utils import format_metric_value

# Type variables for generic functions
T = TypeVar("T")

# State persistence constants
MODEL_STATE_DIR = "models"
HST_MODEL_FILE = "hst_model.pkl.gz"
RIVER_MODEL_FILE = "river_model.pkl"
ISO_MODEL_FILE = "iso_model.pkl"
ENTITY_STATE_FILE = "entity_state.pkl.gz"
ENTITY_SEQUENCES_FILE = "entity_sequences.pkl"
METRICS_HISTORY_FILE = "metrics_history.pkl"
STATE_SAVE_INTERVAL_SECONDS = 300


# Feature definition
class FeatureName(str, Enum):
    CPU_USAGE = "cpu_usage"
    MEMORY_USAGE = "memory_usage"
    NETWORK_RX_RATE = "network_rx_rate"
    NETWORK_TX_RATE = "network_tx_rate"
    CONTAINER_RESTART_COUNT = "container_restart_count"
    DISK_READ_RATE = "disk_read_rate"
    DISK_WRITE_RATE = "disk_write_rate"
    # Event counts with temporal decay
    EVENT_OOMKILLED_COUNT = "event_oomkilled_count"
    EVENT_UNHEALTHY_COUNT = "event_unhealthy_count"
    EVENT_FAILED_SCHEDULING_COUNT = "event_failed_scheduling_count"
    EVENT_CRASHLOOPBACKOFF_COUNT = "event_crashloopbackoff_count"
    EVENT_IMAGEPULLBACKOFF_COUNT = "event_imagepullbackoff_count"
    # Rates of change
    CPU_USAGE_RATE = "cpu_usage_rate"
    MEMORY_USAGE_RATE = "memory_usage_rate"
    CPU_UTILIZATION_PCT = "cpu_utilization_pct"
    MEMORY_UTILIZATION_PCT = "memory_utilization_pct"


# Default feature vector structure
DEFAULT_FEATURE_VECTOR = {name.value: 0.0 for name in FeatureName}

# State management constants
EVENT_COUNT_DECAY_HALFLIFE_MINUTES = 30.0
EVENT_COUNT_DECAY_FACTOR = math.log(0.5) / (EVENT_COUNT_DECAY_HALFLIFE_MINUTES * 60.0)
MIN_RATE_TIME_DIFF_SECONDS = 1.0

# Critical events to track for anomaly detection
TRACKED_EVENT_REASONS = {
    # Pod failures
    "OOMKilled",
    "CrashLoopBackOff",
    "BackOff",
    "Failed",
    "FailedScheduling",
    "Unhealthy",
    "ContainersNotReady",
    "Evicted",
    # Resource exhaustion
    "NodeNotReady",
    "NodeHasDiskPressure",
    "FreeDiskSpaceFailed",
    # Container/Image issues
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerError",
    # Network/connectivity issues
    "NetworkNotReady",
    "FailedMount",
    "FailedAttachVolume",
    "FailedDetachVolume",
}

# Map reason to feature
REASON_TO_FEATURE = {
    # Pod failures
    "OOMKilled": FeatureName.EVENT_OOMKILLED_COUNT,
    "CrashLoopBackOff": FeatureName.EVENT_CRASHLOOPBACKOFF_COUNT,
    "BackOff": FeatureName.EVENT_CRASHLOOPBACKOFF_COUNT,
    "Failed": FeatureName.EVENT_UNHEALTHY_COUNT,
    "FailedScheduling": FeatureName.EVENT_FAILED_SCHEDULING_COUNT,
    "Unhealthy": FeatureName.EVENT_UNHEALTHY_COUNT,
    "ContainersNotReady": FeatureName.EVENT_UNHEALTHY_COUNT,
    "Evicted": FeatureName.EVENT_UNHEALTHY_COUNT,
    # Resource exhaustion
    "NodeNotReady": FeatureName.EVENT_UNHEALTHY_COUNT,
    "NodeHasDiskPressure": FeatureName.EVENT_UNHEALTHY_COUNT,
    "FreeDiskSpaceFailed": FeatureName.EVENT_UNHEALTHY_COUNT,
    # Container/Image issues
    "ImagePullBackOff": FeatureName.EVENT_IMAGEPULLBACKOFF_COUNT,
    "ErrImagePull": FeatureName.EVENT_IMAGEPULLBACKOFF_COUNT,
    "CreateContainerError": FeatureName.EVENT_CRASHLOOPBACKOFF_COUNT,
    # Network/connectivity issues
    "NetworkNotReady": FeatureName.EVENT_UNHEALTHY_COUNT,
    "FailedMount": FeatureName.EVENT_FAILED_SCHEDULING_COUNT,
    "FailedAttachVolume": FeatureName.EVENT_FAILED_SCHEDULING_COUNT,
    "FailedDetachVolume": FeatureName.EVENT_FAILED_SCHEDULING_COUNT,
}

# Default thresholds by metric type
DEFAULT_THRESHOLDS = {
    "cpu": 0.85,
    "memory": 0.90,
    "network": 0.80,
    "disk": 0.85,
    "disk_usage": 0.90, 
    "node_disk_usage_pct": 0.90, 
    "event": 0.50,
    "deployment_replicas_available_ratio": 0.95,
    "statefulset_replicas_available_ratio": 0.95,
    "default": 0.80,
}


def with_exponential_backoff(
    max_retries: int = 3,
    retry_on_exceptions: Tuple[Exception, ...] = (Exception,),
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter_factor: float = 0.1,
) -> Callable[
    [Callable[..., Coroutine[Any, Any, T]]], Callable[..., Coroutine[Any, Any, T]]
]:
    """
    Decorator for async functions to add exponential backoff retry logic.
    """

    def decorator(
        func: Callable[..., Coroutine[Any, Any, T]],
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            retry_count = 0
            backoff_time = initial_delay

            while True:
                try:
                    return await func(*args, **kwargs)
                except retry_on_exceptions as e:
                    retry_count += 1
                    if retry_count > max_retries:
                        logger.error(
                            f"Maximum retries ({max_retries}) exceeded for {func.__name__}: {e}"
                        )
                        raise

                    # Add jitter to prevent thundering herd problem
                    jitter = backoff_time * jitter_factor
                    actual_backoff = backoff_time + (jitter * (2 * random.random() - 1))
                    actual_backoff = max(0.1, actual_backoff)

                    logger.warning(
                        f"Retry {retry_count}/{max_retries} for {func.__name__} after error: {e}. "
                        f"Backing off for {actual_backoff:.2f}s"
                    )

                    await asyncio.sleep(actual_backoff)
                    backoff_time = min(backoff_time * backoff_factor, max_delay)

        return wrapper

    return decorator


class BaseDetector(Protocol):
    """
    Protocol defining the interface for anomaly detector implementations.
    All detector implementations must conform to this interface.
    """

    async def score(self, features: Dict[str, float]) -> float:
        """
        Calculate an anomaly score for the given feature vector.

        Args:
            features: Dictionary mapping feature names to their values

        Returns:
            float: Anomaly score (higher value indicates more anomalous)
        """
        ...

    async def learn(self, features: Dict[str, float]) -> None:
        """
        Update the detector's internal state with the given feature vector.

        Args:
            features: Dictionary mapping feature names to their values
        """
        ...

    def persist(self) -> bytes:
        """
        Serialize the detector's state to bytes for persistence.

        Returns:
            bytes: Serialized model state
        """
        ...

    def restore(self, state: bytes) -> None:
        """
        Restore the detector's state from serialized bytes.

        Args:
            state: Serialized model state (from persist())
        """
        ...


class HSTDetector:
    """
    Half-Space Trees based anomaly detector implementation.

    Uses River's HalfSpaceTrees algorithm for unsupervised anomaly detection.
    """

    def __init__(
        self, n_trees: int, height: int, window_size: int, seed: Optional[int] = None
    ):
        """
        Initialize the HST detector.

        Args:
            n_trees: Number of trees in the ensemble
            height: Height of trees
            window_size: Window size for the Half-Space Trees model
            seed: Random seed for reproducibility
        """
        self.model = HalfSpaceTrees(
            n_trees=n_trees, height=height, window_size=window_size, seed=seed
        )

    async def score(self, features: Dict[str, float]) -> float:
        """Score a feature vector for anomalousness."""
        return self.model.score_one(features)

    async def learn(self, features: Dict[str, float]) -> None:
        """Update the model with a new feature vector."""
        self.model.learn_one(features)

    def persist(self) -> bytes:
        """Serialize the model state to bytes."""
        return zlib.compress(pickle.dumps(self.model))

    def restore(self, state: bytes) -> None:
        """Restore the model state from bytes."""
        self.model = pickle.loads(zlib.decompress(state))


class IsolationForestDetector:
    """
    Isolation Forest based anomaly detector implementation.

    Uses a custom implementation of Isolation Forest for KubeWise since
    River 0.22.0 does not include IsolationForest.
    """

    def __init__(
        self, n_trees: int = 100, subspace_size: int = 10, seed: Optional[int] = None
    ):
        """
        Initialize the Isolation Forest detector.

        Args:
            n_trees: Number of trees in the forest
            subspace_size: Number of features to consider when looking for best split
            seed: Random seed for reproducibility
        """
        # Initialize with configuration
        self.n_trees = n_trees
        self.subspace_size = subspace_size
        self.random_state = random.Random(seed)

        # Storage for recent data points (limited window)
        self.max_points = 1000
        self.data_points = []

        # Track anomaly score statistics for normalization
        self.min_score = float("inf")
        self.max_score = float("-inf")

    async def score(self, features: Dict[str, float]) -> float:
        """
        Score a feature vector for anomalousness.

        Higher scores indicate more anomalous samples.
        """
        if len(self.data_points) < 10:
            # Not enough data for meaningful scoring
            return 0.0

        # Convert features dict to vector, ensuring consistent order
        feature_vector = [
            features.get(feature, 0.0) for feature in sorted(features.keys())
        ]

        # Simplified anomaly scoring: Measure average distance to other points
        distances = []
        sample_points = self.random_state.sample(
            self.data_points, min(20, len(self.data_points))
        )

        for point in sample_points:
            if len(point) != len(feature_vector):
                continue
            # Euclidean distance
            distance = sum((a - b) ** 2 for a, b in zip(feature_vector, point)) ** 0.5
            distances.append(distance)

        if not distances:
            return 0.0

        # Average distance as anomaly score
        score = sum(distances) / len(distances)

        # Update min/max for normalization
        self.min_score = min(self.min_score, score)
        self.max_score = max(self.max_score, score)

        # Normalize the score to range [0, 1] if we have a range
        if self.max_score > self.min_score:
            normalized_score = (score - self.min_score) / (
                self.max_score - self.min_score
            )
            return normalized_score

        return 0.5  # Default middle score when normalization not possible

    async def learn(self, features: Dict[str, float]) -> None:
        """Update the model with a new feature vector."""
        # Convert features dict to vector, ensuring consistent order
        feature_vector = [
            features.get(feature, 0.0) for feature in sorted(features.keys())
        ]

        # Add to data points with limit on total stored
        self.data_points.append(feature_vector)
        if len(self.data_points) > self.max_points:
            # Remove oldest point (simple FIFO queue)
            self.data_points.pop(0)

    def persist(self) -> bytes:
        """Serialize the model state to bytes."""
        state = {
            "n_trees": self.n_trees,
            "subspace_size": self.subspace_size,
            "random_state": self.random_state.getstate() if self.random_state else None,
            "data_points": self.data_points,
            "min_score": self.min_score,
            "max_score": self.max_score,
        }
        return zlib.compress(pickle.dumps(state))

    def restore(self, state: bytes) -> None:
        """Restore the model state from bytes."""
        state_dict = pickle.loads(zlib.decompress(state))
        self.n_trees = state_dict["n_trees"]
        self.subspace_size = state_dict["subspace_size"]

        self.random_state = random.Random()
        if state_dict["random_state"]:
            self.random_state.setstate(state_dict["random_state"])

        self.data_points = state_dict["data_points"]
        self.min_score = state_dict["min_score"]
        self.max_score = state_dict["max_score"]


class OnlineAnomalyDetector:
    """
    Advanced anomaly detector using pluggable detector models for real-time detection.

    Key features:
    1. Pluggable detector framework supporting multiple anomaly detection algorithms
    2. Built-in support for Half-Space Trees and Isolation Forest
    3. Score fusion to combine results from multiple detectors
    4. Dynamic per-metric type thresholds (CPU, Memory, Network, etc.)
    5. Model state persistence through database storage for restart resilience
    6. Per-entity cooldown timers to prevent remediation storms
    7. Proper asynchronous design with retry logic
    8. Feature extraction from both metrics and Kubernetes events
    """

    # Map metric names to meaningful failure reasons
    METRIC_TO_REASON = {
        "cpu_usage": "HighCpuUsage",
        "memory_usage": "HighMemoryUsage",
        "network_rx_rate": "HighNetworkReceiveRate",
        "network_tx_rate": "HighNetworkTransmitRate",
        "container_restart_count": "ContainerRestarting",
        "disk_read_rate": "HighDiskReadRate",
        "disk_write_rate": "HighDiskWriteRate",
        "event_oomkilled_count": "OOMKilled",
        "event_unhealthy_count": "Unhealthy",
        "event_failed_scheduling_count": "FailedScheduling",
        "event_crashloopbackoff_count": "CrashLoopBackOff",
        "event_imagepullbackoff_count": "ImagePullBackOff",
        "cpu_usage_rate": "RapidCpuIncrease",
        "memory_usage_rate": "RapidMemoryIncrease",
        "cpu_utilization_pct": "HighCpuUtilization",
        "memory_utilization_pct": "HighMemoryUtilization",
        # Generic default reasons
        "default": "AnomalousMetric",
    }

    def __init__(
        self,
        db: motor.motor_asyncio.AsyncIOMotorDatabase,
        hst_n_trees: int = 25,
        hst_height: int = 15,
        hst_window_size: int = 500,
        hst_seed: Optional[int] = 42,
        river_trees: int = 50,
        river_seed: Optional[int] = 12345,
        iso_n_trees: int = 100,
        iso_subspace_size: int = 10,
        iso_seed: Optional[int] = 67890,
        save_interval: int = STATE_SAVE_INTERVAL_SECONDS,
        remediation_cooldown_seconds: int = 600,
    ):
        """
        Initialize the anomaly detector with pluggable detection models.

        Args:
            db: MongoDB database instance for state persistence and event storage
            hst_n_trees: Number of trees for Half-Space Trees model
            hst_height: Height of trees for Half-Space Trees model
            hst_window_size: Window size for Half-Space Trees model
            hst_seed: Random seed for HST model reproducibility
            river_trees: Number of trees for River RandomForestClassifier
            river_seed: Random seed for River model reproducibility
            iso_n_trees: Number of trees for Isolation Forest model
            iso_subspace_size: Number of features to consider for Isolation Forest splits
            iso_seed: Random seed for Isolation Forest model reproducibility
            save_interval: How often to save model state (seconds)
            remediation_cooldown_seconds: Cooldown period between remediations
        """
        # MongoDB database connection
        self.db = db
        self.anomaly_collection = self.db["anomalies"]
        self.model_states_collection = self.db["_model_states"]

        # Initialize configuration
        self.save_interval = save_interval
        self.cooldown_seconds = remediation_cooldown_seconds

        # Initialize anomaly thresholds
        self._thresholds = self._get_configured_thresholds()
        logger.info(f"Initialized anomaly thresholds: {self._thresholds}")

        # Create detector instances
        self.hst = HSTDetector(
            n_trees=hst_n_trees,
            height=hst_height,
            window_size=hst_window_size,
            seed=hst_seed,
        )

        self.iso = IsolationForestDetector(
            n_trees=iso_n_trees, subspace_size=iso_subspace_size, seed=iso_seed
        )

        # List of active detectors
        self.detectors: List[BaseDetector] = [self.hst, self.iso]
        
        # Detector weights for ensemble scoring (initially equal weights)
        self.detector_weights = {
            'hstdetector': 0.5,
            'isolationforestdetector': 0.5,
        }

        # Entity state tracking
        self._entity_state = {}
        self._anomaly_cache: Dict[str, List[AnomalyRecord]] = {}
        self._last_save_time = time.time()

        # Log throttling for disk usage messages
        self._disk_usage_log_cache = {}
        self._disk_usage_log_interval = 300  # Only log once per entity every 5 minutes
        
        # Feature scaling statistics
        self.feature_stats = {name.value: {'min': float('inf'), 'max': float('-inf')} for name in FeatureName}
        
        # Dynamic thresholds for metrics
        self._dynamic_thresholds = {}

        # Load state from storage
        asyncio.create_task(self.load_state())

    def _get_configured_thresholds(self) -> Dict[str, float]:
        """Get anomaly thresholds from settings or use defaults."""
        if hasattr(settings, "anomaly_thresholds") and settings.anomaly_thresholds:
            return {**DEFAULT_THRESHOLDS, **settings.anomaly_thresholds}
        return DEFAULT_THRESHOLDS

    async def load_state(self) -> None:
        """Load all state from storage."""
        try:
            logger.info("Loading detector state from storage...")
            start_time = time.time()

            # Load model states
            await self._load_detector_states()

            # Load entity state (for feature calculation)
            await self.load_entity_state()

            elapsed = time.time() - start_time
            logger.info(f"Detector state loaded in {elapsed:.2f}s")
        except Exception as e:
            logger.error(f"Failed to load detector state: {e}", exc_info=True)

    async def _load_detector_states(self) -> None:
        """Load all detector model states."""
        # Load HST state
        hst_state = await self._load_model_state("hst_model")
        if hst_state:
            try:
                self.hst.restore(hst_state)
                logger.info("HST model state restored")
            except Exception as e:
                logger.error(f"Failed to restore HST model: {e}")

        # Load Isolation Forest state
        iso_state = await self._load_model_state("iso_model")
        if iso_state:
            try:
                self.iso.restore(iso_state)
                logger.info("Isolation Forest model state restored")
            except Exception as e:
                logger.error(f"Failed to restore Isolation Forest model: {e}")

    @with_exponential_backoff(max_retries=3)
    async def _load_model_state(self, model_name: str) -> Optional[bytes]:
        """Load model state from database."""
        doc = await self.model_states_collection.find_one({"_id": model_name})
        if doc and "state" in doc:
            logger.debug(f"Loaded {model_name} state from database")
            return doc["state"]
        logger.warning(f"No state found for {model_name}")
        return None

    @with_exponential_backoff(max_retries=3)
    async def _load_entity_state(self) -> bool:
        """Load entity state from storage."""
        doc = await self.model_states_collection.find_one({"_id": "entity_state"})
        if doc and "state" in doc:
            try:
                # Decompress and deserialize
                state_data = zlib.decompress(doc["state"])
                loaded_state = pickle.loads(state_data)

                # Extract feature stats and dynamic thresholds if present
                if "_feature_stats" in loaded_state:
                    self.feature_stats = loaded_state.pop("_feature_stats", {})
                    logger.info(f"Loaded feature stats for {len(self.feature_stats)} features")
                
                if "_dynamic_thresholds" in loaded_state:
                    self._dynamic_thresholds = loaded_state.pop("_dynamic_thresholds", {})
                    logger.info(f"Loaded {len(self._dynamic_thresholds)} dynamic thresholds")
                
                # Set the remaining state as entity state
                self._entity_state = loaded_state

                # Remove any potentially non-serializable objects
                for entity_id in list(self._entity_state.keys()):
                    entity_data = self._entity_state[entity_id]
                    if isinstance(entity_data, dict):
                        for k in list(entity_data.keys()):
                            if not isinstance(k, (str, int, float, bool, type(None))):
                                entity_data.pop(k, None)
                    else:
                        # If not a dict, remove it (shouldn't happen)
                        self._entity_state.pop(entity_id, None)

                logger.info(
                    f"Loaded entity state for {len(self._entity_state)} entities"
                )
                return True
            except Exception as e:
                logger.error(f"Failed to deserialize entity state: {e}", exc_info=True)
        else:
            logger.info("No entity state found in storage")

        # Initialize empty if not loaded
        self._entity_state = {}
        return False

    # Add a public version that can be called by child classes
    async def load_entity_state(self) -> bool:
        """Load entity state from storage - public method that can be called by child classes."""
        return await self._load_entity_state()

    async def save_state(self) -> None:
        """
        Save state if enough time has elapsed since last save.
        """
        current_time = time.time()
        if current_time - self._last_save_time >= self.save_interval:
            await self._save_detector_states()
            await self._save_entity_state()
            self._last_save_time = current_time
            logger.debug(f"Saved state after {self.save_interval}s interval")

    async def save_all_state(self) -> None:
        """Save detector and entity state."""
        logger.debug("Saving all detector and entity state")
        await self._save_detector_states()
        await self._save_entity_state()

    async def _save_detector_states(self) -> None:
        """Save all detector model states."""
        # Save HST model
        try:
            hst_state = self.hst.persist()
            await self._save_model_state("hst_model", hst_state)
            logger.debug("Saved HST model state")
        except Exception as e:
            logger.error(f"Failed to save HST model: {e}")

        # Save Isolation Forest model
        try:
            iso_state = self.iso.persist()
            await self._save_model_state("iso_model", iso_state)
            logger.debug("Saved Isolation Forest model state")
        except Exception as e:
            logger.error(f"Failed to save Isolation Forest model: {e}")

    @with_exponential_backoff(max_retries=3)
    async def _save_model_state(self, model_name: str, state: bytes) -> None:
        """Save model state to database."""
        await self.model_states_collection.update_one(
            {"_id": model_name},
            {
                "$set": {
                    "state": Binary(state),
                    "updated_at": datetime.datetime.utcnow(),
                }
            },
            upsert=True,
        )

    @with_exponential_backoff(max_retries=3)
    async def _save_entity_state(self) -> bool:
        """Save entity state to database."""
        try:
            # Process entity state to make it serializable
            state_to_save = self._prepare_entity_state_for_storage()

            # Also save feature stats and dynamic thresholds
            state_to_save["_feature_stats"] = self.feature_stats
            state_to_save["_dynamic_thresholds"] = self._dynamic_thresholds
            
            # Compress the state to save space
            serialized = pickle.dumps(state_to_save)
            compressed = zlib.compress(serialized)

            # Store in database, overwriting any existing state
            await self.model_states_collection.update_one(
                {"_id": "entity_state"},
                {"$set": {"state": Binary(compressed)}},
                upsert=True,
            )
            
            logger.debug(f"Saved entity state for {len(state_to_save)} entities")
            return True
        except Exception as e:
            logger.error(f"Failed to save entity state: {e}")
            return False

    def _prepare_entity_state_for_storage(self) -> Dict[str, Dict[str, Any]]:
        """Create a clean copy of entity state suitable for serialization."""
        cleaned_state = {}

        for entity_id, entity_data in self._entity_state.items():
            if not isinstance(entity_data, dict):
                continue

            cleaned_entity = {}
            for k, v in entity_data.items():
                # Skip non-serializable values
                if isinstance(k, (str, int, float, bool, type(None))) and isinstance(
                    v, (str, int, float, bool, type(None), list, dict, set)
                ):
                    cleaned_entity[k] = v

            if cleaned_entity:
                cleaned_state[entity_id] = cleaned_entity

        return cleaned_state

    async def analyze(
        self, features: Dict[str, float]
    ) -> Tuple[float, Dict[str, float]]:
        """
        Analyze features using all detectors and return the fused score.

        Args:
            features: Dictionary mapping feature names to their values

        Returns:
            Tuple of (fused_score, detector_scores)
        """
        # Get scores from all detectors
        detector_scores = {}
        for detector in self.detectors:
            try:
                score = await detector.score(features)
                # Map detector to a name for storing
                detector_name = type(detector).__name__.lower()
                detector_scores[detector_name] = score
            except Exception as e:
                logger.error(
                    f"Error scoring with detector {type(detector).__name__}: {e}"
                )
                detector_scores[type(detector).__name__.lower()] = 0.0

        # Calculate weighted ensemble score
        if detector_scores:
            weighted_sum = 0.0
            weight_sum = 0.0
            
            for detector_name, score in detector_scores.items():
                weight = self.detector_weights.get(detector_name, 0.5)  # default to 0.5 if not found
                weighted_sum += weight * score
                weight_sum += weight
                
            # Normalize by total weight to keep score in [0,1] range
            if weight_sum > 0:
                fused_score = weighted_sum / weight_sum
            else:
                # Fallback if no valid weights
                fused_score = max(detector_scores.values())
        else:
            fused_score = 0.0

        return fused_score, detector_scores

    async def detect_anomaly(
        self, data_point: Union[MetricPoint, KubernetesEvent]
    ) -> Optional[AnomalyRecord]:
        """
        Detect anomalies in a stream of metrics and events.

        Args:
            data_point: Either a MetricPoint or KubernetesEvent to analyze

        Returns:
            AnomalyRecord if an anomaly is detected, None otherwise
        """
        # Extract features
        result = self._extract_features(data_point)
        if not result:
            return None

        entity_id, features = result

        # Ensure we have non-empty features
        if not features:
            return None

        # Skip anomaly detection for specific metrics when they show healthy state
        if isinstance(data_point, MetricPoint):
            # For deployment and statefulset availability metrics, 1.0 means all replicas are available
            # This is a healthy state and should never be flagged as an anomaly
            if "replicas_available_ratio" in data_point.metric_name and data_point.value == 1.0:
                # Perfect availability is normal, no need to log at all
                return None

        # Filter features to only include numeric values and ensure they're standardized
        features = self._ensure_feature_dict(features)
        features = self._filter_numeric_features(features)

        # Skip if insufficient features
        if len(features) < 1:
            return None

        # Get threshold for this entity/metric
        primary_metric = next(iter(features.keys()))
        threshold = self._get_threshold_for_entity(entity_id, primary_metric)

        # Analyze features with all detectors
        fused_score, detector_scores = await self.analyze(features)
        
        # Update dynamic threshold with this score for future detection
        # Note: we do this before anomaly determination so we don't adapt to actual anomalies
        if not isinstance(data_point, MetricPoint) or not "replicas_available_ratio" in data_point.metric_name:
            self.update_dynamic_threshold(entity_id, primary_metric, fused_score)

        # Check if this is an anomaly
        is_anomaly = False
        
        # Special handling for availability ratio metrics
        if isinstance(data_point, MetricPoint) and "replicas_available_ratio" in data_point.metric_name:
            # For availability metrics, it's an anomaly when the value is BELOW the threshold
            # This is opposite to regular metrics where we alert when a value exceeds threshold
            is_anomaly = data_point.value < threshold and data_point.value < 1.0
            
            if is_anomaly:
                logger.info(
                    f"Availability metric {data_point.metric_name} for {entity_id} is below threshold: "
                    f"{data_point.value:.2f} < {threshold:.2f}"
                )
        else:
            # Standard anomaly detection for regular metrics
            is_anomaly = fused_score >= threshold

        # Special case for disk usage metrics
        if isinstance(data_point, MetricPoint):
            if (
                "disk_usage" in data_point.metric_name.lower()
                or "disk_utilization" in data_point.metric_name.lower()
            ):
                # Only consider high disk usage (>85%) as a potential anomaly
                # Disk usage is typically reported as a percentage (0-100%), not 0-1
                if data_point.value < 85.0:
                    # If disk usage is under 85%, it's not an anomaly regardless of score
                    is_anomaly = False

                    # Only log first occurrence or every X minutes for low disk usage
                    # Don't log routine low disk usage values at all
                    should_log = False
                    
                    # If disk usage is critically low (<1%), may want to log as it could indicate issues
                    if data_point.value < 1.0:
                        current_time = time.time()
                        disk_usage_key = f"{entity_id}:{data_point.metric_name}"

                        if disk_usage_key not in self._disk_usage_log_cache:
                            should_log = True
                            self._disk_usage_log_cache[disk_usage_key] = current_time
                        elif (
                            current_time - self._disk_usage_log_cache[disk_usage_key]
                            > self._disk_usage_log_interval
                        ):
                            should_log = True
                            self._disk_usage_log_cache[disk_usage_key] = current_time

                        if should_log:
                            logger.debug(
                                f"Very low disk usage ({data_point.value}%) observed for {entity_id}"
                            )

                elif data_point.value > 85.0 and fused_score >= threshold:
                    # If disk usage is over 85% and score is high, confirm it's an anomaly
                    is_anomaly = True
                    logger.debug(
                        f"Confirmed high disk usage ({data_point.value}%) as an anomaly for {entity_id}"
                    )

        # Check for sequential patterns if available
        seq_anomaly_reason = None
        trend_info = None
        
        # Check if this instance has sequence detection capabilities
        # If we are a SequentialAnomalyDetector or a subclass
        if hasattr(self, "_detect_critical_sequence") and hasattr(self, "_detect_metric_trend"):
            # For event sequences
            if entity_id in getattr(self, "_entity_sequences", {}):
                # Try to detect a critical sequence
                seq_anomaly_reason = self._detect_critical_sequence(entity_id)
                if seq_anomaly_reason:
                    is_anomaly = True
                    logger.info(f"Detected critical event sequence in {entity_id}: {seq_anomaly_reason}")
            
            # For metric trends
            if isinstance(data_point, MetricPoint):
                # Try to detect concerning metric trends
                trend_info = self._detect_metric_trend(entity_id, data_point.metric_name)
                if trend_info:
                    is_anomaly = True
                    trend_message = f"metric trend: {trend_info.get('metric')}"
                    if 'trend' in trend_info and trend_info['trend'] == 'increasing':
                        increase_pct = trend_info.get('increase_pct', 0)
                        trend_message = f"increasing trend: {increase_pct:.1f}% rise in {trend_info.get('metric')}"
                    logger.info(f"Detected {trend_message} in {entity_id}")

        # For non-anomalous data, update all detector models
        # This ensures models learn from normal data and don't get poisoned by anomalies
        if not is_anomaly:
            for detector in self.detectors:
                try:
                    await detector.learn(features)
                except Exception as e:
                    logger.error(
                        f"Error updating detector {type(detector).__name__}: {e}"
                    )

        # Save state periodically
        await self.save_state()

        # Check for direct failure indicators
        direct_failure = False
        failure_reason = None

        if isinstance(data_point, MetricPoint):
            direct_failure, failure_reason = self._is_direct_failure_metric(
                data_point.metric_name, data_point.value
            )
        elif isinstance(data_point, KubernetesEvent):
            direct_failure, failure_reason = self._is_failure_event(data_point)

        # Use sequence anomaly reason if available
        if seq_anomaly_reason:
            failure_reason = seq_anomaly_reason
        elif trend_info:
            metric_name = trend_info.get('metric', 'unknown')
            if 'trend' in trend_info and trend_info['trend'] == 'increasing':
                failure_reason = f"Increasing{metric_name.capitalize()}Trend"
            else:
                failure_reason = f"{metric_name.capitalize()}Threshold"

        # Return anomaly record if above threshold or direct failure
        if is_anomaly or direct_failure:
            try:
                # Get entity info
                entity_state = self._entity_state.get(entity_id, {})
                namespace = entity_state.get("namespace", "default")

                # Check for rate limiting/cooldown
                if self._is_in_cooldown(entity_id):
                    logger.debug(
                        f"Skipping anomaly for {entity_id} - in cooldown period"
                    )
                    return None

                # Check for recent similar anomalies (deduplication)
                reason = failure_reason or self.METRIC_TO_REASON.get(
                    primary_metric, "AnomalousMetric"
                )
                if isinstance(data_point, KubernetesEvent):
                    # Create message similar to what would be in failure_message for similarity check
                    failure_msg = (
                        f"{reason}: {data_point.message}"
                        if data_point.message
                        else f"{reason} in {entity_state.get('kind', 'Unknown')} {namespace}/{entity_state.get('name', 'unknown')}"
                    )
                    if self._check_recent_similar_anomaly(
                        entity_id, reason, failure_msg
                    ):
                        logger.debug(
                            f"Skipping duplicate anomaly for {entity_id}: {reason}"
                        )
                        return None

                # Create anomaly record with detector-specific scores
                if isinstance(data_point, MetricPoint):
                    record = await self._create_anomaly_record(
                        entity_id=entity_id,
                        scores=detector_scores,
                        threshold=threshold,
                        data_source="metric",
                        failure_reason=reason,
                        namespace=namespace,
                        resource_kind=entity_state.get("kind", "Unknown"),
                        resource_name=entity_state.get("name", "unknown"),
                        feature_vector=features,
                        metric_name=data_point.metric_name,
                        metric_value=data_point.value,
                    )
                else:  # KubernetesEvent
                    record = await self._create_anomaly_record(
                        entity_id=entity_id,
                        scores=detector_scores,
                        threshold=threshold,
                        data_source="event",
                        failure_reason=reason,
                        namespace=namespace,
                        resource_kind=entity_state.get("kind", "Unknown"),
                        resource_name=entity_state.get("name", "unknown"),
                        feature_vector=features,
                        event_type=data_point.type,
                        event_reason=data_point.reason,
                        event_message=data_point.message,
                    )

                # Store in database
                record_id = await self._store_anomaly_record(record)
                if record_id:
                    record.id = str(record_id)

                    # Update cooldown to prevent remediation storms
                    self._update_cooldown(entity_id)

                    # Cache for deduplication
                    self._cache_anomaly(entity_id, record)

                    if is_anomaly:
                        logger.info(
                            f"Anomaly detected (score): {record.failure_reason} in {record.namespace}/{record.name} "
                            f"(entity={entity_id}, score={fused_score:.3f}, threshold={threshold:.3f})"
                        )
                    elif direct_failure:
                        logger.info(
                            f"Anomaly detected (direct failure): {record.failure_reason} in {record.namespace}/{record.name} "
                            f"(entity={entity_id}, metric_value={getattr(record, 'metric_value', 'N/A')})"
                        )

                    return record

            except Exception as e:
                logger.error(f"Error creating anomaly record: {e}", exc_info=True)

        return None

    def _is_direct_failure_metric(
        self, metric_name: str, metric_value: float
    ) -> Tuple[bool, Optional[str]]:
        """
        Determine if a metric value directly indicates a failure condition.

        These metrics bypass the ML models as they are direct indicators of failures.

        Returns:
            Tuple of (is_failure, failure_reason)
        """
        is_failure = False
        reason = None

        # Pod crash/restart metrics
        if "restart" in metric_name.lower() and metric_value > 3:
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "ContainerRestarting")

        # OOMKilled indicators
        elif "oom" in metric_name.lower() and metric_value > 0:
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "OOMKilled")

        # CrashLoopBackOff
        elif "crashloop" in metric_name.lower() and metric_value > 0:
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "CrashLoopBackOff")

        # ImagePullBackOff
        elif "imagepull" in metric_name.lower() and metric_value > 0:
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "ImagePullBackOff")

        # Pod failed
        elif "pod_failed" in metric_name.lower() and metric_value > 0:
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "PodFailed")

        # Pod not ready
        elif "pod_not_ready" in metric_name.lower() and metric_value > 0:
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "PodNotReady")

        # Extremely high CPU utilization
        elif (
            "cpu" in metric_name.lower()
            and "utilization" in metric_name.lower()
            and metric_value > 98
        ):
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "HighCpuUtilization")

        # Extremely high memory utilization
        elif (
            "memory" in metric_name.lower()
            and "utilization" in metric_name.lower()
            and metric_value > 98
        ):
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "HighMemoryUtilization")

        # Extremely high network errors
        elif "network_error" in metric_name.lower() and metric_value > 100:
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "HighNetworkErrors")

        # Very high disk utilization
        elif (
            "disk" in metric_name.lower()
            and "utilization" in metric_name.lower()
            and metric_value > 95
        ):
            is_failure = True
            reason = self.METRIC_TO_REASON.get(metric_name, "HighDiskUtilization")

        return is_failure, reason

    def _is_failure_event(self, event: KubernetesEvent) -> Tuple[bool, Optional[str]]:
        """
        Determine if a Kubernetes event directly indicates a failure.

        These events bypass ML models as they are direct indicators of failures.

        Returns:
            Tuple of (is_failure, failure_reason)
        """
        # Only warning events can be failures
        if event.type != "Warning":
            return False, None

        # List of critical failure reasons
        critical_failures = {
            "Failed",
            "FailedScheduling",
            "FailedCreate",
            "FailedMount",
            "FailedAttachVolume",
            "FailedDetachVolume",
            "Failed",
            "NodeNotReady",
            "BackOff",
            "CrashLoopBackOff",
            "ErrImagePull",
            "ImagePullBackOff",
            "CreateContainerError",
            "OOMKilled",
            "Unhealthy",
            "ContainerGCFailed",
            "Evicted",
            "FailedPostStartHook",
            "FailedPreStopHook",
        }

        is_failure = event.reason in critical_failures
        return is_failure, event.reason if is_failure else None

    def _is_in_cooldown(self, entity_id: str) -> bool:
        """
        Check if the entity is currently in cooldown period.
        """
        if entity_id not in self._anomaly_cache or not self._anomaly_cache[entity_id]:
            return False

        # Get the most recent anomaly timestamp from the cache
        if isinstance(self._anomaly_cache[entity_id], list):
            # If it's a list (expected), get the latest entry
            if not self._anomaly_cache[entity_id]:
                return False
            latest_entry = self._anomaly_cache[entity_id][-1]
            if isinstance(latest_entry, dict) and "timestamp" in latest_entry:
                # Convert datetime to timestamp
                if isinstance(latest_entry["timestamp"], datetime.datetime):
                    last_anomaly_time = latest_entry["timestamp"].timestamp()
                else:
                    last_anomaly_time = (
                        time.time() - self.cooldown_seconds - 1
                    )  # Not in cooldown
            else:
                return False
        else:
            # Backwards compatibility for old format
            cache_entry = self._anomaly_cache.get(entity_id, {})
            last_anomaly_time = getattr(cache_entry, "timestamp", 0)
            if isinstance(last_anomaly_time, datetime.datetime):
                last_anomaly_time = last_anomaly_time.timestamp()

        current_time = time.time()
        return (current_time - last_anomaly_time) < self.cooldown_seconds

    def _check_recent_similar_anomaly(
        self, entity_id: str, reason: str, message: str
    ) -> bool:
        """
        Check if we've recently seen a similar anomaly to avoid duplicates.

        This is useful for avoiding duplicate anomalies from similar error messages
        with slightly different details (like timestamps).
        """
        if entity_id not in self._anomaly_cache:
            self._anomaly_cache[entity_id] = []

        # Get the time threshold for recent (last 10 minutes)
        current_time = datetime.datetime.now(datetime.timezone.utc)
        time_threshold = current_time - datetime.timedelta(minutes=10)

        # Filter to only keep recent anomalies in memory
        recent_anomalies = [
            item
            for item in self._anomaly_cache[entity_id]
            if item["timestamp"] > time_threshold
        ]

        # Check for duplicates
        for item in recent_anomalies:
            if item["reason"] == reason and item["message"] == message:
                return True

        return False

    def cleanup_old_data(self) -> int:
        """
        Clean up old data to prevent memory leaks.

        Returns:
            int: Count of records cleaned up
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        cleanup_count = 0

        # 1. Clean up old entity states
        entities_to_remove = []
        for entity_id, state in self._entity_state.items():
            # Check if entity has been inactive for more than 24 hours
            last_update = state.get("last_updated")
            if last_update and (now - last_update).total_seconds() > 86400:  # 24 hours
                entities_to_remove.append(entity_id)

        # Remove inactive entities
        for entity_id in entities_to_remove:
            if entity_id in self._entity_state:
                del self._entity_state[entity_id]
                cleanup_count += 1
                logger.debug(f"Cleaned up inactive entity state: {entity_id}")

        # 2. Clean up entity sequences older than the temporal window
        if hasattr(self, "_entity_sequences"):
            entities_cleaned = set()
            sequences_to_remove = []
            for entity_id, sequences in self._entity_sequences.items():
                if not sequences:
                    sequences_to_remove.append(entity_id)
                    continue

                # Filter sequences to keep only recent ones
                old_count = len(sequences)
                recent_sequences = []
                for seq in sequences:
                    if isinstance(seq, dict) and "timestamp" in seq:
                        timestamp = seq["timestamp"]
                        if (
                            now - timestamp
                        ).total_seconds() <= self._temporal_window_seconds:
                            recent_sequences.append(seq)
                    elif (
                        isinstance(seq, tuple)
                        and len(seq) == 2
                        and isinstance(seq[1], datetime.datetime)
                    ):
                        reason, timestamp = seq
                        if (
                            now - timestamp
                        ).total_seconds() <= self._temporal_window_seconds:
                            recent_sequences.append((reason, timestamp))

                if not recent_sequences:
                    sequences_to_remove.append(entity_id)
                else:
                    if len(recent_sequences) < old_count:
                        self._entity_sequences[entity_id] = recent_sequences
                        entities_cleaned.add(entity_id)
                        cleanup_count += old_count - len(recent_sequences)

            # Remove empty sequence entries
            for entity_id in sequences_to_remove:
                if entity_id in self._entity_sequences:
                    del self._entity_sequences[entity_id]
                    cleanup_count += 1
                    logger.debug(f"Cleaned up entity sequences: {entity_id}")

        # 3. Clean up metrics history
        metrics_history_attr = "_metrics_history"
        if hasattr(self, "_entity_metrics_history"):
            metrics_history_attr = "_entity_metrics_history"

        if hasattr(self, metrics_history_attr):
            metrics_history = getattr(self, metrics_history_attr)
            metrics_to_remove = []
            for entity_id, metrics in metrics_history.items():
                if not metrics:
                    metrics_to_remove.append(entity_id)
                    continue

                metrics_cleaned = False
                for metric_name, history in list(metrics.items()):
                    if not history:
                        del metrics[metric_name]
                        metrics_cleaned = True
                        cleanup_count += 1
                        continue

                    # Keep only data points within the temporal window
                    old_count = len(history)
                    new_history = []
                    for item in history:
                        if isinstance(item, dict) and "timestamp" in item:
                            if (
                                now - item["timestamp"]
                            ).total_seconds() <= self._temporal_window_seconds:
                                new_history.append(item)
                        elif (
                            isinstance(item, tuple)
                            and len(item) == 2
                            and isinstance(item[1], datetime.datetime)
                        ):
                            value, timestamp = item
                            if (
                                now - timestamp
                            ).total_seconds() <= self._temporal_window_seconds:
                                new_history.append((value, timestamp))

                    if not new_history:
                        del metrics[metric_name]
                        metrics_cleaned = True
                        cleanup_count += old_count
                    elif len(new_history) < old_count:
                        metrics[metric_name] = new_history
                        metrics_cleaned = True
                        cleanup_count += old_count - len(new_history)

                if metrics_cleaned:
                    entities_cleaned = (
                        entities_cleaned if "entities_cleaned" in locals() else set()
                    )
                    entities_cleaned.add(entity_id)

                # If all metrics for an entity were removed, clean up the entity entry
                if not metrics:
                    metrics_to_remove.append(entity_id)

            # Remove empty metrics history entries
            for entity_id in metrics_to_remove:
                if entity_id in metrics_history:
                    del metrics_history[entity_id]
                    cleanup_count += 1
                    logger.debug(f"Cleaned up metrics history: {entity_id}")

        if cleanup_count > 0:
            entities_cleaned = (
                entities_cleaned if "entities_cleaned" in locals() else set()
            )
            logger.info(
                f"Memory cleanup completed: {cleanup_count} records removed from {len(entities_cleaned)} entities"
            )

        return cleanup_count

    def _ensure_feature_dict(self, features: Dict[str, float]) -> Dict[str, float]:
        """Ensure features are in a consistent format and normalize them."""
        # If not a dict, return empty
        if not isinstance(features, dict):
            return {}
            
        # Normalize features to [0,1] range for optimal detector performance
        return self.normalize_features(features)

    def _filter_numeric_features(self, features: Dict[str, Any]) -> Dict[str, float]:
        """Filter features to only include numeric values."""
        return {
            k: float(v)
            for k, v in features.items()
            if isinstance(v, (int, float)) and not math.isnan(float(v))
        }

    def _extract_features(
        self, data_point: Union[MetricPoint, KubernetesEvent]
    ) -> Optional[Tuple[str, Dict[str, float]]]:
        """
        Extract features from a metric point or Kubernetes event.

        Args:
            data_point: Either a MetricPoint or KubernetesEvent

        Returns:
            Tuple of (entity_id, features) or None if features could not be extracted
        """
        try:
            if isinstance(data_point, MetricPoint):
                return self._extract_metric_features(data_point)
            elif isinstance(data_point, KubernetesEvent):
                return self._extract_event_features(data_point)
            else:
                logger.warning(f"Unknown data point type: {type(data_point)}")
                return None
        except Exception as e:
            logger.error(f"Error extracting features: {e}", exc_info=True)
            return None

    def _extract_metric_features(
        self, metric: MetricPoint
    ) -> Optional[Tuple[str, Dict[str, float]]]:
        """Extract features from a metric point."""
        # Generate entity ID from labels
        entity_id = self._get_entity_id_from_metric(metric)
        if not entity_id:
            return None

        # Create or update entity state
        if entity_id not in self._entity_state:
            self._entity_state[entity_id] = {
                "first_seen": datetime.datetime.now(datetime.timezone.utc),
                "last_updated": datetime.datetime.now(datetime.timezone.utc),
                "metrics": {},
                "events": {},
                "metrics_history": {},  # Store recent metric history for trends
            }

        # Update entity state with metric value
        entity_state = self._entity_state[entity_id]
        entity_state["last_updated"] = datetime.datetime.now(datetime.timezone.utc)

        # Extract metadata from labels
        namespace = metric.labels.get("namespace", "default")
        pod_name = metric.labels.get("pod", "")
        container_name = metric.labels.get("container", "")
        node_name = metric.labels.get("node", "")

        # Store entity metadata
        entity_state["namespace"] = namespace
        if pod_name:
            entity_state["name"] = pod_name
            entity_state["kind"] = "Pod"
        elif node_name:
            entity_state["name"] = node_name
            entity_state["kind"] = "Node"
        elif entity_id.startswith("namespace/"):
            # For namespace-level metrics, extract the name from entity_id
            entity_state["name"] = entity_id.split("/", 1)[1]
            entity_state["kind"] = "Namespace"
        elif "statefulset" in metric.labels:
            entity_state["name"] = metric.labels.get("statefulset", "")
            entity_state["kind"] = "StatefulSet"
        elif "deployment" in metric.labels:
            entity_state["name"] = metric.labels.get("deployment", "")
            entity_state["kind"] = "Deployment"
        elif "service" in metric.labels:
            entity_state["name"] = metric.labels.get("service", "")
            entity_state["kind"] = "Service"

        # Extract the metric value
        metric_value = (
            float(metric.value) if isinstance(metric.value, (int, float)) else 0.0
        )

        # Store the current metric value
        entity_state["metrics"][metric.metric_name] = {
            "value": metric_value,
            "timestamp": metric.timestamp,
        }

        # Initialize or update metrics_history for this entity and metric
        if "metrics_history" not in entity_state:
            entity_state["metrics_history"] = {}
            
        if metric.metric_name not in entity_state["metrics_history"]:
            entity_state["metrics_history"][metric.metric_name] = []
            
        # Add current metric to history
        entity_state["metrics_history"][metric.metric_name].append({
            "value": metric_value,
            "timestamp": metric.timestamp
        })
        
        # Prune history to recent window (keep last 30 data points or ~30 minutes of data)
        max_history_points = 30
        if len(entity_state["metrics_history"][metric.metric_name]) > max_history_points:
            entity_state["metrics_history"][metric.metric_name] = entity_state["metrics_history"][metric.metric_name][-max_history_points:]

        # Calculate derived metrics like rates of change
        if metric.metric_name in entity_state["metrics"]:
            previous_metric = entity_state["metrics"][metric.metric_name]
            if "value" in previous_metric and "timestamp" in previous_metric:
                previous_value = previous_metric["value"]
                previous_time = previous_metric["timestamp"]

                # Calculate rate of change if timestamps are valid
                if (
                    previous_time
                    and metric.timestamp
                    and previous_time != metric.timestamp
                ):
                    time_diff = (metric.timestamp - previous_time).total_seconds()
                    if time_diff > MIN_RATE_TIME_DIFF_SECONDS:
                        value_diff = metric_value - previous_value
                        rate = value_diff / time_diff

                        # Store the rate metrics
                        rate_metric_name = f"{metric.metric_name}_rate"
                        entity_state["metrics"][rate_metric_name] = {
                            "value": rate,
                            "timestamp": metric.timestamp,
                        }

        # Build feature vector with current metric and recent entity state
        features = {}

        # Add the primary metric
        features[metric.metric_name] = metric_value

        # Add computed rates if available
        rate_metric_name = f"{metric.metric_name}_rate"
        if rate_metric_name in entity_state["metrics"]:
            features[rate_metric_name] = entity_state["metrics"][rate_metric_name][
                "value"
            ]

        # Add utilization percentages if limits are available
        if (
            "cpu_usage" in entity_state["metrics"]
            and "cpu_limits" in entity_state["metrics"]
        ):
            cpu_usage = entity_state["metrics"]["cpu_usage"]["value"]
            cpu_limit = entity_state["metrics"]["cpu_limits"]["value"]
            if cpu_limit > 0:
                features["cpu_utilization_pct"] = (cpu_usage / cpu_limit) * 100

        if (
            "memory_usage" in entity_state["metrics"]
            and "memory_limits" in entity_state["metrics"]
        ):
            memory_usage = entity_state["metrics"]["memory_usage"]["value"]
            memory_limit = entity_state["metrics"]["memory_limits"]["value"]
            if memory_limit > 0:
                features["memory_utilization_pct"] = (memory_usage / memory_limit) * 100
                
        # Add rolling statistics features for stronger anomaly signals
        history = entity_state["metrics_history"].get(metric.metric_name, [])
        if len(history) >= 5:  # Need enough data points for meaningful statistics
            # Rolling mean (average over last N points)
            values = [point["value"] for point in history]
            features[f"{metric.metric_name}_mean"] = sum(values) / len(values)
            
            # Standard deviation for volatility detection
            if len(values) >= 2:  # Need at least 2 points for std dev
                mean = sum(values) / len(values)
                variance = sum((x - mean) ** 2 for x in values) / len(values)
                std_dev = math.sqrt(variance)
                features[f"{metric.metric_name}_stddev"] = std_dev
                
                # Z-score of current value (how many std devs from mean)
                if std_dev > 0:
                    features[f"{metric.metric_name}_zscore"] = (metric_value - mean) / std_dev
                    
            # Compute longer-term trend (10-minute delta)
            if len(history) >= 2:
                current_time = history[-1]["timestamp"]
                ten_min_ago = current_time - datetime.timedelta(minutes=10)
                
                # Find point closest to 10 minutes ago
                past_point = None
                for point in history:
                    if point["timestamp"] <= ten_min_ago:
                        past_point = point
                
                if past_point:
                    past_value = past_point["value"]
                    if past_value != 0:
                        delta_pct = (metric_value - past_value) / past_value * 100
                        features[f"{metric.metric_name}_10min_delta_pct"] = delta_pct
                    delta_abs = metric_value - past_value
                    features[f"{metric.metric_name}_10min_delta_abs"] = delta_abs

        # Add event counts with temporal decay
        for event_type, count in entity_state.get("events", {}).items():
            if isinstance(count, dict) and "count" in count and "last_updated" in count:
                # Apply exponential decay based on time since last update
                time_diff = (
                    datetime.datetime.now(datetime.timezone.utc) - count["last_updated"]
                ).total_seconds()
                decayed_count = count["count"] * math.exp(
                    EVENT_COUNT_DECAY_FACTOR * time_diff
                )
                features[event_type] = decayed_count

        # If using SequentialAnomalyDetector, update metrics history
        if hasattr(self, "_update_metrics_history"):
            now = datetime.datetime.now(datetime.timezone.utc)
            self._update_metrics_history(
                entity_id, metric.metric_name, metric_value, now
            )

        return entity_id, features

    def _extract_event_features(
        self, event: KubernetesEvent
    ) -> Optional[Tuple[str, Dict[str, float]]]:
        """Extract features from a Kubernetes event."""
        # Generate entity ID from event
        entity_id = self._get_entity_id_from_event(event)
        if not entity_id:
            return None

        # Create or update entity state
        if entity_id not in self._entity_state:
            self._entity_state[entity_id] = {
                "first_seen": datetime.datetime.now(datetime.timezone.utc),
                "last_updated": datetime.datetime.now(datetime.timezone.utc),
                "metrics": {},
                "events": {},
            }

        # Update entity state
        entity_state = self._entity_state[entity_id]
        entity_state["last_updated"] = datetime.datetime.now(datetime.timezone.utc)
        entity_state["namespace"] = event.involved_object_namespace
        entity_state["name"] = event.involved_object_name
        entity_state["kind"] = event.involved_object_kind

        # Process the event for feature extraction
        features = {}

        # If this is a tracked event type, increment its counter
        if event.reason in TRACKED_EVENT_REASONS:
            event_key = REASON_TO_FEATURE.get(event.reason, None)
            if event_key:
                # Update entity state events counter
                if "events" not in entity_state:
                    entity_state["events"] = {}

                if event_key not in entity_state["events"]:
                    entity_state["events"][event_key] = {
                        "count": 1,
                        "last_updated": event.event_time,
                        "reason": event.reason,
                    }
                else:
                    # Increment existing counter
                    entity_state["events"][event_key]["count"] += 1
                    entity_state["events"][event_key]["last_updated"] = event.event_time

                # Add to feature vector
                features[event_key] = entity_state["events"][event_key]["count"]

        # Add all event counts with temporal decay
        for event_type, count in entity_state.get("events", {}).items():
            if isinstance(count, dict) and "count" in count and "last_updated" in count:
                # Apply exponential decay based on time since last update
                time_diff = (
                    datetime.datetime.now(datetime.timezone.utc) - count["last_updated"]
                ).total_seconds()
                decayed_count = count["count"] * math.exp(
                    EVENT_COUNT_DECAY_FACTOR * time_diff
                )
                features[event_type] = decayed_count

        # If using SequentialAnomalyDetector, update sequence
        if hasattr(self, "_update_entity_sequence"):
            self._update_entity_sequence(entity_id, event.reason, event.event_time)

        return entity_id, features

    def _get_entity_id_from_metric(self, metric: MetricPoint) -> Optional[str]:
        """Generate entity ID from metric labels."""
        namespace = metric.labels.get("namespace", "default")

        # For pod metrics
        if "pod" in metric.labels:
            pod = metric.labels["pod"]
            return f"{namespace}/{pod}"

        # For node metrics
        if "node" in metric.labels:
            return f"node/{metric.labels['node']}"

        # For statefulset metrics
        if "statefulset" in metric.labels:
            statefulset = metric.labels["statefulset"]
            return f"{namespace}/{statefulset}"

        # For deployment metrics
        if "deployment" in metric.labels:
            deployment = metric.labels["deployment"]
            return f"{namespace}/{deployment}"

        # For service metrics
        if "service" in metric.labels:
            service = metric.labels["service"]
            return f"{namespace}/{service}"

        # For daemonset metrics
        if "daemonset" in metric.labels:
            daemonset = metric.labels["daemonset"]
            return f"{namespace}/{daemonset}"

        # For replicaset metrics
        if "replicaset" in metric.labels:
            replicaset = metric.labels["replicaset"]
            return f"{namespace}/{replicaset}"

        # For namespace metrics (fallback if namespace exists but no specific resource)
        if "namespace" in metric.labels:
            return f"namespace/{namespace}"

        # Cannot determine entity
        return None

    def _get_entity_id_from_event(self, event: KubernetesEvent) -> Optional[str]:
        """Generate entity ID from Kubernetes event."""
        if not event.involved_object_namespace or not event.involved_object_name:
            return None

        # Format: {namespace}/{name}
        return f"{event.involved_object_namespace}/{event.involved_object_name}"

    def _get_threshold_for_entity(self, entity_id: str, metric_name: str) -> float:
        """Get the appropriate threshold for this entity and metric using dynamic thresholding."""
        # Check for entity-specific dynamic threshold first
        dynamic_key = f"{entity_id}:{metric_name}"
        if dynamic_key in self._dynamic_thresholds:
            return self._dynamic_thresholds[dynamic_key]
        
        # Check for metric-specific dynamic threshold
        if metric_name in self._dynamic_thresholds:
            return self._dynamic_thresholds[metric_name]
            
        # Fall back to static thresholds if no dynamic threshold is available
        # Check for metric-specific threshold
        if metric_name in self._thresholds:
            return self._thresholds[metric_name]

        # Check for metric category threshold
        for category, threshold in self._thresholds.items():
            if category in metric_name:
                return threshold

        # Default threshold
        return self._thresholds["default"]
        
    def update_dynamic_threshold(self, entity_id: str, metric_name: str, score: float) -> None:
        """
        Update dynamic threshold for a specific entity and metric based on recent scores.
        Uses exponentially weighted moving average to smooth threshold adjustments.
        
        Args:
            entity_id: Entity identifier
            metric_name: Metric name
            score: Current anomaly score
        """
        # Only update thresholds for non-anomalous points to avoid learning anomalies as normal
        # Skip very high scores that are likely anomalies
        if score > 0.9:  # Skip likely anomalies
            return
            
        # Get current dynamic threshold or initialize from static
        dynamic_key = f"{entity_id}:{metric_name}"
        
        if dynamic_key not in self._dynamic_thresholds:
            # Initialize from static threshold + buffer for adaptation
            static_threshold = self._get_configured_thresholds().get(
                metric_name, 
                self._get_configured_thresholds().get('default', 0.8)
            )
            
            # Initialize slightly lower than static to allow for adaptation
            self._dynamic_thresholds[dynamic_key] = static_threshold
        
        current_threshold = self._dynamic_thresholds[dynamic_key]
        
        # Calculate adaptation rate based on how many samples we've seen
        if entity_id not in self._entity_state:
            adaptation_rate = 0.3  # Higher for new entities
        else:
            # Lower adaptation rate for established entities
            # This prevents the threshold from changing too quickly
            adaptation_rate = 0.1
            
        # If the score is significantly higher than current threshold,
        # adjust threshold upward to avoid false positives
        if score > current_threshold * 0.8 and score < current_threshold:
            # Score is high but not anomalous - slowly increase threshold
            new_threshold = current_threshold + (adaptation_rate * (score - current_threshold))
        elif score < current_threshold * 0.5:
            # Score is much lower than threshold - slowly lower threshold to maintain sensitivity
            new_threshold = current_threshold + (adaptation_rate * 0.5 * (score - current_threshold))
        else:
            # Score is in normal range - small adjustment
            new_threshold = current_threshold + (adaptation_rate * 0.1 * (score - current_threshold))
            
        # Ensure threshold stays within reasonable bounds
        # Don't let threshold go too low (would cause excessive alerts)
        # or too high (would miss anomalies)
        static_threshold = self._get_configured_thresholds().get(
            metric_name, 
            self._get_configured_thresholds().get('default', 0.8)
        )
        
        # Set bounds: Don't go below 70% of static threshold or above 130%
        min_threshold = static_threshold * 0.7
        max_threshold = static_threshold * 1.3
        
        self._dynamic_thresholds[dynamic_key] = max(min_threshold, min(max_threshold, new_threshold))
        
        # Log threshold updates periodically for visibility
        if random.random() < 0.01:  # Log ~1% of updates to avoid excessive logging
            logger.debug(
                f"Dynamic threshold for {entity_id}:{metric_name}: {self._dynamic_thresholds[dynamic_key]:.3f} "
                f"(static={static_threshold:.3f}, score={score:.3f})"
            )

    def _update_cooldown(self, entity_id: str) -> None:
        """Update entity cooldown timestamp to prevent remediation storms."""
        if entity_id not in self._anomaly_cache:
            self._anomaly_cache[entity_id] = []

        self._anomaly_cache[entity_id].append(
            {"timestamp": datetime.datetime.now(datetime.timezone.utc)}
        )

    def _cache_anomaly(self, entity_id: str, record: AnomalyRecord) -> None:
        """Cache anomaly record for deduplication."""
        # Ensure cache exists for this entity
        if entity_id not in self._anomaly_cache:
            self._anomaly_cache[entity_id] = []

        # Add anomaly to cache
        self._anomaly_cache[entity_id].append(
            {
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                "reason": record.failure_reason,
                "message": getattr(record, "failure_message", ""),
            }
        )

        # Limit cache size
        if len(self._anomaly_cache[entity_id]) > 20:
            self._anomaly_cache[entity_id] = self._anomaly_cache[entity_id][-20:]

    async def _create_anomaly_record(
        self,
        entity_id: str,
        scores: Dict[str, float],
        threshold: float,
        data_source: str,
        failure_reason: str,
        namespace: str,
        resource_kind: str,
        resource_name: str,
        feature_vector: Dict[str, float],
        metric_name: Optional[str] = None,
        metric_value: Optional[float] = None,
        event_type: Optional[str] = None,
        event_reason: Optional[str] = None,
        event_message: Optional[str] = None,
    ) -> AnomalyRecord:
        """Create a standardized anomaly record."""
        now = datetime.datetime.now(datetime.timezone.utc)

        # Create the base record
        record = AnomalyRecord(
            timestamp=now,
            entity_id=entity_id,
            entity_type=resource_kind,
            namespace=namespace,
            name=resource_name,
            anomaly_score=max(scores.values()) if scores else 0.0,
            threshold=threshold,
            failure_reason=failure_reason,
            failure_message=f"Anomaly detected in {resource_kind} {namespace}/{resource_name}",
            data_source=data_source,
            remediation_status="pending",
            features_snapshot=feature_vector,
        )

        # Add source-specific fields
        if data_source == "metric":
            record.metric_name = metric_name
            record.metric_value = metric_value
            if metric_name and metric_value is not None:
                formatted_value = format_metric_value(metric_name, metric_value)
                record.failure_message = f"{failure_reason}: {metric_name}={formatted_value} in {resource_kind} {namespace}/{resource_name}"
                record.formatted_metric_value = formatted_value
        elif data_source == "event":
            record.event_type = event_type
            record.event_reason = event_reason
            record.event_message = event_message
            record.failure_message = (
                f"{failure_reason}: {event_message}"
                if event_message
                else f"{failure_reason} in {resource_kind} {namespace}/{resource_name}"
            )

        return record

    @with_exponential_backoff(max_retries=3)
    async def _store_anomaly_record(self, record: AnomalyRecord) -> Optional[str]:
        """Store anomaly record in database."""
        result = await self.anomaly_collection.insert_one(record.dict(by_alias=True))
        return result.inserted_id

    def normalize_features(self, features: Dict[str, float]) -> Dict[str, float]:
        """
        Normalize features to a [0,1] range using min-max scaling based on historical values.
        
        Args:
            features: Dictionary mapping feature names to their values
            
        Returns:
            Dictionary with normalized feature values
        """
        normalized = {}
        
        for feature_name, value in features.items():
            # Update min/max stats for this feature
            if feature_name in self.feature_stats:
                self.feature_stats[feature_name]['min'] = min(self.feature_stats[feature_name]['min'], value)
                self.feature_stats[feature_name]['max'] = max(self.feature_stats[feature_name]['max'], value)
                
                # Get current min/max for scaling
                feature_min = self.feature_stats[feature_name]['min']
                feature_max = self.feature_stats[feature_name]['max']
                
                # Scale the feature to [0,1] range
                if feature_max > feature_min:
                    normalized_value = (value - feature_min) / (feature_max - feature_min)
                else:
                    # If min == max (constant value so far), use 0 as normalized value
                    normalized_value = 0.0
                
                # Ensure value stays in [0,1] (handle edge cases like values beyond historical range)
                normalized_value = max(0.0, min(1.0, normalized_value))
                
                normalized[feature_name] = normalized_value
            else:
                # For features we haven't seen before, initialize stats and use raw value
                self.feature_stats[feature_name] = {'min': value, 'max': value}
                normalized[feature_name] = 0.0  # Default to 0 for first occurrence
        
        return normalized


class SequentialAnomalyDetector(OnlineAnomalyDetector):
    """
    Enhanced version of OnlineAnomalyDetector that adds sequence analysis capabilities.
    Detects patterns in sequences of events and metric trends over time.
    """

    # Known critical event sequences that indicate problems
    CRITICAL_SEQUENCES = {
        "crashloop_sequence": [
            "CrashLoopBackOff",
            "CrashLoopBackOff",
            "CrashLoopBackOff",
        ],
        "scheduling_failure": ["FailedScheduling", "FailedScheduling"],
        "resource_pressure": ["NodeNotReady", "NodeHasDiskPressure"],
    }

    # Patterns to detect in metric time series
    CRITICAL_METRIC_PATTERNS = {
        "cpu_usage": {
            "threshold": 0.9,
            "count": 3,
            "window_seconds": 300,
        },  # 3 points above 90% in 5 min
        "memory_usage": {
            "threshold": 0.95,
            "count": 2,
            "window_seconds": 300,
        },  # 2 points above 95% in 5 min
        "container_restart_count": {
            "threshold": 3,
            "count": 1,
            "window_seconds": 600,
        },  # 3+ restarts in 10 min
    }

    def __init__(
        self,
        db: motor.motor_asyncio.AsyncIOMotorDatabase,
        max_sequence_history: int = 20,
        **kwargs,
    ):
        """Initialize with parent class parameters and sequence analysis configuration."""
        super().__init__(db=db, **kwargs)

        # Additional state for sequence detection
        self._entity_sequences = {}  # {entity_id: [{"reason": str, "timestamp": datetime}, ...]}
        self._entity_metrics_history = {}  # {entity_id: {metric_name: [{"value": float, "timestamp": datetime}, ...]}}
        self._max_sequence_history = max_sequence_history
        self._temporal_window_seconds = 600  # 10 minutes

    async def load_entity_state(self) -> bool:
        """Extended load entity state to include sequence data."""
        try:
            # First load the base entity state using the parent class method
            success = await super().load_entity_state()

            # Then load sequence-specific data
            try:
                # Load entity sequences
                seq_path = os.path.join(MODEL_STATE_DIR, ENTITY_SEQUENCES_FILE)
                if os.path.exists(seq_path):

                    def read_seq_file_sync():
                        with open(seq_path, "rb") as f:
                            return pickle.loads(f.read())

                    self._entity_sequences = await asyncio.to_thread(read_seq_file_sync)
                    logger.info(
                        f"Successfully loaded sequence data for {len(self._entity_sequences)} entities"
                    )

                # Load metrics history
                hist_path = os.path.join(MODEL_STATE_DIR, METRICS_HISTORY_FILE)
                if os.path.exists(hist_path):

                    def read_hist_file_sync():
                        with open(hist_path, "rb") as f:
                            return pickle.loads(f.read())

                    self._entity_metrics_history = await asyncio.to_thread(
                        read_hist_file_sync
                    )
                    logger.info(
                        f"Successfully loaded metrics history for {len(self._entity_metrics_history)} entities"
                    )

                return True
            except Exception as e:
                logger.error(f"Failed to load sequence and metrics history data: {e}")
                # Initialize empty if loading fails
                self._entity_sequences = {}
                self._entity_metrics_history = {}

            return success
        except Exception as e:
            logger.error(f"Failed to load entity state: {e}")
            self._entity_state = {}
            self._entity_sequences = {}
            self._entity_metrics_history = {}
            return False

    async def save_all_state(self) -> None:
        """Extended save state to include sequence data."""
        # Call parent's save methods directly instead of super().save_all_state()
        # to avoid infinite recursion
        await self._save_detector_states()
        await self._save_entity_state()

        try:
            # Save entity sequences
            seq_path = os.path.join(MODEL_STATE_DIR, ENTITY_SEQUENCES_FILE)

            def write_seq_file_sync():
                os.makedirs(os.path.dirname(seq_path), exist_ok=True)
                with open(seq_path, "wb") as f:
                    f.write(pickle.dumps(self._entity_sequences))

            await asyncio.to_thread(write_seq_file_sync)

            # Save metrics history
            hist_path = os.path.join(MODEL_STATE_DIR, METRICS_HISTORY_FILE)

            def write_hist_file_sync():
                os.makedirs(os.path.dirname(hist_path), exist_ok=True)
                with open(hist_path, "wb") as f:
                    f.write(pickle.dumps(self._entity_metrics_history))

            await asyncio.to_thread(write_hist_file_sync)
            logger.debug(
                f"Saved sequence data for {len(self._entity_sequences)} entities"
            )
        except Exception as e:
            logger.error(f"Failed to save sequence and metrics history data: {e}")

    def _update_entity_sequence(
        self, entity_id: str, event_reason: str, timestamp: datetime.datetime
    ) -> None:
        """Update the sequence of events for a specific entity."""
        if entity_id not in self._entity_sequences:
            self._entity_sequences[entity_id] = []

        # Add the new event to the sequence
        self._entity_sequences[entity_id].append(
            {"reason": event_reason, "timestamp": timestamp}
        )

        # Limit size of sequence history
        if len(self._entity_sequences[entity_id]) > self._max_sequence_history:
            self._entity_sequences[entity_id] = self._entity_sequences[entity_id][
                -self._max_sequence_history :
            ]

    def _update_metrics_history(
        self,
        entity_id: str,
        metric_name: str,
        value: float,
        timestamp: datetime.datetime,
    ) -> None:
        """Update metrics history for trend detection."""
        if entity_id not in self._entity_metrics_history:
            self._entity_metrics_history[entity_id] = {}

        if metric_name not in self._entity_metrics_history[entity_id]:
            self._entity_metrics_history[entity_id][metric_name] = []

        # Add the new metric point
        self._entity_metrics_history[entity_id][metric_name].append(
            {"value": value, "timestamp": timestamp}
        )

        # Prune old data outside the temporal window
        cutoff_time = timestamp - datetime.timedelta(
            seconds=self._temporal_window_seconds
        )
        self._entity_metrics_history[entity_id][metric_name] = [
            point
            for point in self._entity_metrics_history[entity_id][metric_name]
            if point["timestamp"] > cutoff_time
        ]

    def _detect_critical_sequence(self, entity_id: str) -> Optional[str]:
        """Detect if entity has experienced a critical sequence of events."""
        if entity_id not in self._entity_sequences:
            return None

        sequence = self._entity_sequences[entity_id]
        if len(sequence) < 2:
            return None

        # Get events within the temporal window
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff_time = now - datetime.timedelta(seconds=self._temporal_window_seconds)
        recent_sequence = [
            item["reason"] for item in sequence if item["timestamp"] > cutoff_time
        ]

        # Check for matches with known critical sequences
        for seq_name, pattern in self.CRITICAL_SEQUENCES.items():
            # Check if pattern is a subsequence of recent events
            pattern_idx = 0
            for event in recent_sequence:
                if event == pattern[pattern_idx]:
                    pattern_idx += 1
                    if pattern_idx == len(pattern):
                        return seq_name

        return None

    def _detect_metric_trend(self, entity_id: str, metric_name: str) -> Optional[dict]:
        """Detect concerning trends in metrics."""
        if (
            entity_id not in self._entity_metrics_history
            or metric_name not in self._entity_metrics_history[entity_id]
        ):
            return None

        # Get pattern definition if it exists
        pattern = self.CRITICAL_METRIC_PATTERNS.get(metric_name)
        if not pattern:
            return None

        threshold = pattern["threshold"]
        required_count = pattern["count"]
        window_seconds = pattern["window_seconds"]

        # Get metric history within the specified window
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff_time = now - datetime.timedelta(seconds=window_seconds)

        # Count points exceeding threshold in the window
        points = [
            point
            for point in self._entity_metrics_history[entity_id][metric_name]
            if point["timestamp"] > cutoff_time
        ]

        if len(points) < required_count:
            return None

        # Count points exceeding threshold
        exceeding_points = [point for point in points if point["value"] > threshold]

        if len(exceeding_points) >= required_count:
            return {
                "metric": metric_name,
                "threshold": threshold,
                "count": len(exceeding_points),
                "required_count": required_count,
                "window_seconds": window_seconds,
                "first_exceeded": exceeding_points[0]["timestamp"],
                "last_exceeded": exceeding_points[-1]["timestamp"],
            }

        # Check for steady increase (upward trend)
        if len(points) >= 3:
            values = [point["value"] for point in points]
            # Simple trend detection - check if values are monotonically increasing
            is_increasing = all(
                values[i] <= values[i + 1] for i in range(len(values) - 1)
            )

            if (
                is_increasing and values[-1] > values[0] * 1.5
            ):  # 50% increase from first to last
                return {
                    "metric": metric_name,
                    "trend": "increasing",
                    "start_value": values[0],
                    "end_value": values[-1],
                    "increase_pct": (values[-1] - values[0]) / values[0] * 100
                    if values[0] > 0
                    else float("inf"),
                    "first_point": points[0]["timestamp"],
                    "last_point": points[-1]["timestamp"],
                }

        return None


# --- Async Detection Loop for API Integration ---


async def detection_loop(
    detector: OnlineAnomalyDetector,
    metric_queue: asyncio.Queue,
    event_queue: asyncio.Queue,
    remediation_queue: asyncio.Queue,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    log_interval_seconds: int = 10,
    app_context=None,  # Optional context for metrics
) -> None:
    """
    Main anomaly detection loop that processes metrics and events.

    Continuously consumes data from both metric and event queues, using
    the provided detector to identify anomalies. Detected anomalies are
    stored in MongoDB and placed in the remediation queue.
    """
    last_log_time = time.time()
    processed_metrics = 0
    processed_events = 0
    detected_anomalies = 0

    logger.info("Starting anomaly detection loop")

    while True:
        try:
            # Process all available metrics first
            metrics_processed_batch = 0
            while not metric_queue.empty():
                # Get metric from queue
                metric = await metric_queue.get()
                processed_metrics += 1
                metrics_processed_batch += 1

                # Set a reasonable batch size to avoid blocking the event loop
                if metrics_processed_batch >= 100:
                    break

                # Process the metric and detect anomalies
                anomaly = await detector.detect_anomaly(metric)
                if anomaly:
                    detected_anomalies += 1
                    # Use available field for logging
                    reason_field = (
                        getattr(anomaly, "failure_reason", None)
                        or getattr(anomaly, "event_reason", None)
                        or getattr(anomaly, "metric_name", "unknown")
                    )
                    logger.info(
                        f"Detected metric anomaly: {anomaly.entity_id} ({reason_field})"
                    )

                    # Record metrics if app_context available
                    if app_context and hasattr(app_context, "record_anomaly"):
                        try:
                            entity_type = anomaly.entity_type or "unknown"
                            namespace = "unknown"
                            if "/" in anomaly.entity_id:
                                namespace = anomaly.entity_id.split("/")[0]
                            app_context.record_anomaly(
                                source="metric",
                                entity_type=entity_type,
                                namespace=namespace,
                            )
                        except Exception as metrics_err:
                            logger.warning(
                                f"Failed to record metric anomaly metrics: {metrics_err}"
                            )

                    # Queue for remediation
                    await remediation_queue.put(anomaly)

                # Mark metric as processed
                metric_queue.task_done()

            # Process events up to a certain batch size
            events_processed_batch = 0
            while not event_queue.empty():
                # Get event from queue
                event = await event_queue.get()
                processed_events += 1
                events_processed_batch += 1

                # Set a reasonable batch size to avoid blocking
                if events_processed_batch >= 50:
                    break

                # Process the event and detect anomalies
                anomaly = await detector.detect_anomaly(event)
                if anomaly:
                    detected_anomalies += 1
                    # Use the most specific reason field available
                    reason_field = (
                        getattr(anomaly, "failure_reason", None)
                        or getattr(anomaly, "event_reason", None)
                        or "Event anomaly"
                    )
                    logger.info(
                        f"Detected event anomaly: {anomaly.entity_id} ({reason_field})"
                    )

                    # Record metrics if app_context available
                    if app_context and hasattr(app_context, "record_anomaly"):
                        try:
                            entity_type = anomaly.entity_type or "unknown"
                            namespace = "unknown"
                            if "/" in anomaly.entity_id:
                                namespace = anomaly.entity_id.split("/")[0]
                            app_context.record_anomaly(
                                source="event",
                                entity_type=entity_type,
                                namespace=namespace,
                            )
                        except Exception as metrics_err:
                            logger.warning(
                                f"Failed to record event anomaly metrics: {metrics_err}"
                            )

                    # Queue for remediation
                    await remediation_queue.put(anomaly)

                # Mark event as processed
                event_queue.task_done()

            # Log statistics periodically
            current_time = time.time()
            if current_time - last_log_time > log_interval_seconds:
                logger.debug(
                    f"Detection stats: "
                    f"Processed {processed_metrics} metrics, {processed_events} events. "
                    f"Detected {detected_anomalies} anomalies."
                )

                # Update app context stats if available
                if app_context and hasattr(app_context, "detector_stats"):
                    app_context.detector_stats["processed_metrics"] = processed_metrics
                    app_context.detector_stats["processed_events"] = processed_events
                    app_context.detector_stats["anomalies_detected"] = (
                        detected_anomalies
                    )

                last_log_time = current_time

            # If both queues are empty, avoid tight loop
            if metric_queue.empty() and event_queue.empty():
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.info("Anomaly detection loop cancelled")
            break
        except Exception as e:
            logger.exception(f"Error in anomaly detection loop: {e}")
            # Continue processing despite errors
            await asyncio.sleep(1)


# For testing/debugging
def main():
    """Simple main function for testing the detector standalone."""

    async def run():
        # Setup database connection
        mongo_client = motor.motor_asyncio.AsyncIOMotorClient(settings.mongo_uri)
        db = mongo_client[settings.mongo_dbname]

        # Create queues
        metric_queue = asyncio.Queue()
        event_queue = asyncio.Queue()
        remediation_queue = asyncio.Queue()

        # Create detector - now using SequentialAnomalyDetector for better pattern detection
        detector = SequentialAnomalyDetector(
            db=db,
            hst_n_trees=settings.hst_n_trees,
            hst_height=settings.hst_height,
            hst_window_size=settings.hst_window_size,
            river_trees=settings.river_trees,
            # Enhanced settings for better detection
            max_sequence_history=30,  # Increased from default 20
            iso_n_trees=150,  # More trees for better isolation forest
            # Adjust remediation cooldown to prevent alert storms
            remediation_cooldown_seconds=300,  # Reduced from 600 for more responsive detection
        )
        
        # Configure detector weights to favor HST for better real-time detection
        detector.detector_weights = {
            'hstdetector': 0.6,  # Higher weight for HST's streaming anomaly detection
            'isolationforestdetector': 0.4,  # Lower weight for isolation forest
        }
        
        # Set custom thresholds for critical metric patterns
        detector.CRITICAL_METRIC_PATTERNS.update({
            "memory_usage": {
                "threshold": 0.9,  # Lower from 0.95
                "count": 2,
                "window_seconds": 300,
            },
            # Add new pattern for slow memory growth detection
            "memory_10min_delta_pct": {
                "threshold": 20,  # Alert on 20% growth in 10 min
                "count": 1,
                "window_seconds": 600,
            },
        })

        # Start detection loop
        detection_task = asyncio.create_task(
            detection_loop(
                detector=detector,
                metric_queue=metric_queue,
                event_queue=event_queue,
                remediation_queue=remediation_queue,
                db=db,
            )
        )

        # Generate some test metrics/events
        for i in range(100):
            # Add a test metric
            test_metric = MetricPoint(
                metric_name="cpu_usage",
                value=random.random(),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
                labels={"namespace": "test", "pod": f"pod-{i % 10}"},
            )
            await metric_queue.put(test_metric)
            
            # Add memory usage with growing trend for some pods
            if i % 10 == 5:  # Just for pod-5
                growth_factor = 1.0 + (i / 100) * 0.5  # Gradual growth
                test_memory_metric = MetricPoint(
                    metric_name="memory_usage",
                    value=0.5 * growth_factor,  # Steadily increasing memory
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                    labels={"namespace": "test", "pod": "pod-5"},
                )
                await metric_queue.put(test_memory_metric)

            # Random event
            if random.random() > 0.8:
                test_event = KubernetesEvent(
                    type="Warning",
                    reason="CrashLoopBackOff",
                    message=f"Pod test/pod-{i % 10} is in CrashLoopBackOff state",
                    involved_object_kind="Pod",
                    involved_object_name=f"pod-{i % 10}",
                    involved_object_namespace="test",
                    event_time=datetime.datetime.now(datetime.timezone.utc),
                )
                await event_queue.put(test_event)

            await asyncio.sleep(0.1)

        # Monitor remediation queue
        while True:
            try:
                anomaly = remediation_queue.get_nowait()
                logger.info(
                    f"Remediation queue: {anomaly.entity_id} ({anomaly.failure_reason})"
                )
                remediation_queue.task_done()
            except asyncio.QueueEmpty:
                break

        # Wait for detection to finish
        await metric_queue.join()
        await event_queue.join()
        detection_task.cancel()
        try:
            await detection_task
        except asyncio.CancelledError:
            pass

        # Save detector state
        await detector.save_all_state()

    asyncio.run(run())


if __name__ == "__main__":
    main()
