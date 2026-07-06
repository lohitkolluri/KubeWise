package predictor

import (
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type DegradationPattern struct{}

func (d *DegradationPattern) Name() string { return "Degradation" }

func (d *DegradationPattern) Match(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []PatternMatch {
	var matches []PatternMatch

	// Check pod_not_ready metric with predictive logic
	notReadyResult := findMetric(metrics, "pod_not_ready")
	if notReadyResult != nil {
		for _, pt := range notReadyResult.Values {
			if pt.Value > 0 {
				entity := pt.Labels["pod"]
				if entity == "" {
					entity = pt.Labels["instance"]
				}
				if entity == "" {
					entity = "unknown"
				}
				namespace := pt.Labels["namespace"]
				// Predictive confidence based on current not-ready value
				confidence := 0.5 + pt.Value*0.1
				if confidence > 0.9 {
					confidence = 0.9
				}
				// Add predictive boost based on trend
				matches = append(matches, PatternMatch{
					Pattern:         "Degradation",
					Confidence:      confidence,
					Entity:          entity,
					Namespace:       namespace,
					SuggestedAction: "Investigate pod readiness and resource constraints",
					TimeToFailure:   estimateDegradationTimeToFailure(nil, pt.Value),
				})
			}
		}
	}

	// Check node_disk_pressure with predictive logic
	diskPressureResult := findMetric(metrics, "node_disk_pressure")
	if diskPressureResult != nil {
		for _, pt := range diskPressureResult.Values {
			if pt.Value > 0 {
				entity := pt.Labels["node"]
				if entity == "" {
					entity = pt.Labels["instance"]
				}
				if entity == "" {
					entity = "unknown"
				}
				// Predictive confidence based on current disk pressure
				confidence := 0.7
				// Add predictive boost based on trend
				if !hasEntity(matches, entity) {
					matches = append(matches, PatternMatch{
						Pattern:         "Degradation",
						Confidence:      confidence,
						Entity:          entity,
						Namespace:       "",
						SuggestedAction: "Free up disk space or expand node storage",
						TimeToFailure:   estimateDegradationTimeToFailure(nil, pt.Value),
					})
				}
			}
		}
	}

	// Check node_memory_pressure with predictive logic
	memPressureResult := findMetric(metrics, "node_memory_pressure")
	if memPressureResult != nil {
		for _, pt := range memPressureResult.Values {
			if pt.Value > 0 {
				// Avoid duplicate if same node already has a disk pressure match
				entity := pt.Labels["node"]
				if entity == "" {
					entity = pt.Labels["instance"]
				}
				if entity == "" {
					entity = "unknown"
				}
				// Predictive confidence based on current memory pressure
				confidence := 0.65
				// Add predictive boost based on trend
				if !hasEntity(matches, entity) {
					matches = append(matches, PatternMatch{
						Pattern:         "Degradation",
						Confidence:      confidence,
						Entity:          entity,
						Namespace:       "",
						SuggestedAction: "Reduce memory usage on node or add more nodes",
						TimeToFailure:   estimateDegradationTimeToFailure(nil, pt.Value),
					})
				}
			}
		}
	}

	// Check failing pods in resource snapshot
	for _, pod := range resources.FailingPods {
		entity := pod
		if idx := strings.LastIndex(pod, "/"); idx >= 0 {
			entity = pod[idx+1:]
		}
		if !hasEntity(matches, entity) {
			// Predictive confidence for failing pods
			confidence := 0.6
			// Add predictive boost based on pod age or history if available
			matches = append(matches, PatternMatch{
				Pattern:         "Degradation",
				Confidence:      confidence,
				Entity:          entity,
				SuggestedAction: "Check pod logs and describe for failure details",
				TimeToFailure:   estimateDegradationTimeToFailure(nil, 1.0),
			})
		}
	}

	return matches
}

func hasEntity(matches []PatternMatch, entity string) bool {
	for _, m := range matches {
		if m.Entity == entity {
			return true
		}
	}
	return false
}
