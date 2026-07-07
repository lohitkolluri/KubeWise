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

// --- NewScorer config validation defaults ---

func TestNewScorer_ZeroConfigDefaults(t *testing.T) {
	s := NewScorer(ScorerConfig{})
	if s.config.HoeffdingDelta != DefaultHoeffdingDelta {
		t.Fatalf("HoeffdingDelta: expected %f, got %f", DefaultHoeffdingDelta, s.config.HoeffdingDelta)
	}
	if s.config.HoeffdingK != DefaultHoeffdingK {
		t.Fatalf("HoeffdingK: expected %f, got %f", DefaultHoeffdingK, s.config.HoeffdingK)
	}
	if s.config.MinWarmup != MinimumWarmupPoints {
		t.Fatalf("MinWarmup: expected %d, got %d", MinimumWarmupPoints, s.config.MinWarmup)
	}
	if s.config.ROCBoostWeight != 0.3 {
		t.Fatalf("ROCBoostWeight: expected 0.3, got %f", s.config.ROCBoostWeight)
	}
	if s.config.MinScore != 0.3 {
		t.Fatalf("MinScore: expected 0.3, got %f", s.config.MinScore)
	}
	if s.config.Persistence != 1 {
		t.Fatalf("Persistence: expected 1, got %d", s.config.Persistence)
	}
	if s.config.PatternPersistence != 1 {
		t.Fatalf("PatternPersistence: expected 1, got %d", s.config.PatternPersistence)
	}
	if s.config.PatternCooldownScrapes != 0 {
		t.Fatalf("PatternCooldownScrapes: expected 0, got %d", s.config.PatternCooldownScrapes)
	}
}

func TestNewScorer_PartialDefaults(t *testing.T) {
	// Only set a couple fields; everything else should get defaults.
	s := NewScorer(ScorerConfig{
		HoeffdingDelta: 0.1,
		HoeffdingK:     3.0,
	})
	if s.config.HoeffdingDelta != 0.1 {
		t.Fatalf("HoeffdingDelta: expected 0.1, got %f", s.config.HoeffdingDelta)
	}
	if s.config.HoeffdingK != 3.0 {
		t.Fatalf("HoeffdingK: expected 3.0, got %f", s.config.HoeffdingK)
	}
	// These should still get defaults.
	if s.config.MinWarmup != MinimumWarmupPoints {
		t.Fatalf("MinWarmup: expected %d, got %d", MinimumWarmupPoints, s.config.MinWarmup)
	}
	if s.config.MinScore != 0.3 {
		t.Fatalf("MinScore: expected 0.3, got %f", s.config.MinScore)
	}
	if s.config.Persistence != 1 {
		t.Fatalf("Persistence: expected 1, got %d", s.config.Persistence)
	}
}

func TestNewScorer_DeltaBoundary(t *testing.T) {
	// Delta = 0 should trigger default.
	s := NewScorer(ScorerConfig{HoeffdingDelta: 0, HoeffdingK: 3})
	if s.config.HoeffdingDelta != DefaultHoeffdingDelta {
		t.Fatalf("Delta=0: expected default %f, got %f", DefaultHoeffdingDelta, s.config.HoeffdingDelta)
	}
	// Delta = -1 should trigger default.
	s = NewScorer(ScorerConfig{HoeffdingDelta: -1, HoeffdingK: 3})
	if s.config.HoeffdingDelta != DefaultHoeffdingDelta {
		t.Fatalf("Delta=-1: expected default %f, got %f", DefaultHoeffdingDelta, s.config.HoeffdingDelta)
	}
	// Delta = 1 should trigger default (>=1).
	s = NewScorer(ScorerConfig{HoeffdingDelta: 1, HoeffdingK: 3})
	if s.config.HoeffdingDelta != DefaultHoeffdingDelta {
		t.Fatalf("Delta=1: expected default %f, got %f", DefaultHoeffdingDelta, s.config.HoeffdingDelta)
	}
	// Delta = 0.5 should be kept (in (0,1)).
	s = NewScorer(ScorerConfig{HoeffdingDelta: 0.5, HoeffdingK: 3})
	if s.config.HoeffdingDelta != 0.5 {
		t.Fatalf("Delta=0.5: expected 0.5, got %f", s.config.HoeffdingDelta)
	}
}

// --- NaN / Inf handling ---

func TestScoreNaN(t *testing.T) {
	s := NewScorer(DefaultScorerConfig())

	// NaN primary → clamp treats NaN < 0 as true (via math.IsNaN) → 0.
	score := s.Score(math.NaN(), 0)
	if score != 0 {
		t.Fatalf("expected 0 for NaN primary, got %f", score)
	}

	// NaN boost → clamp treats NaN < 0 as true → 0, so score = 0.5.
	score = s.Score(0.5, math.NaN())
	if math.Abs(score-0.5) > 1e-6 {
		t.Fatalf("expected 0.5 for NaN boost, got %f", score)
	}

	// Both NaN → both clamped to 0 → score 0.
	score = s.Score(math.NaN(), math.NaN())
	if score != 0 {
		t.Fatalf("expected 0 for both NaN, got %f", score)
	}
}

func TestScoreInf(t *testing.T) {
	s := NewScorer(DefaultScorerConfig())

	// +Inf primary → 1 (clamped).
	score := s.Score(math.Inf(1), 0)
	if score != 1.0 {
		t.Fatalf("expected 1.0 for +Inf primary, got %f", score)
	}

	// -Inf primary → 0 (clamped).
	score = s.Score(math.Inf(-1), 0)
	if score != 0 {
		t.Fatalf("expected 0 for -Inf primary, got %f", score)
	}

	// +Inf boost → clamped to ROCBoostWeight then added.
	score = s.Score(0.5, math.Inf(1))
	want := 0.5 + DefaultScorerConfig().ROCBoostWeight
	if math.Abs(score-want) > 1e-6 {
		t.Fatalf("expected %f for +Inf boost, got %f", want, score)
	}

	// Both +Inf → 1.0 (clamped after 1 + boost).
	score = s.Score(math.Inf(1), math.Inf(1))
	if score != 1.0 {
		t.Fatalf("expected 1.0 for both +Inf, got %f", score)
	}
}

// --- Overflow clamping edge cases ---

func TestScore_OverflowClamping(t *testing.T) {
	s := NewScorer(DefaultScorerConfig())

	tests := []struct {
		name  string
		prim  float64
		boost float64
		want  float64
	}{
		{"max+maxboost", 1.0, DefaultScorerConfig().ROCBoostWeight, 1.0},
		{"high+med", 0.85, 0.15, 1.0},
		{"good+good", 0.6, 0.1, 0.7},
		{"just-under", 0.8, 0.1, 0.9},
		{"at-cap", 0.8, 0.2, 1.0},
		{"zero", 0.0, 0.0, 0.0},
		{"one", 1.0, 0.0, 1.0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := s.Score(tt.prim, tt.boost)
			if math.Abs(got-tt.want) > 1e-6 {
				t.Errorf("Score(%v, %v) = %v, want %v", tt.prim, tt.boost, got, tt.want)
			}
		})
	}
}

// --- clamp edge cases ---

func TestClampEdgeCases(t *testing.T) {
	// Equal bounds.
	if v := clamp(5, 5, 5); v != 5 {
		t.Fatalf("equal bounds: expected 5, got %f", v)
	}

	// NaN is clamped to lo (math.IsNaN guard catches NaN < lo).
	v := clamp(math.NaN(), 0, 1)
	if v != 0 {
		t.Fatalf("NaN: expected 0 (clamped to lo), got %f", v)
	}

	// +Inf clamped to hi.
	v = clamp(math.Inf(1), 0, 1)
	if v != 1 {
		t.Fatalf("+Inf: expected 1, got %f", v)
	}

	// -Inf clamped to lo.
	v = clamp(math.Inf(-1), 0, 1)
	if v != 0 {
		t.Fatalf("-Inf: expected 0, got %f", v)
	}

	// Zero-width range.
	v = clamp(42, 42, 42)
	if v != 42 {
		t.Fatalf("zero-width: expected 42, got %f", v)
	}

	// Large values.
	v = clamp(1e300, 0, 1)
	if v != 1 {
		t.Fatalf("large: expected 1, got %f", v)
	}

	v = clamp(-1e300, 0, 1)
	if v != 0 {
		t.Fatalf("large neg: expected 0, got %f", v)
	}
}
