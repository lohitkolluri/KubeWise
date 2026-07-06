package predictor

// ScorerConfig controls the anomaly scoring pipeline.  The primary signal is
// the Hoeffding-based anomaly score from the adaptive median estimator.
// A secondary ROC-acceleration boost is added when a metric is trending
// sharply upward or downward.
type ScorerConfig struct {
	// HoeffdingDelta is the false-positive target for the Hoeffding bound
	// (default 0.05 = 5%).
	HoeffdingDelta float64

	// HoeffdingK is the sensitivity multiplier: score reaches 1.0 when
	// |x-median| >= K * epsilon (default 3.0).
	HoeffdingK float64

	// MinWarmup is the minimum data points per metric key before scoring
	// starts (default 10).
	MinWarmup int

	// ROCBoostWeight controls how much the rate-of-change acceleration
	// can boost the final score (default 0.3 = max 0.3 boost).
	ROCBoostWeight float64
}

// DefaultScorerConfig returns a production-ready ScorerConfig with robust
// defaults for Kubernetes workload metrics.
//
// K=5 means score reaches 1.0 at |dev| >= 5ε, providing enough headroom
// that routine Gaussian noise produces moderate scores (0.1–0.6) while
// genuine anomalies (spikes, shifts) still saturate at 1.0.
func DefaultScorerConfig() ScorerConfig {
	return ScorerConfig{
		HoeffdingDelta: 0.05,
		HoeffdingK:     5.0,
		MinWarmup:      MinimumWarmupPoints,
		ROCBoostWeight: 0.3,
	}
}

// Scorer combines the primary Hoeffding anomaly score with a secondary
// ROC-acceleration boost.
type Scorer struct {
	config ScorerConfig
}

// NewScorer creates a Scorer from the given config.
func NewScorer(config ScorerConfig) *Scorer {
	cfg := config
	if cfg.HoeffdingDelta <= 0 || cfg.HoeffdingDelta >= 1 {
		cfg.HoeffdingDelta = DefaultHoeffdingDelta
	}
	if cfg.HoeffdingK <= 0 {
		cfg.HoeffdingK = DefaultHoeffdingK
	}
	if cfg.MinWarmup <= 0 {
		cfg.MinWarmup = MinimumWarmupPoints
	}
	if cfg.ROCBoostWeight <= 0 {
		cfg.ROCBoostWeight = 0.3
	}
	return &Scorer{config: cfg}
}

// Score combines the primary anomaly score with the ROC boost.  Both inputs
// should be in [0, 1] (the caller is responsible for clamping).  The result
// is clamped to [0, 1].
func (s *Scorer) Score(primary, rocBoost float64) float64 {
	total := primary + clamp(rocBoost, 0, s.config.ROCBoostWeight)
	return clamp(total, 0, 1)
}

// clamp bounds v to [lo, hi].
func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
