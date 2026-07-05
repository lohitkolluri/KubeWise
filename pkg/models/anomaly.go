package models

import "time"

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
