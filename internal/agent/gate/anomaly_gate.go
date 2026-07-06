package gate

import (
	"sync"
	"sync/atomic"
	"time"
)

// DefaultConfig returns a Config with sensible production defaults.
func DefaultConfig() Config {
	return Config{
		Threshold:         0.3,
		SustainCount:      3,
		CooldownDuration:  5 * time.Minute,
		ScrapeInterval:    30 * time.Second,
		CorrelationWindow: 60 * time.Second,
		HighScoreBypass:   0.8,
		BypassPatterns:    true,
	}
}

// Config holds the configuration for the AnomalyGate.
type Config struct {
	// Threshold is the minimum score to consider for sustainment (default: 0.3)
	Threshold float64
	// SustainCount is the number of consecutive scores >= Threshold required to pass (default: 3)
	SustainCount int
	// CooldownDuration is how long to suppress after a pass (default: 5 minutes)
	CooldownDuration time.Duration
	// ScrapeInterval is the expected interval between scrapes (default: 30 seconds)
	ScrapeInterval time.Duration
	// CorrelationWindow is the time window to look for correlated metrics (default: 2*ScrapeInterval)
	CorrelationWindow time.Duration
	// HighScoreBypass is the score threshold for immediate bypass (default: 0.8)
	HighScoreBypass float64
	// BypassPatterns indicates whether pattern-based results should bypass the gate (default: true)
	BypassPatterns bool
}

// Result represents the outcome of filtering an anomaly.
type Result struct {
	// Pass indicates whether the anomaly should be stored (true) or dropped (false)
	Pass bool
	// Reason explains why the decision was made
	Reason string
}

// entityHistory tracks the score history for a single entity.
type entityHistory struct {
	// scores holds the recent scores for sustainment checking
	scores []float64
	// lastSeen is the timestamp of the last score received for this entity
	lastSeen time.Time
	// lastPassedScores holds the scores that last passed the gate for this entity (for cooldown escalation)
	lastPassedScores []float64
}

// Stats tracks gate evaluation counters since process start.
type Stats struct {
	Observed uint64
	Passed   uint64
	Dropped  uint64
}

// AnomalyGate filters anomalies based on sustainment, correlation, and bypass rules.
type AnomalyGate struct {
	config    Config
	history   map[string]*entityHistory
	cooldowns map[string]time.Time
	metrics   map[string]map[string]time.Time
	stats     Stats
	mu        sync.Mutex
}

// NewGate creates a new AnomalyGate with the given config.
// Zero values in config are replaced with sensible defaults.
func NewGate(config Config) *AnomalyGate {
	// Set defaults for zero values
	if config.Threshold == 0 {
		config.Threshold = 0.3
	}
	if config.SustainCount == 0 {
		config.SustainCount = 3
	}
	if config.CooldownDuration == 0 {
		config.CooldownDuration = 5 * time.Minute
	}
	if config.ScrapeInterval == 0 {
		config.ScrapeInterval = 30 * time.Second
	}
	if config.CorrelationWindow == 0 {
		config.CorrelationWindow = 2 * config.ScrapeInterval
	}
	if config.HighScoreBypass == 0 {
		config.HighScoreBypass = 0.8
	}

	return &AnomalyGate{
		config:    config,
		history:   make(map[string]*entityHistory),
		cooldowns: make(map[string]time.Time),
		metrics:   make(map[string]map[string]time.Time),
	}
}

// StatsSnapshot returns a copy of gate counters.
func (g *AnomalyGate) StatsSnapshot() Stats {
	return Stats{
		Observed: atomic.LoadUint64(&g.stats.Observed),
		Passed:   atomic.LoadUint64(&g.stats.Passed),
		Dropped:  atomic.LoadUint64(&g.stats.Dropped),
	}
}

// ShouldPersist reports whether an anomaly should be stored and eventually
// reach the LLM correlator. This is the pre-LLM false-positive gate.
func (g *AnomalyGate) ShouldPersist(entity string, metricName string, score float64, resultType string, now time.Time) (bool, string) {
	r := g.Filter(entity, metricName, score, resultType, now)
	return r.Pass, r.Reason
}

// GatePrediction is a single prediction input for batch gate evaluation.
type GatePrediction struct {
	Entity     string
	MetricName string
	Score      float64
	Type       string
}

// FilterBatch evaluates multiple predictions in order, returning only those
// that pass the gate. Useful for dry-run / test simulations.
func (g *AnomalyGate) FilterBatch(predictions []GatePrediction, now time.Time) []Result {
	results := make([]Result, 0, len(predictions))
	for _, p := range predictions {
		r := g.Filter(p.Entity, p.MetricName, p.Score, p.Type, now)
		if r.Pass {
			results = append(results, r)
		}
	}
	return results
}

// ObserveScore records a score for sustainment tracking without evaluating pass rules.
// Call on every scrape (including sub-threshold) so sustainment reflects consecutive scrapes.
func (g *AnomalyGate) ObserveScore(entity string, metricName string, score float64, now time.Time) {
	g.mu.Lock()
	defer g.mu.Unlock()

	hKey := historyKey(entity, metricName)
	if g.history[hKey] == nil {
		g.history[hKey] = &entityHistory{
			scores:           make([]float64, 0),
			lastSeen:         now,
			lastPassedScores: make([]float64, 0),
		}
	}
	g.recordSustainmentScore(hKey, score, now)
	atomic.AddUint64(&g.stats.Observed, 1)
	g.pruneStaleLocked(now, now.Add(-24*time.Hour))
}

// PruneStale removes gate state for entities not seen within maxAge.
func (g *AnomalyGate) PruneStale(now time.Time, maxAge time.Duration) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.pruneStaleLocked(now, now.Add(-maxAge))
}

func (g *AnomalyGate) pruneStaleLocked(now, cutoff time.Time) {
	for key, hist := range g.history {
		if hist.lastSeen.Before(cutoff) {
			delete(g.history, key)
		}
	}
	for entity, metrics := range g.metrics {
		for metric, ts := range metrics {
			if ts.Before(cutoff) {
				delete(metrics, metric)
			}
		}
		if len(metrics) == 0 {
			delete(g.metrics, entity)
		}
	}
	for entity, until := range g.cooldowns {
		if until.Before(now) {
			delete(g.cooldowns, entity)
		}
	}
}

func (g *AnomalyGate) recordSustainmentScore(hKey string, score float64, now time.Time) {
	history := g.history[hKey]
	if score < g.config.Threshold {
		history.scores = make([]float64, 0)
		history.lastSeen = now
		return
	}
	if now.Sub(history.lastSeen) > 2*g.config.ScrapeInterval {
		history.scores = make([]float64, 0)
	}
	history.scores = append(history.scores, score)
	history.lastSeen = now
	if len(history.scores) > g.config.SustainCount {
		history.scores = history.scores[len(history.scores)-g.config.SustainCount:]
	}
}

// Filter determines whether an anomaly should be passed through the gate.
// It applies rules in this order:
// 1. Pattern bypass (if enabled and resultType is "pattern")
// 2. High-score bypass (score >= HighScoreBypass)
// 3. Multi-metric correlation (>=2 different metrics for same entity in CorrelationWindow)
// 4. Sustainment (SustainCount consecutive scores >= Threshold)
// 5. Otherwise drop
func (g *AnomalyGate) Filter(entity string, metricName string, score float64, resultType string, now time.Time) Result {
	g.mu.Lock()
	defer g.mu.Unlock()

	hKey := historyKey(entity, metricName)

	// Initialize entity history if not present
	if g.history[hKey] == nil {
		g.history[hKey] = &entityHistory{
			scores:           make([]float64, 0),
			lastSeen:         now,
			lastPassedScores: make([]float64, 0),
		}
	}
	// Initialize metrics map for entity if not present
	if g.metrics[entity] == nil {
		g.metrics[entity] = make(map[string]time.Time)
	}

	g.recordSustainmentScore(hKey, score, now)
	atomic.AddUint64(&g.stats.Observed, 1)

	// Rule 1: Pattern bypass
	if g.config.BypassPatterns && (resultType == "pattern" || resultType == "event") {
		if g.checkCooldownAndRecord(entity, metricName, score, now) {
			atomic.AddUint64(&g.stats.Passed, 1)
			return Result{Pass: true, Reason: "pattern_bypass"}
		}
		atomic.AddUint64(&g.stats.Dropped, 1)
		return Result{Pass: false, Reason: "cooldown_active"}
	}

	// Rule 2: High-score bypass
	if score >= g.config.HighScoreBypass {
		if g.checkCooldownAndRecord(entity, metricName, score, now) {
			atomic.AddUint64(&g.stats.Passed, 1)
			return Result{Pass: true, Reason: "high_score_bypass"}
		}
		atomic.AddUint64(&g.stats.Dropped, 1)
		return Result{Pass: false, Reason: "cooldown_active"}
	}

	// Rule 3: Multi-metric correlation
	if score >= g.config.Threshold && g.isCorrelated(entity, metricName, now) {
		if g.checkCooldownAndRecord(entity, metricName, score, now) {
			atomic.AddUint64(&g.stats.Passed, 1)
			return Result{Pass: true, Reason: "multi_metric_correlation"}
		}
		atomic.AddUint64(&g.stats.Dropped, 1)
		return Result{Pass: false, Reason: "cooldown_active"}
	}

	// Rule 4: Sustainment — use pre-recorded scores from ObserveScore
	if g.isSustainedFromHistory(hKey) {
		if g.checkCooldownAndRecord(entity, metricName, score, now) {
			atomic.AddUint64(&g.stats.Passed, 1)
			return Result{Pass: true, Reason: "sustainment"}
		}
		atomic.AddUint64(&g.stats.Dropped, 1)
		return Result{Pass: false, Reason: "cooldown_active"}
	}

	// Default: drop
	atomic.AddUint64(&g.stats.Dropped, 1)
	return Result{Pass: false, Reason: "no_rule_triggered"}
}

// checkCooldownAndRecord checks if the entity is in cooldown and updates state on pass.
// Returns true if the check passes (either not in cooldown or cooldown overridden by escalation).
func (g *AnomalyGate) checkCooldownAndRecord(entity, metricName string, score float64, now time.Time) bool {
	hKey := historyKey(entity, metricName)
	// Check per-signal cooldown
	if cooldownUntil, exists := g.cooldowns[hKey]; exists {
		if now.Before(cooldownUntil) {
			// In cooldown - check for escalation override
			history := g.history[hKey]
			if history != nil && len(history.lastPassedScores) > 0 {
				// Find the maximum score from last passed scores
				var maxLastPassed float64
				for _, s := range history.lastPassedScores {
					if s > maxLastPassed {
						maxLastPassed = s
					}
				}
				// If current score is higher than last passed score, allow escalation
				if score > maxLastPassed {
					g.cooldowns[hKey] = now.Add(g.config.CooldownDuration)
					// Update last passed scores
					history.lastPassedScores = append(history.lastPassedScores, score)
					// Keep only last SustainCount scores
					if len(history.lastPassedScores) > g.config.SustainCount {
						history.lastPassedScores = history.lastPassedScores[len(history.lastPassedScores)-g.config.SustainCount:]
					}
					return true
				}
			}
			// Still in cooldown and no escalation
			return false
		}
		// Cooldown expired, remove it
		delete(g.cooldowns, hKey)
	}

	g.cooldowns[hKey] = now.Add(g.config.CooldownDuration)
	history := g.history[hKey]
	if history == nil {
		return true
	}
	history.lastPassedScores = append(history.lastPassedScores, score)
	// Keep only last SustainCount scores
	if len(history.lastPassedScores) > g.config.SustainCount {
		history.lastPassedScores = history.lastPassedScores[len(history.lastPassedScores)-g.config.SustainCount:]
	}
	return true
}

func (g *AnomalyGate) isSustainedFromHistory(hKey string) bool {
	history := g.history[hKey]
	if len(history.scores) < g.config.SustainCount {
		return false
	}
	for _, s := range history.scores {
		if s < g.config.Threshold {
			return false
		}
	}
	return true
}

// historyKey combines entity and metric for independent sustainment tracking.
func historyKey(entity, metricName string) string {
	return entity + "|" + metricName
}

// isCorrelated checks if there are at least 2 different metrics for this entity
// within the CorrelationWindow. It records the current metric and cleans old entries.
func (g *AnomalyGate) isCorrelated(entity string, metricName string, now time.Time) bool {
	// Record this metric
	g.metrics[entity][metricName] = now

	// Clean stale entries (older than CorrelationWindow)
	for metric, ts := range g.metrics[entity] {
		if now.Sub(ts) > g.config.CorrelationWindow {
			delete(g.metrics[entity], metric)
		}
	}

	// Count distinct metrics in window
	count := 0
	for range g.metrics[entity] {
		count++
		if count >= 2 {
			return true
		}
	}
	return false
}
