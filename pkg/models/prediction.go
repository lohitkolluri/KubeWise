package models

import "time"

// PredictionResult represents a failure prediction made by the engine.
type PredictionResult struct {
	Type       string        `json:"type"`
	Entity     string        `json:"entity"`
	Namespace  string        `json:"namespace"`
	MetricName string        `json:"metric_name"`
	Action     string        `json:"action"`
	Confidence float64       `json:"confidence"`
	ETA        time.Duration `json:"eta"`
	Timestamp  time.Time     `json:"timestamp"`
	Score      float64       `json:"score"`
}
