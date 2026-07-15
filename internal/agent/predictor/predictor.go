package predictor

import (
	"log/slog"
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
	// streak counts consecutive scrapes above MinScore per metric key.
	// This suppresses one-off spikes (reduces false positives).
	streak          map[string]int
	patternTick     int
	patternStreak   map[string]int
	patternLastSeen map[string]int
	patternLastEmit map[string]int
	patterns        []PatternMatcher

	// lastSeen tracks the last access time for each metric key, used by the
	// cleanup goroutine to prune stale state from in-memory maps.
	lastSeen      map[string]time.Time
	cleanupStopCh chan struct{}
}

// NewPredictor creates a Predictor wired with the given scorer configuration.
func NewPredictor(config ScorerConfig) *Predictor {
	return &Predictor{
		estimators:      make(map[string]*AdaptiveMedian),
		changepoints:    make(map[string]*ChangepointDetector),
		roc:             &RateOfChange{},
		scorer:          NewScorer(config),
		config:          config,
		history:         make(map[string][]MetricPoint),
		patternHistory:  make(map[string][]MetricPoint),
		datapoints:      make(map[string]int),
		streak:          make(map[string]int),
		patternStreak:   make(map[string]int),
		patternLastSeen: make(map[string]int),
		patternLastEmit: make(map[string]int),
		lastSeen:        make(map[string]time.Time),
		cleanupStopCh:   make(chan struct{}),
	}
}

// StartCleanup launches a background goroutine that periodically prunes stale keys
// from in-memory maps (estimators, changepoints, history, streak, datapoints).
// Keys not seen within the last hour are removed.
func (p *Predictor) StartCleanup() {
	go func() {
		ticker := time.NewTicker(10 * time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				p.pruneStaleKeys()
			case <-p.cleanupStopCh:
				return
			}
		}
	}()
}

// StopCleanup signals the cleanup goroutine to stop.
func (p *Predictor) StopCleanup() {
	select {
	case <-p.cleanupStopCh:
	default:
		close(p.cleanupStopCh)
	}
}

// pruneStaleKeys removes entries from in-memory maps that have not been seen
// within the last hour.
func (p *Predictor) pruneStaleKeys() {
	const staleThreshold = 1 * time.Hour
	cutoff := time.Now().Add(-staleThreshold)

	p.mu.Lock()
	defer p.mu.Unlock()

	for key, last := range p.lastSeen {
		if last.Before(cutoff) {
			delete(p.estimators, key)
			delete(p.changepoints, key)
			delete(p.history, key)
			delete(p.datapoints, key)
			delete(p.streak, key)
			delete(p.lastSeen, key)
			slog.Debug("predictor: pruned stale key", "key", key)
		}
	}
}

// LoadPatternHistory seeds in-memory pattern history from persisted metric series.
func (p *Predictor) LoadPatternHistory(s *store.Store, limit int) {
	if s == nil || limit <= 0 {
		return
	}
	names := []string{
		"pod_memory_usage", "restart_rate",
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
	p.mu.Lock()
	p.patternTick++
	tick := p.patternTick
	p.mu.Unlock()

	required := p.config.PatternPersistence
	if required <= 0 {
		required = 1
	}
	cooldown := p.config.PatternCooldownScrapes
	if cooldown < 0 {
		cooldown = 0
	}

	for _, pat := range patterns {
		matches := pat.Match(metrics, events, resources)
		for _, m := range matches {
			// Debounce pattern predictions: require consecutive matches + cooldown.
			key := m.Pattern + "/" + m.Namespace + "/" + m.Entity
			emit := false
			p.mu.Lock()
			lastSeen := p.patternLastSeen[key]
			if lastSeen != tick-1 {
				p.patternStreak[key] = 0
			}
			p.patternStreak[key]++
			p.patternLastSeen[key] = tick
			if p.patternStreak[key] >= required {
				if tick-p.patternLastEmit[key] >= cooldown {
					p.patternLastEmit[key] = tick
					emit = true
				}
			}
			p.mu.Unlock()
			if emit {
				results = append(results, patternToResult(m))
			}
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

	// Track per-key stats for the diagnostic summary at the end of Run().
	type metricStat struct {
		dp    int
		score float64
	}
	seen := make(map[string]*metricStat)

	for _, mr := range metrics {
		if len(mr.Values) == 0 {
			continue
		}

		for _, pt := range mr.Values {
			key := metricKey(mr.Name, pt.Labels)
			profile := ProfileForMetric(mr.Name)

			// --- state access -------------------------------------------------------
			p.mu.Lock()

			p.lastSeen[key] = time.Now()
			p.datapoints[key]++
			dp := p.datapoints[key]

			est, ok := p.estimators[key]
			if !ok {
				est = NewAdaptiveMedian(DefaultAdaptiveMedianWindow)
				p.estimators[key] = est
			}

			cp, ok := p.changepoints[key]
			if !ok {
				cp = NewChangepointDetector(DefaultChangepointWindow, DefaultChangepointMinSample, DefaultChangepointBlockSize, DefaultChangepointConfidence)
				p.changepoints[key] = cp
			}

			// Keep a bounded history for ROC computations. Copy under lock
			// for safe concurrent access.
			p.history[key] = append(p.history[key], pt)
			if len(p.history[key]) > maxPatternHistory {
				p.history[key] = p.history[key][len(p.history[key])-maxPatternHistory:]
			}
			localHistory := make([]MetricPoint, len(p.history[key]))
			copy(localHistory, p.history[key])

			p.mu.Unlock()
			// --- warmup phase ------------------------------------------------------
			est.Add(pt.Value)

			if _, tracked := seen[key]; !tracked {
				seen[key] = &metricStat{dp: dp, score: -1}
			}
			if dp < p.config.MinWarmup {
				continue
			}

			// --- changepoint detection (adds to buffer AND returns detection flag) -
			if cp.Add(pt.Value) {
				est.Reset()
				est.Add(pt.Value)
				p.mu.Lock()
				p.history[key] = nil
				p.datapoints[key] = 1
				p.mu.Unlock()
				continue
			}

			// --- adaptive-median + strategy-specific scoring ------------------------
			median, mad, _, n, ok := est.Stats()
			if !ok || n < p.config.MinWarmup {
				continue
			}

			rzScore := RobustAnomalyScore(pt.Value, median, mad)

			var primaryScore float64
			switch profile.Strategy {
			case StrategyChangepoint:
				// Changepoint-detected anomalies get a baseline score;
				// the burst of recent changepoints IS the anomaly signal.
				primaryScore = rzScore
				if rzScore < 0.3 {
					primaryScore = 0.3
				}
			case StrategyCombinedOR:
				primaryScore = rzScore
				if rzScore < 0.3 {
					primaryScore = 0.3
				}
			default:
				primaryScore = rzScore
			}

			// --- rate-of-change boost -----------------------------------------------
			rocBoost := 0.0
			history := localHistory
			if len(history) >= 4 {
				vel := p.roc.Velocity(history)
				if math.Abs(vel.Slope) > 0 {
					relSlope := math.Abs(vel.Slope) / math.Max(math.Abs(median), 1.0)
					rocBoost = math.Min(relSlope*5.0, p.config.ROCBoostWeight)
				}
			}

			// --- final score --------------------------------------------------------
			score := p.scorer.Score(primaryScore, rocBoost)

			if prev := seen[key]; prev != nil {
				prev.score = score
				prev.dp = dp
			}

			// Persistence: require >=N consecutive scrapes above threshold before emitting.
			// This cuts false positives from single-sample spikes.
			// Profile values override config when non-zero (benchmark-tuned per metric family).
			emit := false
			required := profile.Persistence
			if required <= 0 {
				required = p.config.Persistence
				if required <= 0 {
					required = 1
				}
			}
			minScore := profile.MinScore
			if minScore <= 0 {
				minScore = p.config.MinScore
			}
			p.mu.Lock()
			if score >= minScore {
				p.streak[key]++
				if p.streak[key] >= required {
					emit = true
				}
			} else {
				p.streak[key] = 0
			}
			p.mu.Unlock()

			if emit {
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

	// --- diagnostic summary ----------------------------------------------------
	loggedKeys := len(seen)
	warmup := 0
	maxScore := 0.0
	maxKey := ""
	aboveThreshold := 0
	belowThreshold := 0
	for k, st := range seen {
		switch {
		case st.dp < p.config.MinWarmup:
			warmup++
		case st.score >= p.config.MinScore:
			aboveThreshold++
			if st.score > maxScore {
				maxScore = st.score
				maxKey = k
			}
		case st.score >= 0:
			belowThreshold++
			if st.score > maxScore {
				maxScore = st.score
				maxKey = k
			}
		}
	}
	if loggedKeys > 0 {
		slog.Info("predictor: scoring stats",
			"keys", loggedKeys,
			"warmup", warmup,
			"scoring", loggedKeys-warmup,
			"above_min", aboveThreshold,
			"below_min", belowThreshold,
			"max_score", maxScore,
			"max_key", maxKey,
		)
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
		if v, ok := labels[k]; ok && v != "" {
			key += "/" + k + "=" + v
		}
	}
	return key
}

// entityName extracts the entity name from labels, falling back to the metric
// name when none of the standard label keys are present.
func entityName(metricName string, labels map[string]string) string {
	for _, k := range []string{"pod", "node", "container", "instance"} {
		if v, ok := labels[k]; ok && v != "" {
			return v
		}
	}
	return metricName
}
