// Package predictor provides failure pattern detection and prediction logic.
package predictor

import (
	"maps"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const (
	defaultMemLimitBytes = 1 << 30 // 1 GiB fallback when limit unknown
	oomRatioThreshold    = 0.95
	crashLoopRateLimit   = 1.0
	maxPatternHistory    = 20
)

// MaxPatternHistory is the number of cross-scrape points kept per metric series.
const MaxPatternHistory = maxPatternHistory

var scrapeInterval = 30 * time.Second

// SetScrapeInterval configures the scrape interval used for ETA projections.
func SetScrapeInterval(d time.Duration) {
	if d > 0 {
		scrapeInterval = d
	}
}

// PodResource describes a pod's resource limits.
type PodResource struct {
	Name      string
	Namespace string
	CPULimit  float64
	MemLimit  float64
}

// ResourceSnapshot is a point-in-time view of cluster resource health.
type ResourceSnapshot struct {
	FailingPods    []string
	UnhealthyNodes []string
	PodResources   []PodResource
}

// PatternMatch represents a detected failure pattern with confidence and suggested action.
type PatternMatch struct {
	Pattern         string
	Confidence      float64
	TimeToFailure   time.Duration
	SuggestedAction string
	Entity          string
	Namespace       string
}

// PatternMatcher is implemented by types that can detect a specific failure pattern.
type PatternMatcher interface {
	Name() string
	Match(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []PatternMatch
}

func patternToResult(m PatternMatch) models.PredictionResult {
	return models.PredictionResult{
		Type:       "pattern",
		Entity:     m.Entity,
		Namespace:  m.Namespace,
		MetricName: m.Pattern,
		Action:     m.SuggestedAction,
		Confidence: m.Confidence,
		Score:      m.Confidence,
		ETASeconds: m.TimeToFailure.Seconds(),
		Timestamp:  time.Now(),
	}
}

// entityKey returns a unique key for an entity including namespace when present.
func entityKey(labels map[string]string) string {
	ns := labels["namespace"]
	entity := labels["pod"]
	if entity == "" {
		entity = labels["node"]
	}
	if entity == "" {
		entity = labels["container"]
	}
	if entity == "" {
		entity = labels["instance"]
	}
	if entity == "" {
		entity = "unknown"
	}
	if ns != "" {
		return ns + "/" + entity
	}
	return entity
}

// entityNameFromKey extracts the display entity name from an entityKey.
func entityNameFromKey(key string) string {
	if idx := strings.LastIndex(key, "/"); idx >= 0 {
		return key[idx+1:]
	}
	return key
}

// namespaceFromKey extracts namespace from an entityKey.
func namespaceFromKey(key string) string {
	if idx := strings.Index(key, "/"); idx >= 0 {
		return key[:idx]
	}
	return ""
}

func groupByEntity(pts []MetricPoint) map[string][]MetricPoint {
	result := make(map[string][]MetricPoint)
	for _, pt := range pts {
		key := entityKey(pt.Labels)
		result[key] = append(result[key], pt)
	}
	return result
}

func aggregatePodMemorySeries(pts []MetricPoint) []MetricPoint {
	if len(pts) == 0 {
		return nil
	}
	byTS := make(map[int64]float64)
	labels := maps.Clone(pts[0].Labels)
	for _, p := range pts {
		byTS[p.Timestamp.UnixNano()] += p.Value
	}
	result := make([]MetricPoint, 0, len(byTS))
	for ts, val := range byTS {
		result = append(result, MetricPoint{
			Value:     val,
			Timestamp: time.Unix(0, ts),
			Labels:    maps.Clone(labels),
		})
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].Timestamp.Before(result[j].Timestamp)
	})
	return result
}

func aggregatePodRestartSeries(pts []MetricPoint) []MetricPoint {
	if len(pts) == 0 {
		return nil
	}
	containers := make(map[string]struct{})
	for _, p := range pts {
		if c := p.Labels["container"]; c != "" {
			containers[c] = struct{}{}
		}
	}
	if len(containers) <= 1 {
		sorted := slices.Clone(pts)
		sort.Slice(sorted, func(i, j int) bool {
			return sorted[i].Timestamp.Before(sorted[j].Timestamp)
		})
		return sorted
	}

	byTS := make(map[int64]float64)
	labels := make(map[string]string)
	for k, v := range pts[0].Labels {
		labels[k] = v
	}
	for _, p := range pts {
		ts := p.Timestamp.UnixNano()
		if p.Value > byTS[ts] {
			byTS[ts] = p.Value
		}
	}
	result := make([]MetricPoint, 0, len(byTS))
	for ts, val := range byTS {
		result = append(result, MetricPoint{
			Value:     val,
			Timestamp: time.Unix(0, ts),
			Labels:    labels,
		})
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].Timestamp.Before(result[j].Timestamp)
	})
	return result
}

func lookupMemLimit(resources ResourceSnapshot, namespace, pod string) float64 {
	for _, pr := range resources.PodResources {
		if pr.Name != pod {
			continue
		}
		if namespace != "" && pr.Namespace != "" && pr.Namespace != namespace {
			continue
		}
		if namespace == "" && pr.Namespace != "" {
			continue
		}
		if pr.MemLimit > 0 {
			return pr.MemLimit
		}
	}
	return defaultMemLimitBytes
}

func hasEvent(events []models.AnomalyRecord, namespace, entity, pattern string) bool {
	for _, e := range events {
		if e.Pattern != pattern {
			continue
		}
		eNs, eName := models.ParseEntity(e.Entity)
		if e.Namespace != "" && eNs == "" {
			eNs = e.Namespace
		}
		if eName == entity && eNs == namespace {
			return true
		}
	}
	return false
}

func contains(slice []string, s string) bool {
	for _, v := range slice {
		if v == s {
			return true
		}
	}
	return false
}

// perStepDelta returns the change per scrape between the last two points.
func perStepDelta(pts []MetricPoint) float64 {
	if len(pts) < 2 {
		return 0
	}
	return pts[len(pts)-1].Value - pts[len(pts)-2].Value
}

func estimateOOMTimeToFailure(pts []MetricPoint, usageRatio float64) time.Duration {
	if usageRatio >= oomRatioThreshold {
		return 0
	}
	growth := perStepDelta(pts)
	if growth <= 0 {
		if usageRatio >= 0.85 {
			return time.Hour
		}
		if usageRatio >= 0.6 {
			return 6 * time.Hour
		}
		return 24 * time.Hour
	}
	steps := (oomRatioThreshold - usageRatio) / growth
	maxSteps := float64(24 * time.Hour / scrapeInterval)
	if steps > maxSteps {
		steps = maxSteps
	}
	return time.Duration(steps * float64(scrapeInterval))
}

func estimateCrashLoopTimeToFailure(pts []MetricPoint, currentRate float64) time.Duration {
	if currentRate >= crashLoopRateLimit {
		return 0
	}
	growth := perStepDelta(pts)
	if growth <= 0 {
		return 0
	}
	steps := (crashLoopRateLimit - currentRate) / growth
	maxSteps := float64(24 * time.Hour / scrapeInterval)
	if steps > maxSteps {
		steps = maxSteps
	}
	return time.Duration(steps * float64(scrapeInterval))
}

func estimateDegradationTimeToFailure(pts []MetricPoint, currentValue float64) time.Duration {
	growth := perStepDelta(pts)
	if growth <= 0 {
		if currentValue > 0.8 {
			return time.Hour
		}
		if currentValue > 0.5 {
			return 4 * time.Hour
		}
		return 24 * time.Hour
	}
	threshold := 1.0
	if currentValue >= threshold {
		return 0
	}
	steps := (threshold - currentValue) / growth
	maxSteps := float64(7 * 24 * time.Hour / scrapeInterval)
	if steps > maxSteps {
		steps = maxSteps
	}
	return time.Duration(steps * float64(scrapeInterval))
}

func findMetric(metrics []MetricResult, name string) (MetricResult, bool) {
	for _, m := range metrics {
		if m.Name == name {
			return m, true
		}
	}
	return MetricResult{}, false
}

func estimateTrend(pts []MetricPoint) float64 {
	if len(pts) < 2 {
		return 0
	}
	first := pts[0].Value
	last := pts[len(pts)-1].Value
	if first == 0 {
		return 0
	}
	return (last - first) / first
}
