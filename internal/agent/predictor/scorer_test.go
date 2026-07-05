package predictor

import (
	"math"
	"testing"
)

func TestScorerDefaultWeights(t *testing.T) {
	s := NewScorer(DefaultScorerConfig())

	// All zero → score zero
	score := s.Combine(0, 0, 0)
	if score != 0 {
		t.Fatalf("expected 0, got %f", score)
	}

	// All max → score 1
	score = s.Combine(1, 1, 1)
	if score != 1.0 {
		t.Fatalf("expected 1, got %f", score)
	}

	// EWMA only (0.25 weight)
	score = s.Combine(1.0, 0, 0)
	if math.Abs(score-0.25) > 1e-6 {
		t.Fatalf("expected 0.25, got %f", score)
	}

	// Z-score only (0.50 weight)
	score = s.Combine(0, 1.0, 0)
	if math.Abs(score-0.50) > 1e-6 {
		t.Fatalf("expected 0.50, got %f", score)
	}

	// ROC only (0.25 weight)
	score = s.Combine(0, 0, 1.0)
	if math.Abs(score-0.25) > 1e-6 {
		t.Fatalf("expected 0.25, got %f", score)
	}
}

func TestScorerCustomWeights(t *testing.T) {
	cfg := ScorerConfig{EWMAWeight: 0.5, ZScoreWeight: 0.3, ROCWeight: 0.2}
	s := NewScorer(cfg)

	score := s.Combine(1.0, 1.0, 1.0)
	if math.Abs(score-1.0) > 1e-6 {
		t.Fatalf("expected 1.0, got %f", score)
	}

	score = s.Combine(0.5, 0.5, 0.5)
	expected := 0.5*0.5 + 0.3*0.5 + 0.2*0.5
	if math.Abs(score-expected) > 1e-6 {
		t.Fatalf("expected %f, got %f", expected, score)
	}
}

func TestScorerClamp(t *testing.T) {
	s := NewScorer(DefaultScorerConfig())

	// Values above 1.0 should be clamped
	score := s.Combine(2.0, 3.0, -1.0)
	if score < 0 || score > 1 {
		t.Fatalf("expected clamped score [0,1], got %f", score)
	}
}
