package outcome

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/predictor"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestTracker_TrackAndVerifyHit(t *testing.T) {
	dir := t.TempDir()
	s, err := store.Open(filepath.Join(dir, "agent.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	tr := NewTracker(s)
	now := time.Now()
	tr.TrackPatternPredictions([]models.PredictionResult{
		{
			Type:       "pattern",
			Entity:     "api",
			Namespace:  "demo",
			MetricName: "oom_predicted",
			Confidence: 0.9,
			ETASeconds: 300,
		},
	}, now)

	pending, err := s.ListTrackedPredictions(models.PredictionOutcomePending, 10)
	if err != nil || len(pending) != 1 {
		t.Fatalf("expected 1 pending, got %d err=%v", len(pending), err)
	}

	tr.VerifyPending(predictor.ResourceSnapshot{
		FailingPods: []string{models.FormatEntity("demo", "api")},
	}, now.Add(time.Minute))

	pending, _ = s.ListTrackedPredictions(models.PredictionOutcomePending, 10)
	hits, _ := s.ListTrackedPredictions(models.PredictionOutcomeHit, 10)
	if len(pending) != 0 || len(hits) != 1 {
		t.Fatalf("expected hit: pending=%d hits=%d", len(pending), len(hits))
	}

	stats, err := s.ComputeAgentStats()
	if err != nil {
		t.Fatal(err)
	}
	if stats.PredictionsHit != 1 || stats.PredictionAccuracy != 1 {
		t.Fatalf("unexpected stats: %+v", stats)
	}
}

func TestTracker_PruneExpired(t *testing.T) {
	dir := t.TempDir()
	s, err := store.Open(filepath.Join(dir, "agent.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	past := time.Now().Add(-time.Hour)
	_ = s.SaveTrackedPrediction(models.TrackedPrediction{
		ID:        "exp-1",
		Entity:    "demo/api",
		Pattern:   "crashloop",
		CreatedAt: past,
		ExpiresAt: past.Add(time.Minute),
		Outcome:   models.PredictionOutcomePending,
	})

	n, err := s.PruneExpiredPredictions(time.Now())
	if err != nil || n != 1 {
		t.Fatalf("expected 1 pruned prediction, got %d err=%v", n, err)
	}
	missed, _ := s.ListTrackedPredictions(models.PredictionOutcomeMiss, 10)
	if len(missed) != 1 {
		t.Fatalf("expected 1 missed prediction, got %d", len(missed))
	}
}
