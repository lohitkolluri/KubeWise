package predictor

import (
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type DegradationPattern struct{}

func (d *DegradationPattern) Name() string { return "Degradation" }

func (d *DegradationPattern) Match(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []PatternMatch {
	var matches []PatternMatch

	// Check pod_not_ready metric
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
				confidence := 0.5 + pt.Value*0.1
				if confidence > 0.9 {
					confidence = 0.9
				}

				matches = append(matches, PatternMatch{
					Pattern:         "Degradation",
					Confidence:      confidence,
					Entity:          entity,
					Namespace:       namespace,
					SuggestedAction: "Investigate pod readiness and resource constraints",
				})
			}
		}
	}

	// Check node_disk_pressure
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
				matches = append(matches, PatternMatch{
					Pattern:         "Degradation",
					Confidence:      0.7,
					Entity:          entity,
					Namespace:       "",
					SuggestedAction: "Free up disk space or expand node storage",
				})
			}
		}
	}

	// Check node_memory_pressure
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
				if !hasEntity(matches, entity) {
					matches = append(matches, PatternMatch{
						Pattern:         "Degradation",
						Confidence:      0.65,
						Entity:          entity,
						Namespace:       "",
						SuggestedAction: "Reduce memory usage on node or add more nodes",
					})
				}
			}
		}
	}

	// Check failing pods in resource snapshot
	for _, pod := range resources.FailingPods {
		if !hasEntity(matches, pod) {
			matches = append(matches, PatternMatch{
				Pattern:         "Degradation",
				Confidence:      0.6,
				Entity:          pod,
				SuggestedAction: "Check pod logs and describe for failure details",
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
