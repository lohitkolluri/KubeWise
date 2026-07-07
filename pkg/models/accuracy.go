package models

import "time"

// AccuracySnapshot is a point-in-time breakdown of prediction accuracy
// aggregated across multiple dimensions.
type AccuracySnapshot struct {
	GeneratedAt    time.Time                  `json:"generated_at"`
	Overall        AccuracyMetrics            `json:"overall"`
	ByResourceKind map[string]AccuracyMetrics `json:"by_resource_kind"`
	ByPredictor    map[string]AccuracyMetrics `json:"by_predictor"`
	ByNamespace    map[string]AccuracyMetrics `json:"by_namespace"`
	ByMetric       map[string]AccuracyMetrics `json:"by_metric"`
	RollingWindow  string                     `json:"rolling_window"` // e.g. "24h", "7d", "all"
}

// AccuracyMetrics holds precision / recall / F1 for one grouping.
type AccuracyMetrics struct {
	TotalPredictions int     `json:"total_predictions"`
	Hits             int     `json:"hits"`
	Misses           int     `json:"misses"`
	Pending          int     `json:"pending"`
	Expired          int     `json:"expired"`
	Precision        float64 `json:"precision"` // hits / (hits + misses)
	Recall           float64 `json:"recall"`    // hits / total anomalies in window
	F1Score          float64 `json:"f1_score"`
	AvgConfidence    float64 `json:"avg_confidence"` // mean anomaly score for hits
}

// AccuracyHistoryPoint is one point in a time-series of accuracy data.
type AccuracyHistoryPoint struct {
	Timestamp time.Time       `json:"timestamp"`
	Window    string          `json:"window"` // "1h", "24h", "7d"
	Metrics   AccuracyMetrics `json:"metrics"`
}
