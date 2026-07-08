// Package engine provides a deterministic rule engine that evaluates registered rules
// against anomaly input and returns actionable results. Rules run before the LLM
// path in the correlator pipeline, providing fast-path remediation for known failure
// patterns without incurring LLM cost or latency.
package engine

import (
	"context"
	"fmt"
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Severity indicates how urgent the rule match is.
type Severity int

const (
	SeverityLow Severity = iota
	SeverityMedium
	SeverityHigh
	SeverityCritical
)

func (s Severity) String() string {
	switch s {
	case SeverityLow:
		return "low"
	case SeverityMedium:
		return "medium"
	case SeverityHigh:
		return "high"
	case SeverityCritical:
		return "critical"
	default:
		return fmt.Sprintf("severity(%d)", int(s))
	}
}

// RuleResult describes a single rule match.
type RuleResult struct {
	RuleName   string            `json:"rule_name"`
	Action     string            `json:"action"`               // restart_pod, scale_replicas, etc.
	Target     string            `json:"target"`               // pod name, deployment name
	Namespace  string            `json:"namespace"`
	Severity   Severity          `json:"severity"`
	Confidence float64           `json:"confidence"`            // 0.0 - 1.0
	Evidence   []string          `json:"evidence"`              // why this rule fired (human-readable)
	NeedsLLM   bool              `json:"needs_llm"`             // false = deterministic, skip LLM
	Parameters map[string]string `json:"parameters,omitempty"`  // action-specific params
}

// EngineInput is the input passed to each rule for evaluation.
// Keep it scoped to what rules actually need — not the full cluster state.
type EngineInput struct {
	Anomalies []models.AnomalyRecord
	Metrics   []MetricSummary
	Resources ResourceSnapshot
}

// MetricSummary is a computed summary of a metric time series.
type MetricSummary struct {
	Name        string  `json:"name"`
	Current     float64 `json:"current"`
	Average     float64 `json:"average"`
	Max         float64 `json:"max"`
	SampleCount int     `json:"sample_count"`
	Trend       string  `json:"trend"` // "rising", "falling", "stable"
}

// ResourceSnapshot captures the K8s resource state at evaluation time.
type ResourceSnapshot struct {
	PodStatus            string // "Running", "CrashLoopBackOff", etc.
	RestartCount         int
	NodeReady            bool
	DeploymentAvailable  int32
	DeploymentReplicas   int32
}

// Rule is the interface every deterministic rule must implement.
type Rule interface {
	// Name returns a unique rule identifier (used for metrics, dedup, audit).
	Name() string
	// Evaluate checks the input and returns matching results.
	// Must be safe for concurrent evaluation (rules evaluate in order serial).
	Evaluate(ctx context.Context, input EngineInput) ([]RuleResult, error)
}

// ValidateResult checks that a RuleResult has all required fields populated.
// Returns a list of missing-field errors, or nil if the result is valid.
func ValidateResult(r RuleResult) []error {
	var errs []error
	if r.RuleName == "" {
		errs = append(errs, fmt.Errorf("RuleName is empty"))
	}
	if r.Action == "" {
		errs = append(errs, fmt.Errorf("Action is empty"))
	}
	if r.Confidence < 0 || r.Confidence > 1 {
		errs = append(errs, fmt.Errorf("Confidence %.2f out of range [0,1]", r.Confidence))
	}
	if r.Target == "" && r.Action != "noop" && r.Action != "escalate" {
		errs = append(errs, fmt.Errorf("Target is empty for action %q", r.Action))
	}
	if len(r.Evidence) == 0 {
		errs = append(errs, fmt.Errorf("Evidence is empty"))
	}
	return errs
}

// RuleToPlan converts a RuleResult into a RemediationPlan for the existing pipeline.
// Rule-sourced plans still pass through tier assigner, dry-run, T3 approvals, and verifier.
func RuleToPlan(rr RuleResult) models.RemediationPlan {
	return models.RemediationPlan{
		Diagnosis: models.Diagnosis{
			RootCause:  rr.RuleName,
			Severity:   rr.Severity.String(),
			Confidence: rr.Confidence,
			Evidence:   rr.Evidence,
		},
		Action: models.Action{
			Type:       rr.Action,
			Target:     rr.Target,
			Namespace:  rr.Namespace,
			Parameters: rr.Parameters,
			Rationale:  strings.Join(rr.Evidence, "; "),
		},
		Risk: models.Risk{
			BlastRadius: blastRadiusForAction(rr.Action),
			Reversible:  rr.Action == "restart_pod" || rr.Action == "scale_replicas",
		},
	}
}

// blastRadiusForAction maps rule actions to a blast radius string for the tier assigner.
func blastRadiusForAction(action string) string {
	switch action {
	case "restart_pod", "delete_pod":
		return "single_pod"
	case "scale_replicas", "rollback_deployment":
		return "service"
	case "patch_resources":
		return "single_pod"
	default:
		return "single_pod"
	}
}
