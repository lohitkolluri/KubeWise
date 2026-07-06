package predictor

import (
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const oomMinConfidence = 0.5
const memoryMetric = "pod_memory_usage"

type OOMPattern struct{}

func (o *OOMPattern) Name() string { return "OOMRisk" }

func (o *OOMPattern) Match(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []PatternMatch {
	memResult, ok := findMetric(metrics, memoryMetric)
	if !ok || len(memResult.Values) == 0 {
		return nil
	}

	memByEntity := groupByEntity(memResult.Values)
	var matches []PatternMatch

	for entityKey, rawPts := range memByEntity {
		pts := aggregatePodMemorySeries(rawPts)
		if len(pts) < 2 {
			continue
		}

		namespace := namespaceFromKey(entityKey)
		entity := entityNameFromKey(entityKey)
		memLimit := lookupMemLimit(resources, namespace, entity)
		latestMem := pts[len(pts)-1].Value
		usageRatio := latestMem / memLimit

		trend := estimateTrend(pts)
		confidence := 0.3 + usageRatio*0.3
		if trend > 0.05 {
			confidence += trend * 0.35
		}
		if usageRatio > 0.85 {
			confidence += 0.2
		} else if usageRatio > 0.7 {
			confidence += 0.1
		}
		if len(pts) >= 3 {
			acceleration := pts[len(pts)-1].Value/memLimit - pts[len(pts)-2].Value/memLimit
			if acceleration > 0.02 {
				confidence += 0.15
			}
		}
		if hasEvent(events, namespace, entity, "OOMKilled") || hasEvent(events, namespace, entity, "OOMKilling") {
			confidence += 0.1
		}
		if confidence > 0.95 {
			confidence = 0.95
		}

		if confidence >= oomMinConfidence && usageRatio >= 0.6 {
			matches = append(matches, PatternMatch{
				Pattern:         "OOMRisk",
				Confidence:      confidence,
				SuggestedAction: "Increase memory limits or reduce memory usage",
				Entity:          entity,
				Namespace:       namespace,
				TimeToFailure:   estimateOOMTimeToFailure(ratioSeries(pts, memLimit), usageRatio),
			})
		}
	}
	return matches
}

func ratioSeries(pts []MetricPoint, limit float64) []MetricPoint {
	if limit <= 0 {
		limit = defaultMemLimitBytes
	}
	out := make([]MetricPoint, len(pts))
	for i, p := range pts {
		out[i] = MetricPoint{
			Value:     p.Value / limit,
			Timestamp: p.Timestamp,
			Labels:    p.Labels,
		}
	}
	return out
}
