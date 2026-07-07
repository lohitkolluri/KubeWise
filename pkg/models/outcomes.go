package models

import "time"

// NotificationsConfig controls outbound webhook/Slack alerts.
type NotificationsConfig struct {
	Enabled         bool    `json:"enabled" yaml:"enabled"`
	WebhookURL      string  `json:"webhook_url,omitempty" yaml:"webhook_url,omitempty"`
	SlackWebhookURL string  `json:"slack_webhook_url,omitempty" yaml:"slack_webhook_url,omitempty"`
	MinScore        float64 `json:"min_score,omitempty" yaml:"min_score,omitempty"`
	OnPrediction    bool    `json:"on_prediction" yaml:"on_prediction"`
	OnRemediation   bool    `json:"on_remediation" yaml:"on_remediation"`
	OnApproval      bool    `json:"on_approval" yaml:"on_approval"`
}

// TrackedPrediction records an open prediction awaiting outcome verification.
type TrackedPrediction struct {
	ID         string     `json:"id"`
	Entity     string     `json:"entity"`
	Namespace  string     `json:"namespace"`
	Pattern    string     `json:"pattern"`
	MetricName string     `json:"metric_name"`
	Confidence float64    `json:"confidence"`
	ETASeconds float64    `json:"eta_seconds"`
	CreatedAt  time.Time  `json:"created_at"`
	ExpiresAt  time.Time  `json:"expires_at"`
	Outcome    string     `json:"outcome"` // pending, hit, miss, expired
	ResolvedAt *time.Time `json:"resolved_at,omitempty"`
}

// Outcome constants.
const (
	PredictionOutcomePending = "pending"
	PredictionOutcomeHit     = "hit"
	PredictionOutcomeMiss    = "miss"
	PredictionOutcomeExpired = "expired"
)

// AgentStats summarizes prediction accuracy and remediation outcomes (ROI metrics).
type AgentStats struct {
	PredictionsTotal     int     `json:"predictions_total"`
	PredictionsPending   int     `json:"predictions_pending"`
	PredictionsHit       int     `json:"predictions_hit"`
	PredictionsMissed    int     `json:"predictions_missed"`
	PredictionsExpired   int     `json:"predictions_expired"`
	PredictionAccuracy   float64 `json:"prediction_accuracy"`
	RemediationsTotal    int     `json:"remediations_total"`
	RemediationsVerified int     `json:"remediations_verified"`
	RemediationsFailed   int     `json:"remediations_failed"`
	RemediationsDryRun   int     `json:"remediations_dry_run"`
	RemediationsPending  int     `json:"remediations_pending_approval"`
}
