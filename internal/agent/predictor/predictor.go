package predictor

import (
	"math"
	"strings"
	"sync"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Predictor is the Tier-1 anomaly detection engine.  It uses a robust
// adaptive-median estimator combined with Hoeffding-bound anomaly scoring and
// Bayesian Online Changepoint Detection for regime-change adaptation.
type Predictor struct {
	estimators     map[string]*AdaptiveMedian
	changepoints   map[string]*ChangepointDetector
	roc            *RateOfChange
	scorer         *Scorer
	config         ScorerConfig
	mu             sync.RWMutex
	history        map[string][]MetricPoint
	patternHistory map[string][]MetricPoint
	datapoints     map[string]int
	patterns       []PatternMatcher
}

// NewPredictor creates a Predictor wired with the given scorer configuration.
func NewPredictor(config ScorerConfig) *Predictor {
	return &Predictor{
		estimators:     make(map[string]*AdaptiveMedian),
		changepoints:   make(map[string]*ChangepointDetector),
		roc:            &RateOfChange{},
		scorer:         NewScorer(config),
		config:         config,
		history:        make(map[string][]MetricPoint),
		patternHistory: make(map[string][]MetricPoint),
		datapoints:     make(map[string]int),
	}
}

// LoadPatternHistory seeds in-memory pattern history from persisted metric series.
func (p *Predictor) LoadPatternHistory(s *store.Store, limit int) {
	if s == nil || limit <= 0 {
		return
	}
	names := []string{
		"pod_memory_usage", "restart_rate", "crashloop", "oomkilled",
		"pod_not_ready", "node_disk_pressure", "node_memory_pressure",
	}
	for _, name := range names {
		keys, err := s.ListMetricSeries(name)
		if err != nil {
			continue
		}
		for _, key := range keys {
			_, labels, err := store.ParseSeriesKey(key)
			if err != nil {
				continue
			}
			pts, err := s.GetMetricSeries(name, labels, limit)
			if err != nil || len(pts) == 0 {
				continue
			}
			p.mu.Lock()
			for _, pt := range pts {
				mp := MetricPoint{Timestamp: pt.TS, Value: pt.Value, Labels: labels}
				p.patternHistory[key] = append(p.patternHistory[key], mp)
			}
			if len(p.patternHistory[key]) > limit {
				p.patternHistory[key] = p.patternHistory[key][len(p.patternHistory[key])-limit:]
			}
			p.mu.Unlock()
		}
	}
}

// PreparePatternMetrics accumulates the current scrape into cross-scrape history
// and returns enriched metric results for pattern matchers.
func (p *Predictor) PreparePatternMetrics(metrics []MetricResult) []MetricResult {
	p.mu.Lock()
	defer p.mu.Unlock()

	for _, mr := range metrics {
		for _, pt := range mr.Values {
			key := metricKey(mr.Name, pt.Labels)
			p.patternHistory[key] = append(p.patternHistory[key], pt)
			if len(p.patternHistory[key]) > maxPatternHistory {
				p.patternHistory[key] = p.patternHistory[key][len(p.patternHistory[key])-maxPatternHistory:]
			}
		}
	}

	byMetric := make(map[string][]MetricPoint)
	for key, pts := range p.patternHistory {
		metricName := key
		if idx := strings.Index(key, "/"); idx >= 0 {
			metricName = key[:idx]
		}
		byMetric[metricName] = append(byMetric[metricName], pts...)
	}

	result := make([]MetricResult, 0, len(byMetric))
	for name, vals := range byMetric {
		result = append(result, MetricResult{Name: name, Values: vals})
	}
	return result
}

// AddPattern registers a pattern matcher for domain-specific anomaly
// detection (e.g. OOM, CrashLoop, degradation).
func (p *Predictor) AddPattern(m PatternMatcher) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.patterns = append(p.patterns, m)
}

// RunPatterns executes all registered pattern matchers against the given
// metrics, events, and resource state.
func (p *Predictor) RunPatterns(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []models.PredictionResult {
	p.mu.RLock()
	patterns := make([]PatternMatcher, len(p.patterns))
	copy(patterns, p.patterns)
	p.mu.RUnlock()

	var results []models.PredictionResult
	for _, pat := range patterns {
		matches := pat.Match(metrics, events, resources)
		for _, m := range matches {
			results = append(results, patternToResult(m))
		}
	}
	return results
}

// Run performs statistical anomaly detection on the given metrics.
//
// The pipeline per metric point is:
//  1. Accumulate into the adaptive-median estimator.
//  2. Periodically run the changepoint detector; reset window on regime shift.
//  3. After warmup, compute the Hoeffding-bound anomaly score.
//  4. Compute a rate-of-change boost from recent history (velocity/acceleration).
//  5. Combine and emit results with score >= 0.3.
func (p *Predictor) Run(metrics []MetricResult) ([]models.PredictionResult, error) {
	if len(metrics) == 0 {
		return nil, nil
	}

	var results []models.PredictionResult

	for _, mr := range metrics {
		if len(mr.Values) == 0 {
			continue
		}

		for _, pt := range mr.Values {
			key := metricKey(mr.Name, pt.Labels)

			// --- state access -------------------------------------------------------
			p.mu.Lock()

			p.datapoints[key]++
			dp := p.datapoints[key]

			est, ok := p.estimators[key]
			if !ok {
				est = NewAdaptiveMedian(DefaultAdaptiveMedianWindow)
				p.estimators[key] = est
			}

			cp, ok := p.changepoints[key]
			if !ok {
				cp = NewChangepointDetector(DefaultChangepointMinSeg, DefaultChangepointInterval)
				p.changepoints[key] = cp
			}

			// Keep a bounded history for ROC computations. Copy under lock
			// for safe concurrent access.
			p.history[key] = append(p.history[key], pt)
			if len(p.history[key]) > 20 {
				p.history[key] = p.history[key][1:]
			}
			localHistory := make([]MetricPoint, len(p.history[key]))
			copy(localHistory, p.history[key])

			p.mu.Unlock()
			// --- warmup phase ------------------------------------------------------
			est.Add(pt.Value)

			if dp < p.config.MinWarmup {
				continue
			}

			// --- changepoint detection (adds to buffer AND returns detection flag) -
			if cp.Add(pt.Value) {
				est.Reset()
				est.Add(pt.Value) // re-seed with the regime-start value
				p.mu.Lock()
				p.history[key] = nil
				p.mu.Unlock()
				continue // don't score the changepoint-boundary point
			}

			// --- adaptive-median + Hoeffding scoring -------------------------------
			median, _, rng, n, ok := est.Stats()
			if !ok || n < p.config.MinWarmup {
				continue
			}

			primaryScore := HoeffdingAnomalyScore(
				pt.Value, median, rng, n,
				p.config.HoeffdingDelta,
				p.config.HoeffdingK,
			)

			// --- rate-of-change boost -----------------------------------------------
			rocBoost := 0.0
			history := localHistory
			if len(history) >= 4 {
				vel := p.roc.Velocity(history)
				if math.Abs(vel.Slope) > 0 {
					// Scale-invariant relative slope.
					relSlope := math.Abs(vel.Slope) / math.Max(math.Abs(median), 1.0)
					rocBoost = math.Min(relSlope*5.0, p.config.ROCBoostWeight)
				}
			}

			// --- final score --------------------------------------------------------
			score := p.scorer.Score(primaryScore, rocBoost)

			if score >= 0.3 {
				results = append(results, models.PredictionResult{
					Type:       "statistical",
					Entity:     entityName(mr.Name, pt.Labels),
					Namespace:  pt.Labels["namespace"],
					MetricName: mr.Name,
					Score:      score,
					Timestamp:  time.Now(),
				})
			}
		}
	}

	return results, nil
}

// metricKey builds a unique key for a (metric name, labels) pair so that
// each metric stream is tracked independently.
func metricKey(name string, labels map[string]string) string {
	if len(labels) == 0 {
		return name
	}
	key := name
	for _, k := range []string{"pod", "container", "namespace", "node", "instance"} {
		if v, ok := labels[k]; ok {
			key += "/" + v
		}
	}
	return key
}

// entityName extracts the entity name from labels, falling back to the metric
// name when none of the standard label keys are present.
func entityName(metricName string, labels map[string]string) string {
	for _, k := range []string{"pod", "node", "container", "instance"} {
		if v, ok := labels[k]; ok {
			return v
		}
	}
	return metricName
}
