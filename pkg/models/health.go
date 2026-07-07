package models

import "time"

// HealthScore is a 0–100 composite score for a monitored resource.
type HealthScore struct {
	Entity        string        `json:"entity"` // resource identifier (pod/namespace name)
	Namespace     string        `json:"namespace"`
	ResourceKind  string        `json:"resource_kind"`  // "pod", "node", "pvc", "namespace"
	Score         float64       `json:"score"`          // 0 (critical) – 100 (healthy)
	PreviousScore float64       `json:"previous_score"` // prior score for delta/trend
	Trend         string        `json:"trend"`          // "improving", "stable", "degrading", "critical"
	Factors       []ScoreFactor `json:"factors"`        // explainability breakdown
	GeneratedAt   time.Time     `json:"generated_at"`
}

// ScoreFactor explains one component of a health score.
type ScoreFactor struct {
	Name   string  `json:"name"`   // e.g. "anomaly_recency", "forecast_trend", "remediation_rate"
	Weight float64 `json:"weight"` // contribution weight (0–1, sums to 1 across all factors)
	Score  float64 `json:"score"`  // sub-score 0–1
	Detail string  `json:"detail"` // human-readable explanation
}

// ClusterHealthSummary aggregates health across the cluster.
type ClusterHealthSummary struct {
	OverallScore  float64       `json:"overall_score"`
	ResourceCount int           `json:"resource_count"`
	HealthyCount  int           `json:"healthy_count"`  // score >= 80
	WarningCount  int           `json:"warning_count"`  // score >= 50, < 80
	CriticalCount int           `json:"critical_count"` // score < 50
	TopRisks      []HealthScore `json:"top_risks"`      // worst 5 entities
	GeneratedAt   time.Time     `json:"generated_at"`
}
