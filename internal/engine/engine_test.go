package engine

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// mockRule matches anomalies with a specific pattern.
type mockRule struct {
	name       string
	matchOn    string // only match anomalies with this Pattern
	action     string
	confidence float64
	needsLLM   bool
}

func (r *mockRule) Name() string { return r.name }
func (r *mockRule) Evaluate(_ context.Context, input EngineInput) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.Pattern == r.matchOn {
			return []RuleResult{{
				RuleName:   r.name,
				Action:     r.action,
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityHigh,
				Confidence: r.confidence,
				NeedsLLM:   r.needsLLM,
				Evidence:   []string{"mock matched " + a.Pattern},
			}}, nil
		}
	}
	return nil, nil
}

// errRule always returns an error to test error resilience.
type errRule struct{ name string }

func (r *errRule) Name() string { return r.name }
func (r *errRule) Evaluate(_ context.Context, _ EngineInput) ([]RuleResult, error) {
	return nil, errors.New("always fails")
}

func mkAnomaly(entity, namespace, pattern string, score float64) models.AnomalyRecord {
	return models.AnomalyRecord{
		Entity:    entity,
		Namespace: namespace,
		Pattern:   pattern,
		Score:     score,
		Status:    models.AnomalyStatusDetected,
	}
}

func TestEvaluate_SingleMatch(t *testing.T) {
	re := New()
	re.RegisterRule(&mockRule{name: "oom_test", matchOn: "OOMKilled", action: "restart_pod", confidence: 0.98})

	input := EngineInput{
		Anomalies: []models.AnomalyRecord{
			mkAnomaly("pod-1", "default", "OOMKilled", 0.9),
		},
	}

	results, err := re.Evaluate(context.Background(), input)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	if len(results) != 1 {
		t.Fatalf("expected 1 result, got %d", len(results))
	}
	if results[0].RuleName != "oom_test" {
		t.Errorf("expected rule oom_test, got %s", results[0].RuleName)
	}
	if results[0].Confidence != 0.98 {
		t.Errorf("expected confidence 0.98, got %f", results[0].Confidence)
	}
	if results[0].Action != "restart_pod" {
		t.Errorf("expected action restart_pod, got %s", results[0].Action)
	}
}

func TestEvaluate_NoMatch(t *testing.T) {
	re := New()
	re.RegisterRule(&mockRule{name: "oom_test", matchOn: "OOMKilled", action: "restart_pod", confidence: 0.98})

	input := EngineInput{
		Anomalies: []models.AnomalyRecord{
			mkAnomaly("pod-2", "default", "CrashLoopBackOff", 0.8),
		},
	}

	results, err := re.Evaluate(context.Background(), input)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	if len(results) != 0 {
		t.Errorf("expected 0 results for no-match, got %d", len(results))
	}
}

func TestEvaluate_MultipleRules(t *testing.T) {
	re := New()
	re.RegisterRule(&mockRule{name: "oom", matchOn: "OOMKilled", action: "restart_pod", confidence: 0.98})
	re.RegisterRule(&mockRule{name: "crash", matchOn: "CrashLoopBackOff", action: "restart_pod", confidence: 0.95})

	input := EngineInput{
		Anomalies: []models.AnomalyRecord{
			mkAnomaly("pod-a", "default", "OOMKilled", 0.9),
			mkAnomaly("pod-b", "default", "CrashLoopBackOff", 0.8),
		},
	}

	results, err := re.Evaluate(context.Background(), input)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	if len(results) != 2 {
		t.Fatalf("expected 2 results, got %d", len(results))
	}
	// Results should be sorted by confidence descending.
	if results[0].Confidence < results[1].Confidence {
		t.Errorf("results not sorted by confidence descending: %f < %f",
			results[0].Confidence, results[1].Confidence)
	}
}

func TestEvaluate_PartialMatch(t *testing.T) {
	re := New()
	re.RegisterRule(&mockRule{name: "oom", matchOn: "OOMKilled", action: "restart_pod", confidence: 0.98})
	re.RegisterRule(&mockRule{name: "crash", matchOn: "CrashLoopBackOff", action: "restart_pod", confidence: 0.95})

	input := EngineInput{
		Anomalies: []models.AnomalyRecord{
			mkAnomaly("pod-a", "default", "OOMKilled", 0.9),
		},
	}

	results, err := re.Evaluate(context.Background(), input)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	if len(results) != 1 {
		t.Fatalf("expected 1 result (only OOM matches), got %d", len(results))
	}
	if results[0].RuleName != "oom" {
		t.Errorf("expected oom rule, got %s", results[0].RuleName)
	}
}

func TestEvaluate_ErrorRuleDoesNotBlock(t *testing.T) {
	re := New()
	re.RegisterRule(&errRule{name: "broken"})
	re.RegisterRule(&mockRule{name: "oom", matchOn: "OOMKilled", action: "restart_pod", confidence: 0.98})

	input := EngineInput{
		Anomalies: []models.AnomalyRecord{
			mkAnomaly("pod-a", "default", "OOMKilled", 0.9),
		},
	}

	results, err := re.Evaluate(context.Background(), input)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	if len(results) != 1 {
		t.Fatalf("expected 1 result (broken rule skipped), got %d", len(results))
	}
	if results[0].RuleName != "oom" {
		t.Errorf("expected oom rule, got %s", results[0].RuleName)
	}
}

func TestEvaluate_ContextCancellation(t *testing.T) {
	re := New()
	re.RegisterRule(&mockRule{name: "oom", matchOn: "OOMKilled", action: "restart_pod", confidence: 0.98})

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // Cancel immediately.

	_, err := re.Evaluate(ctx, EngineInput{})
	if err == nil {
		t.Fatal("expected error from cancelled context, got nil")
	}
	if !errors.Is(err, context.Canceled) {
		t.Errorf("expected context.Canceled, got %v", err)
	}
}

func TestEvaluate_ContextDeadline(t *testing.T) {
	re := New()
	re.RegisterRule(&mockRule{name: "oom", matchOn: "OOMKilled", action: "restart_pod", confidence: 0.98})

	ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Hour))
	defer cancel()

	_, err := re.Evaluate(ctx, EngineInput{})
	if err == nil {
		t.Fatal("expected error from expired deadline, got nil")
	}
}

func TestEvaluate_EmptyEngine(t *testing.T) {
	re := New()

	results, err := re.Evaluate(context.Background(), EngineInput{
		Anomalies: []models.AnomalyRecord{
			mkAnomaly("pod", "default", "OOMKilled", 0.9),
		},
	})
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	if len(results) != 0 {
		t.Errorf("expected 0 results from empty engine, got %d", len(results))
	}
}

func TestEvaluate_EmptyAnomalies(t *testing.T) {
	re := New()
	re.RegisterRule(&mockRule{name: "oom", matchOn: "OOMKilled", action: "restart_pod", confidence: 0.98})

	results, err := re.Evaluate(context.Background(), EngineInput{})
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	if len(results) != 0 {
		t.Errorf("expected 0 results with empty anomalies, got %d", len(results))
	}
}

func TestRuleCount(t *testing.T) {
	re := New()
	if re.RuleCount() != 0 {
		t.Errorf("expected 0, got %d", re.RuleCount())
	}
	re.RegisterRule(&mockRule{name: "r1", matchOn: "A", action: "noop", confidence: 0.5})
	re.RegisterRule(&mockRule{name: "r2", matchOn: "B", action: "noop", confidence: 0.5})
	if re.RuleCount() != 2 {
		t.Errorf("expected 2, got %d", re.RuleCount())
	}
}

func TestMustRegister_PanicsOnEmptyName(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Error("expected panic from empty rule name")
		}
	}()
	re := New()
	MustRegister(re, &mockRule{name: "", matchOn: "X", action: "noop", confidence: 0.5})
}

func TestValidateResult_Valid(t *testing.T) {
	rr := RuleResult{
		RuleName:   "test",
		Action:     "restart_pod",
		Target:     "pod-1",
		Namespace:  "default",
		Confidence: 0.95,
		Evidence:   []string{"test evidence"},
	}
	errs := ValidateResult(rr)
	if len(errs) != 0 {
		t.Fatalf("expected no errors, got %v", errs)
	}
}

func TestValidateResult_Invalid(t *testing.T) {
	cases := []struct {
		name string
		rr   RuleResult
		// at least one error expected
	}{
		{"empty name", RuleResult{Action: "noop", Confidence: 0.5, Evidence: []string{"e"}}},
		{"empty action", RuleResult{RuleName: "r", Confidence: 0.5, Evidence: []string{"e"}}},
		{"confidence too low", RuleResult{RuleName: "r", Action: "noop", Confidence: -0.1, Evidence: []string{"e"}}},
		{"confidence too high", RuleResult{RuleName: "r", Action: "noop", Confidence: 1.5, Evidence: []string{"e"}}},
		{"empty target for actionable", RuleResult{RuleName: "r", Action: "restart_pod", Confidence: 0.9, Evidence: []string{"e"}}},
		{"empty evidence", RuleResult{RuleName: "r", Action: "noop", Confidence: 0.5}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			errs := ValidateResult(tc.rr)
			if len(errs) == 0 {
				t.Error("expected at least one validation error, got none")
			}
		})
	}
}

func TestRuleToPlan_MapsFields(t *testing.T) {
	rr := RuleResult{
		RuleName:   "oom_killed",
		Action:     "restart_pod",
		Target:     "pod-1",
		Namespace:  "default",
		Severity:   SeverityCritical,
		Confidence: 0.98,
		Evidence:   []string{"Pod default/pod-1 OOMKilled"},
		NeedsLLM:   false,
	}
	plan := RuleToPlan(rr)

	if plan.Diagnosis.RootCause != "oom_killed" {
		t.Errorf("RootCause = %q, want %q", plan.Diagnosis.RootCause, "oom_killed")
	}
	if plan.Diagnosis.Confidence != 0.98 {
		t.Errorf("Confidence = %f, want 0.98", plan.Diagnosis.Confidence)
	}
	if plan.Action.Type != "restart_pod" {
		t.Errorf("Action.Type = %q, want %q", plan.Action.Type, "restart_pod")
	}
	if plan.Action.Target != "pod-1" {
		t.Errorf("Action.Target = %q, want %q", plan.Action.Target, "pod-1")
	}
	if plan.Action.Namespace != "default" {
		t.Errorf("Action.Namespace = %q, want %q", plan.Action.Namespace, "default")
	}
	if plan.Risk.BlastRadius != "single_pod" {
		t.Errorf("BlastRadius = %q, want %q", plan.Risk.BlastRadius, "single_pod")
	}
	if !plan.Risk.Reversible {
		t.Error("restart_pod should be reversible")
	}
}

func TestRuleToPlan_EscalateNotReversible(t *testing.T) {
	rr := RuleResult{
		RuleName:   "node_not_ready",
		Action:     "escalate",
		Target:     "node-1",
		Namespace:  "default",
		Severity:   SeverityCritical,
		Confidence: 0.90,
		Evidence:   []string{"Node node-1 not ready"},
	}
	plan := RuleToPlan(rr)

	if plan.Risk.Reversible {
		t.Error("escalate should not be marked reversible")
	}
	if plan.Risk.BlastRadius != "single_pod" {
		t.Errorf("BlastRadius = %q, want %q", plan.Risk.BlastRadius, "single_pod")
	}
}

func TestSortResults(t *testing.T) {
	results := []RuleResult{
		{RuleName: "a", Confidence: 0.5},
		{RuleName: "b", Confidence: 0.9},
		{RuleName: "c", Confidence: 0.7},
	}
	sortResults(results)

	if results[0].RuleName != "b" || results[1].RuleName != "c" || results[2].RuleName != "a" {
		t.Errorf("unexpected sort order: %v", results)
	}
}
