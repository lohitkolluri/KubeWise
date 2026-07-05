package predictor

type ScorerConfig struct {
	EWMAWeight  float64
	ZScoreWeight float64
	ROCWeight   float64
}

func DefaultScorerConfig() ScorerConfig {
	return ScorerConfig{
		EWMAWeight:   0.25,
		ZScoreWeight: 0.50,
		ROCWeight:    0.25,
	}
}

type Scorer struct {
	config ScorerConfig
}

func NewScorer(config ScorerConfig) *Scorer {
	return &Scorer{config: config}
}

func (s *Scorer) Combine(ewmaScore, zScore, rocScore float64) float64 {
	total := s.config.EWMAWeight*clamp(ewmaScore, 0, 1) +
		s.config.ZScoreWeight*clamp(zScore, 0, 1) +
		s.config.ROCWeight*clamp(rocScore, 0, 1)
	return clamp(total, 0, 1)
}

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
