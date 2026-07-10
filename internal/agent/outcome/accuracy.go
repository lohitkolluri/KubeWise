// Package outcome provides prediction accuracy tracking and validation.
package outcome

import (
	"log/slog"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// AccuracyComputer computes and persists per-predictor/namespace/resource/metric accuracy snapshots.
type AccuracyComputer struct {
	store *store.Store
}

// NewAccuracyComputer creates a new accuracy computer.
func NewAccuracyComputer(s *store.Store) *AccuracyComputer {
	return &AccuracyComputer{store: s}
}

// ComputeSnapshot delegates to the store-level aggregation which already handles
// the full snapshot computation (group-by predictor, namespace, metric, resource kind).
// It exists as a separate type so the agent can invoke it on a periodic cadence.
func (ac *AccuracyComputer) ComputeSnapshot(window time.Duration) (*models.AccuracySnapshot, error) {
	snap, err := ac.store.ComputeAccuracySnapshot(window)
	if err != nil {
		slog.Error("accuracy: compute snapshot error", "error", err)
		return nil, err
	}
	slog.Info("accuracy: snapshot computed",
		"window", window,
		"precision", snap.Overall.Precision,
		"recall", snap.Overall.Recall,
		"f1", snap.Overall.F1Score,
		"predictors", len(snap.ByPredictor),
		"namespaces", len(snap.ByNamespace),
		"metrics", len(snap.ByMetric))
	return snap, nil
}
