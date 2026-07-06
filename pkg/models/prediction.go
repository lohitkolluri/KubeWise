package models

import "time"

// Remediation modes.
const (
	RemediationModeDryRun = "dry-run"
	RemediationModeAuto   = "auto"
	RemediationModeOff    = "off"
	RemediationModeSemi   = "semi" // dry-run with audit emphasis
)

// ValidRemediationMode reports whether mode is a known value.
func ValidRemediationMode(mode string) bool {
	switch mode {
	case RemediationModeDryRun, RemediationModeAuto, RemediationModeOff, RemediationModeSemi, "":
		return true
	default:
		return false
	}
}

// PredictionResult represents a failure prediction made by the engine.
type PredictionResult struct {
	Type       string    `json:"type"`
	Entity     string    `json:"entity"`
	Namespace  string    `json:"namespace"`
	MetricName string    `json:"metric_name"`
	Action     string    `json:"action"`
	Confidence float64   `json:"confidence"`
	ETASeconds float64   `json:"eta_seconds"`
	Timestamp  time.Time `json:"timestamp"`
	Score      float64   `json:"score"`
}

// ETA returns the prediction time-to-failure as a duration.
func (p PredictionResult) ETA() time.Duration {
	if p.ETASeconds <= 0 {
		return 0
	}
	return time.Duration(p.ETASeconds * float64(time.Second))
}
