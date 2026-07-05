package predictor

import (
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const oomMinConfidence = 0.5
const memoryMetric = "pod_memory_usage"

type OOMPattern struct{}

func (o *OOMPattern) Name() string { return "OOMRisk" }

func (o *OOMPattern) Match(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []PatternMatch {
	memResult := findMetric(metrics, memoryMetric)
	if memResult == nil || len(memResult.Values) == 0 {
		return nil
	}

	hasOOMEvent := false
	oomPods := make(map[string]string)
	for _, ev := range events {
		if ev.Pattern == "OOMKilled" || ev.Pattern == "OOMKill" {
			hasOOMEvent = true
			oomPods[ev.Entity] = ev.Namespace
		}
	}

	if !hasOOMEvent {
		return nil
	}

	memByEntity := groupByEntity(memResult.Values)
	var matches []PatternMatch

	for entity, pts := range memByEntity {
		if len(pts) < 2 {
			continue
		}
		trend := estimateTrend(pts)
		namespace := ""
		for _, p := range pts {
			if ns, ok := p.Labels["namespace"]; ok {
				namespace = ns
				break
			}
		}

		confidence := 0.5
		if trend > 0.05 {
			confidence = 0.5 + trend*2.0
		}
		if _, wasOOM := oomPods[entity]; wasOOM {
			confidence += 0.2
		}
		if confidence > 0.95 {
			confidence = 0.95
		}

		if confidence >= oomMinConfidence {
			action := "Increase memory limits or reduce memory usage"
			matches = append(matches, PatternMatch{
				Pattern:         "OOMRisk",
				Confidence:      confidence,
				SuggestedAction: action,
				Entity:          entity,
				Namespace:       namespace,
				TimeToFailure:   0,
			})
		}
	}
	return matches
}
