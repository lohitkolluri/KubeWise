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

	restartByEntity := groupByEntity(restartResult.Values)
	var matches []PatternMatch

	for entityKey, pts := range restartByEntity {
		if len(pts) < 2 {
			continue
		}

		namespace := namespaceFromKey(entityKey)
		entity := entityNameFromKey(entityKey)
		trend := estimateTrend(pts)
		latestRate := pts[len(pts)-1].Value

		confidence := 0.4
		if latestRate > 0.1 {
			confidence += 0.2
		}
		if trend > 0.2 {
			confidence += 0.15
		}
		if len(pts) >= 3 {
			acceleration := pts[len(pts)-1].Value - pts[len(pts)-2].Value
			if acceleration > 0.05 {
				confidence += 0.15
			}
		}
		highRateCount := 0
		for _, p := range pts {
			if p.Value > 0.1 {
				highRateCount++
			}
		}
		if float64(highRateCount) >= float64(len(pts))*0.6 {
			confidence += 0.15
		}
		if len(pts) >= 5 {
			recentSpike := pts[len(pts)-1].Value - pts[len(pts)-5].Value
			if recentSpike > 0.5 {
				confidence += 0.2
			}
		}
		if hasEvent(events, entity, "CrashLoopBackOff") {
			confidence += 0.1
		}
		if contains(resources.FailingPods, entity) || contains(resources.FailingPods, entityKey) {
			confidence += 0.1
		}
		if confidence > 0.95 {
			confidence = 0.95
		}

		if confidence >= 0.5 {
			matches = append(matches, PatternMatch{
				Pattern:         "CrashLoopRisk",
				Confidence:      confidence,
				SuggestedAction: "Check container logs and fix startup errors",
				Entity:          entity,
				Namespace:       namespace,
				TimeToFailure:   estimateCrashLoopTimeToFailure(pts, latestRate),
			})
		}
	}
	return matches
}
