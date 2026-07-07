package predictor

import (
	"math"
	"testing"
)

func TestScorerDefaultConfig(t *testing.T) {
	s := NewScorer(DefaultScorerConfig())

	// Zero inputs → score 0
	score := s.Score(0, 0)
	if score != 0 {
		t.Fatalf("expected 0, got %f", score)
	}

	// Max primary + no boost → 1
	score = s.Score(1.0, 0)
	if score != 1.0 {
		t.Fatalf("expected 1.0, got %f", score)
	}

	// Primary 0.5, no boost → 0.5
	score = s.Score(0.5, 0)
	if math.Abs(score-0.5) > 1e-6 {
		t.Fatalf("expected 0.5, got %f", score)
	}
}

func TestScorerROCBoost(t *testing.T) {
	cfg := DefaultScorerConfig()
	s := NewScorer(cfg)

	// Boost is capped at ROCBoostWeight
	score := s.Score(0.0, 1.0)
	if math.Abs(score-cfg.ROCBoostWeight) > 1e-6 {
		t.Fatalf("expected %.3f (boost capped), got %f", cfg.ROCBoostWeight, score)
	}

	// Boost is additive: 0.2 + 0.25 = 0.45
	score = s.Score(0.2, 0.25)
	want := 0.2 + math.Min(0.25, cfg.ROCBoostWeight)
	if math.Abs(score-want) > 1e-6 {
		t.Fatalf("expected %0.3f, got %f", want, score)
	}
}

func TestScorerClamp(t *testing.T) {
	s := NewScorer(DefaultScorerConfig())

	// Negative inputs clamped to 0
	score := s.Score(-0.5, -0.5)
	if score != 0 {
		t.Fatalf("expected 0 for negative, got %f", score)
	}

	// Overflow clamped to 1
	score = s.Score(0.9, 0.5)
	if score != 1.0 {
		t.Fatalf("expected 1.0 for overflow, got %f", score)
	}
}

func TestScorerCustomConfig(t *testing.T) {
	cfg := ScorerConfig{
		HoeffdingDelta: 0.01,
		HoeffdingK:     2.0,
		MinWarmup:      5,
		ROCBoostWeight: 0.5,
	}
	s := NewScorer(cfg)

	// Boost capped at 0.5
	score := s.Score(0.0, 1.0)
	if math.Abs(score-0.5) > 1e-6 {
		t.Fatalf("expected 0.5, got %f", score)
	}

	// 0.6 + 0.4 = 1.0
	score = s.Score(0.6, 0.4)
	if math.Abs(score-1.0) > 1e-6 {
		t.Fatalf("expected 1.0, got %f", score)
	}
}

func TestClamp(t *testing.T) {
	if v := clamp(5, 0, 10); v != 5 {
		t.Fatalf("expected 5, got %f", v)
	}
	if v := clamp(-1, 0, 10); v != 0 {
		t.Fatalf("expected 0, got %f", v)
	}
	if v := clamp(15, 0, 10); v != 10 {
		t.Fatalf("expected 10, got %f", v)
	}
}
