package remediator

import "github.com/lohitkolluri/KubeWise/internal/agent/store"

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
