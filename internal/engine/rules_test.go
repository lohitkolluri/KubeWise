package engine

import (
	"context"
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// ruleTestCase describes one rule evaluation expectation.
type ruleTestCase struct {
	name   string
	rule   Rule
	input  EngineInput
	match  bool         // true = expect exactly one result
	check  func(*testing.T, RuleResult) // optional extra assertions
}

func runRuleTests(t *testing.T, cases []ruleTestCase) {
	t.Helper()
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			results, err := tc.rule.Evaluate(context.Background(), tc.input)
			if err != nil {
				t.Fatalf("Evaluate returned error: %v", err)
			}
			if tc.match {
				if len(results) == 0 {
					t.Fatal("expected a match, got none")
				}
				if len(results) > 1 {
					t.Fatalf("expected exactly 1 result, got %d", len(results))
				}
				if tc.check != nil {
					tc.check(t, results[0])
				}
			} else {
				if len(results) > 0 {
					t.Fatalf("expected no match, got %d results", len(results))
				}
			}
		})
	}
}

func TestOOMRule(t *testing.T) {
	runRuleTests(t, []ruleTestCase{
		{
			name: "matches OOMKilled",
			rule: &OOMRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "pod-a", Namespace: "default", Pattern: "OOMKilled", Score: 0.95},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Action != "restart_pod" {
					t.Errorf("action = %q, want restart_pod", rr.Action)
				}
				if rr.Confidence != 0.98 {
					t.Errorf("confidence = %f, want 0.98", rr.Confidence)
				}
				if rr.NeedsLLM {
					t.Error("NeedsLLM should be false")
				}
			},
		},
		{
			name: "matches OOMKilling",
			rule: &OOMRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "pod-b", Namespace: "prod", Pattern: "OOMKilling", Score: 0.90},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Target != "pod-b" {
					t.Errorf("target = %q, want pod-b", rr.Target)
				}
				if rr.Namespace != "prod" {
					t.Errorf("namespace = %q, want prod", rr.Namespace)
				}
			},
		},
		{
			name: "no match for unrelated pattern",
			rule: &OOMRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Pattern: "CrashLoopBackOff", Score: 0.8},
			}},
			match: false,
		},
		{
			name:  "no match for empty input",
			rule:  &OOMRule{},
			input: EngineInput{},
			match: false,
		},
	})
}

func TestCrashLoopRule(t *testing.T) {
	runRuleTests(t, []ruleTestCase{
		{
			name: "matches CrashLoopBackOff pattern",
			rule: &CrashLoopRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "pod-a", Namespace: "default", Pattern: "CrashLoopBackOff", Score: 0.9},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Action != "restart_pod" {
					t.Errorf("action = %q", rr.Action)
				}
				if rr.Confidence != 0.95 {
					t.Errorf("confidence = %f", rr.Confidence)
				}
			},
		},
		{
			name: "matches restart_rate metric",
			rule: &CrashLoopRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "pod-b", MetricName: "restart_rate", Score: 0.85},
			}},
			match: true,
		},
		{
			name: "no match for low restart_rate",
			rule: &CrashLoopRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{MetricName: "restart_rate", Score: 0.5},
			}},
			match: false,
		},
		{
			name: "no match for unrelated pattern",
			rule: &CrashLoopRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Pattern: "OOMKilled", Score: 0.9},
			}},
			match: false,
		},
	})
}

func TestImagePullBackOffRule(t *testing.T) {
	runRuleTests(t, []ruleTestCase{
		{
			name: "matches ImagePullBackOff",
			rule: &ImagePullBackOffRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "web", Namespace: "prod", Pattern: "ImagePullBackOff", Score: 0.95},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Action != "escalate" {
					t.Errorf("action = %q, want escalate", rr.Action)
				}
				if rr.Confidence != 0.97 {
					t.Errorf("confidence = %f, want 0.97", rr.Confidence)
				}
			},
		},
		{
			name: "matches ErrImagePull",
			rule: &ImagePullBackOffRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "api", Namespace: "default", Pattern: "ErrImagePull", Score: 0.9},
			}},
			match: true,
		},
		{
			name: "matches ImagePull",
			rule: &ImagePullBackOffRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "worker", Namespace: "staging", Pattern: "ImagePull", Score: 0.85},
			}},
			match: true,
		},
		{
			name: "no match for unrelated pattern",
			rule: &ImagePullBackOffRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Pattern: "OOMKilled", Score: 0.9},
			}},
			match: false,
		},
	})
}

func TestNodeNotReadyRule(t *testing.T) {
	runRuleTests(t, []ruleTestCase{
		{
			name: "matches NodeNotReady",
			rule: &NodeNotReadyRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "node-1", Namespace: "", Pattern: "NodeNotReady", Score: 1.0},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Action != "escalate" {
					t.Errorf("action = %q, want escalate", rr.Action)
				}
				if rr.Confidence != 0.90 {
					t.Errorf("confidence = %f, want 0.90", rr.Confidence)
				}
				if rr.Severity != SeverityCritical {
					t.Errorf("severity = %v, want critical", rr.Severity)
				}
			},
		},
		{
			name: "no match for unrelated pattern",
			rule: &NodeNotReadyRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Pattern: "MemoryPressure", Score: 0.8},
			}},
			match: false,
		},
	})
}

func TestPendingRule(t *testing.T) {
	runRuleTests(t, []ruleTestCase{
		{
			name: "matches Unschedulable",
			rule: &PendingRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "pod-x", Namespace: "default", Pattern: "Unschedulable", Score: 0.9},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Action != "escalate" {
					t.Errorf("action = %q, want escalate", rr.Action)
				}
				if rr.Confidence != 0.85 {
					t.Errorf("confidence = %f, want 0.85", rr.Confidence)
				}
			},
		},
		{
			name: "matches Pending with high score",
			rule: &PendingRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "pod-y", Namespace: "prod", Pattern: "Pending", Score: 0.75},
			}},
			match: true,
		},
		{
			name: "no match for Pending with low score",
			rule: &PendingRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Pattern: "Pending", Score: 0.5},
			}},
			match: false,
		},
		{
			name: "no match for unrelated pattern",
			rule: &PendingRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Pattern: "OOMKilled", Score: 0.9},
			}},
			match: false,
		},
	})
}

func TestReadyRatioRule(t *testing.T) {
	runRuleTests(t, []ruleTestCase{
		{
			name: "matches LowReadyRatio",
			rule: &ReadyRatioRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "web-deploy", Namespace: "prod", Pattern: "LowReadyRatio", Score: 0.4},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Action != "scale_replicas" {
					t.Errorf("action = %q, want scale_replicas", rr.Action)
				}
				if rr.Confidence != 0.80 {
					t.Errorf("confidence = %f, want 0.80", rr.Confidence)
				}
				if rr.NeedsLLM {
					t.Error("NeedsLLM should be false")
				}
			},
		},
		{
			name: "no match for unrelated pattern",
			rule: &ReadyRatioRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Pattern: "OOMKilled", Score: 0.9},
			}},
			match: false,
		},
	})
}

func TestCPUThrottleRule(t *testing.T) {
	runRuleTests(t, []ruleTestCase{
		{
			name: "matches high CPU throttle",
			rule: &CPUThrottleRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "api-5f4d", Namespace: "default", MetricName: "cpu_throttle", Score: 0.65},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Action != "patch_resources" {
					t.Errorf("action = %q, want patch_resources", rr.Action)
				}
				if rr.Confidence != 0.75 {
					t.Errorf("confidence = %f, want 0.75", rr.Confidence)
				}
				if !rr.NeedsLLM {
					t.Error("NeedsLLM should be true")
				}
			},
		},
		{
			name: "no match for throttle below threshold",
			rule: &CPUThrottleRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "web", MetricName: "cpu_throttle", Score: 0.3},
			}},
			match: false,
		},
		{
			name: "no match for wrong metric name",
			rule: &CPUThrottleRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "web", MetricName: "cpu_usage", Score: 0.9},
			}},
			match: false,
		},
	})
}

func TestMemoryPressureRule(t *testing.T) {
	runRuleTests(t, []ruleTestCase{
		{
			name: "matches MemoryPressure pattern",
			rule: &MemoryPressureRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "node-1", Namespace: "", Pattern: "MemoryPressure", Score: 0.95},
			}},
			match: true,
			check: func(t *testing.T, rr RuleResult) {
				if rr.Action != "escalate" {
					t.Errorf("action = %q, want escalate", rr.Action)
				}
				if rr.Confidence != 0.85 {
					t.Errorf("confidence = %f, want 0.85", rr.Confidence)
				}
				if rr.NeedsLLM {
					t.Error("NeedsLLM should be false")
				}
			},
		},
		{
			name: "matches node_memory_pressure metric",
			rule: &MemoryPressureRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Entity: "node-2", MetricName: "node_memory_pressure", Score: 0.9},
			}},
			match: true,
		},
		{
			name: "no match for unrelated pattern",
			rule: &MemoryPressureRule{},
			input: EngineInput{Anomalies: []models.AnomalyRecord{
				{Pattern: "NodeNotReady", Score: 0.8},
			}},
			match: false,
		},
	})
}
