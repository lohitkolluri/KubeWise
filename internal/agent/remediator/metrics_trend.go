package remediator

import (
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// metricData holds extracted metric series data for an anomaly.
type metricData struct {
	Name string
	Pts  []store.MetricPoint
}

// extractMetricSeries deduplicates anomalies by (entity, metricName) and fetches
// their metric series. This is shared between the rule engine and LLM code paths.
func (c *Correlator) extractMetricSeries(anomalies []models.AnomalyRecord) []metricData {
	seen := make(map[string]struct{})
	var result []metricData
	for _, a := range anomalies {
		if a.MetricName == "" {
			continue
		}
		key := a.Entity + "|" + a.MetricName
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		ns, name := models.ParseEntity(a.Entity)
		if a.Namespace != "" && ns == "" {
			ns = a.Namespace
		}
		labels := map[string]string{"namespace": ns, "pod": name}
		pts, err := c.store.GetMetricSeries(a.MetricName, labels, 15)
		if err != nil || len(pts) == 0 {
			result = append(result, metricData{Name: a.MetricName + "@" + a.Entity})
			continue
		}
		result = append(result, metricData{Name: a.MetricName + "@" + a.Entity, Pts: pts})
	}
	return result
}

func metricTrendDirection(pts []store.MetricPoint) string {
	if len(pts) < 2 {
		return "stable"
	}
	last := pts[len(pts)-1].Value
	baseline := pts[0].Value
	if len(pts) >= 3 {
		baseline = medianValue(pts[:3])
	}
	if last > baseline*1.1 {
		return "rising"
	}
	if last < baseline*0.9 {
		return "falling"
	}
	return "stable"
}

func medianValue(pts []store.MetricPoint) float64 {
	if len(pts) == 0 {
		return 0
	}
	values := make([]float64, len(pts))
	for i, p := range pts {
		values[i] = p.Value
	}
	for i := 1; i < len(values); i++ {
		for j := i; j > 0 && values[j] < values[j-1]; j-- {
			values[j], values[j-1] = values[j-1], values[j]
		}
	}
	mid := len(values) / 2
	if len(values)%2 == 0 {
		return (values[mid-1] + values[mid]) / 2
	}
	return values[mid]
}
