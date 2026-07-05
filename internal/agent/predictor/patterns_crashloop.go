package predictor

import (
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type CrashLoopPattern struct{}

func (c *CrashLoopPattern) Name() string { return "CrashLoopRisk" }

func (c *CrashLoopPattern) Match(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []PatternMatch {
	restartResult := findMetric(metrics, "restart_rate")
	if restartResult == nil || len(restartResult.Values) == 0 {
		return nil
	}

	hasCrashLoopEvent := false
	crashPods := make(map[string]string)
	for _, ev := range events {
		if ev.Pattern == "CrashLoopBackOff" {
			hasCrashLoopEvent = true
			crashPods[ev.Entity] = ev.Namespace
		}
	}

	restartByEntity := groupByEntity(restartResult.Values)
	var matches []PatternMatch

	for entity, pts := range restartByEntity {
		if len(pts) < 2 {
			continue
		}

		trend := estimateTrend(pts)
		latestRate := pts[len(pts)-1].Value
		namespace := ""
		for _, p := range pts {
			if ns, ok := p.Labels["namespace"]; ok {
				namespace = ns
				break
			}
		}

		if latestRate < 0.01 && trend < 0.1 && !hasCrashLoopEvent {
			continue
		}

		confidence := 0.4
		if latestRate > 0.1 {
			confidence += 0.2
		}
		if trend > 0.2 {
			confidence += 0.15
		}
		if _, isCrashing := crashPods[entity]; isCrashing {
			confidence += 0.25
		}
		if contains(resources.FailingPods, entity) {
			confidence += 0.1
		}
		if confidence > 0.95 {
			confidence = 0.95
		}

		if confidence >= 0.5 {
			action := "Check container logs and fix startup errors"
			matches = append(matches, PatternMatch{
				Pattern:         "CrashLoopRisk",
				Confidence:      confidence,
				SuggestedAction: action,
				Entity:          entity,
				Namespace:       namespace,
			})
		}
	}
	return matches
}

func contains(slice []string, s string) bool {
	for _, v := range slice {
		if v == s {
			return true
		}
	}
	return false
}
