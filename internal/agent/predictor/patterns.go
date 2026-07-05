package predictor

import (
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

type PatternMatch struct {
	Pattern          string
	Confidence       float64
	TimeToFailure    time.Duration
	SuggestedAction  string
	Entity           string
	Namespace        string
}

type ResourceSnapshot struct {
	FailingPods   []string
	UnhealthyNodes []string
}

type PatternMatcher interface {
	Name() string
	Match(metrics []MetricResult, events []models.AnomalyRecord, resources ResourceSnapshot) []PatternMatch
}

func patternToResult(m PatternMatch) models.PredictionResult {
	return models.PredictionResult{
		Type:       "pattern",
		Entity:     m.Entity,
		Namespace:  m.Namespace,
		Confidence: m.Confidence,
		Score:      m.Confidence,
		ETA:        m.TimeToFailure,
		Timestamp:  time.Now(),
	}
}

func findMetric(metrics []MetricResult, name string) *MetricResult {
	for i := range metrics {
		if metrics[i].Name == name {
			return &metrics[i]
		}
	}
	return nil
}

func groupByEntity(pts []MetricPoint) map[string][]MetricPoint {
	result := make(map[string][]MetricPoint)
	for _, pt := range pts {
		entity := pt.Labels["pod"]
		if entity == "" {
			entity = pt.Labels["container"]
		}
		if entity == "" {
			entity = pt.Labels["instance"]
		}
		if entity == "" {
			entity = "unknown"
		}
		result[entity] = append(result[entity], pt)
	}
	return result
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
