package predictor

import (
	"sort"
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type DegradationPattern struct{}

func (d *DegradationPattern) Name() string { return "Degradation" }

func (d *DegradationPattern) Match(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []PatternMatch {
	var matches []PatternMatch

	if notReadyResult, ok := findMetric(metrics, "pod_not_ready"); ok {
		latest := latestPointsByEntity(notReadyResult.Values)
		for entityKey, pt := range latest {
			if pt.Value <= 0 {
				continue
			}
			val := pt.Value
			if val > 1 {
				val = 1
			}
			ns, entity := splitEntityKey(entityKey)
			confidence := 0.5 + val*0.1
			if confidence > 0.9 {
				confidence = 0.9
			}
			history := historyForEntity(notReadyResult.Values, entityKey)
			matches = append(matches, PatternMatch{
				Pattern:         "Degradation",
				Confidence:      confidence,
				Entity:          entity,
				Namespace:       ns,
				SuggestedAction: "Investigate pod readiness and resource constraints",
				TimeToFailure:   estimateDegradationTimeToFailure(history, val),
			})
		}
	}

	if diskPressureResult, ok := findMetric(metrics, "node_disk_pressure"); ok {
		latest := latestPointsByEntity(diskPressureResult.Values)
		for entityKey, pt := range latest {
			if pt.Value <= 0 {
				continue
			}
			_, entity := splitEntityKey(entityKey)
			if !hasEntityKey(matches, entityKey) {
				history := historyForEntity(diskPressureResult.Values, entityKey)
				matches = append(matches, PatternMatch{
					Pattern:         "Degradation",
					Confidence:      0.7,
					Entity:          entity,
					SuggestedAction: "Free up disk space or expand node storage",
					TimeToFailure:   estimateDegradationTimeToFailure(history, pt.Value),
				})
			}
		}
	}

	if memPressureResult, ok := findMetric(metrics, "node_memory_pressure"); ok {
		latest := latestPointsByEntity(memPressureResult.Values)
		for entityKey, pt := range latest {
			if pt.Value <= 0 {
				continue
			}
			_, entity := splitEntityKey(entityKey)
			if !hasEntityKey(matches, entityKey) {
				history := historyForEntity(memPressureResult.Values, entityKey)
				matches = append(matches, PatternMatch{
					Pattern:         "Degradation",
					Confidence:      0.65,
					Entity:          entity,
					SuggestedAction: "Reduce memory usage on node or add more nodes",
					TimeToFailure:   estimateDegradationTimeToFailure(history, pt.Value),
				})
			}
		}
	}

	for _, pod := range resources.FailingPods {
		if !hasEntityKey(matches, pod) {
			ns, name := models.ParseEntity(pod)
			matches = append(matches, PatternMatch{
				Pattern:         "Degradation",
				Confidence:      0.6,
				Entity:          name,
				Namespace:       ns,
				SuggestedAction: "Check pod logs and describe for failure details",
				TimeToFailure:   estimateDegradationTimeToFailure(nil, 1.0),
			})
		}
	}

	return matches
}

func historyForEntity(values []MetricPoint, key string) []MetricPoint {
	var history []MetricPoint
	for _, pt := range values {
		if entityKey(pt.Labels) == key {
			history = append(history, pt)
		}
	}
	sort.Slice(history, func(i, j int) bool {
		return history[i].Timestamp.Before(history[j].Timestamp)
	})
	return history
}

// latestPointsByEntity keeps the newest point per entity (namespace-aware when present).
func latestPointsByEntity(values []MetricPoint) map[string]MetricPoint {
	latest := make(map[string]MetricPoint)
	for _, pt := range values {
		key := entityKey(pt.Labels)
		if key == "" || key == "/" {
			continue
		}
		if existing, ok := latest[key]; !ok || pt.Timestamp.After(existing.Timestamp) {
			latest[key] = pt
		}
	}
	return latest
}

func splitEntityKey(key string) (namespace, entity string) {
	if idx := strings.Index(key, "/"); idx >= 0 {
		return key[:idx], key[idx+1:]
	}
	return "", key
}

func hasEntityKey(matches []PatternMatch, entityKey string) bool {
	ns, name := splitEntityKey(entityKey)
	for _, m := range matches {
		matchKey := m.Entity
		if m.Namespace != "" {
			matchKey = models.FormatEntity(m.Namespace, m.Entity)
		}
		if matchKey == entityKey || (ns == "" && m.Entity == name) {
			return true
		}
	}
	return false
}
