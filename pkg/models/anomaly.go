package models

import "time"

// Anomaly status constants.
const (
	AnomalyStatusDetected   = "detected"
	AnomalyStatusActive     = "active"
	AnomalyStatusCorrelated = "correlated"
	AnomalyStatusRejected   = "rejected"
	AnomalyStatusRemediated = "remediated"
	AnomalyStatusResolved   = "resolved"
)

// AnomalyRecord represents a detected anomaly in the cluster.
type AnomalyRecord struct {
	ID           string     `json:"id"`
	Entity       string     `json:"entity"`
	Namespace    string     `json:"namespace"`
	MetricName   string     `json:"metric_name"`
	Score        float64    `json:"score"`
	Pattern      string     `json:"pattern"`
	DetectedAt   *time.Time `json:"detected_at,omitempty"`
	RemediatedAt *time.Time `json:"remediated_at,omitempty"`
	Status       string     `json:"status"`
}
