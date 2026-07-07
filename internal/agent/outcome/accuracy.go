package outcome

import (
	"log"
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
		log.Printf("accuracy: compute snapshot error: %v", err)
		return nil, err
	}
	log.Printf("accuracy: snapshot computed for window=%s overall="+
		"precision=%.3f recall=%.3f f1=%.3f predictors=%d namespaces=%d metrics=%d",
		window, snap.Overall.Precision, snap.Overall.Recall, snap.Overall.F1Score,
		len(snap.ByPredictor), len(snap.ByNamespace), len(snap.ByMetric))
	return snap, nil
}
