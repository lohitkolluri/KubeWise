package promptctx

import (
	"math"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// MetricPoint is a time-ordered metric sample used for trend calculation.
type MetricPoint struct {
	Timestamp float64 // seconds since epoch
	Value     float64
}

// BuildMetricSummary computes metric summaries from the store for a given metric.
// Returns nil when the metric has no time series data.
func BuildMetricSummary(s DataSource, metricName, entity, namespace string, fallbackScore float64) []MetricSummary {
	labels := map[string]string{}
	if ns, name := parseEntity(entity); name != "" {
		labels["pod"] = name
		if namespace != "" {
			labels["namespace"] = namespace
		} else if ns != "" {
			labels["namespace"] = ns
		}
	}

	pts, err := s.GetMetricSamples(metricName, labels, 30)
	if err != nil || len(pts) == 0 {
		// No time series available — report the anomaly score as a single point.
		return []MetricSummary{{
			Name:        metricName + "@" + entity,
			Current:     fallbackScore,
			Trend:       "unknown",
			Max:         fallbackScore,
			SampleCount: 0,
		}}
	}

	samples := make([]MetricPoint, len(pts))
	var maxVal, total float64
	for i, p := range pts {
		v := p.Value
		samples[i] = MetricPoint{Timestamp: float64(p.TS), Value: v}
		total += v
		if v > maxVal || i == 0 {
			maxVal = v
		}
	}

	avg := total / float64(len(pts))
	trend := detectTrend(samples, avg)
	current := pts[len(pts)-1].Value

	return []MetricSummary{{
		Name:        metricName + "@" + entity,
		Current:     current,
		Trend:       trend,
		Max:         maxVal,
		SampleCount: len(pts),
	}}
}

// detectTrend uses the sign of a linear regression slope to classify trend.
// slope = (n*Σ(x_i*y_i) - Σx_i*Σy_i) / (n*Σ(x_i²) - (Σx_i)²)
// If slope > 0.01 * mean(y): "rising"
// If slope < -0.01 * mean(y): "falling"
// Otherwise: "stable"
func detectTrend(samples []MetricPoint, meanY float64) string {
	if len(samples) < 2 {
		return "stable"
	}

	var sumX, sumY, sumXY, sumX2 float64
	n := float64(len(samples))

	for _, s := range samples {
		sumX += s.Timestamp
		sumY += s.Value
		sumXY += s.Timestamp * s.Value
		sumX2 += s.Timestamp * s.Timestamp
	}

	denom := n*sumX2 - sumX*sumX
	if math.Abs(denom) < 1e-12 {
		return "stable"
	}

	slope := (n*sumXY - sumX*sumY) / denom
	threshold := 0.01 * math.Abs(meanY)
	if math.Abs(meanY) < 1e-12 {
		threshold = 1e-6
	}

	if slope > threshold {
		return "rising"
	}
	if slope < -threshold {
		return "falling"
	}
	return "stable"
}

// parseEntity splits a K8s entity string into namespace and name.
func parseEntity(entity string) (string, string) {
	return models.ParseEntity(entity)
}
