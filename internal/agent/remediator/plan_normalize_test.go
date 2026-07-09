package remediator

import (
	"strings"
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func kwTestAnomalies() []models.AnomalyRecord {
	return []models.AnomalyRecord{
		{ID: "1", Entity: "kw-test/crashloop-demo", Namespace: "kw-test", Pattern: "CrashLoopRisk", Status: models.AnomalyStatusDetected},
		{ID: "2", Entity: "kw-test/oom-demo", Namespace: "kw-test", Pattern: "Degradation", Status: models.AnomalyStatusDetected},
	}
}

func TestNormalizePlan_SplitNamespaceTarget(t *testing.T) {
	plan := models.RemediationPlan{
		Action: models.Action{Type: "restart_pod", Namespace: "", Target: "kw-test/crashloop-demo", Rationale: "fix"},
		Risk:   models.Risk{},
	}
	normalizePlan(&plan, kwTestAnomalies())
	if plan.Action.Namespace != "kw-test" || plan.Action.Target != "crashloop-demo" {
		t.Fatalf("got ns=%q target=%q", plan.Action.Namespace, plan.Action.Target)
	}
}

func TestNormalizePlan_GenericPodTarget(t *testing.T) {
	plan := models.RemediationPlan{
		Action: models.Action{Type: "restart_pod", Namespace: "kw-test", Target: "kw-test/pod", Rationale: "fix"},
		Risk:   models.Risk{},
	}
	normalizePlan(&plan, kwTestAnomalies())
	if plan.Action.Target != "crashloop-demo" {
		t.Fatalf("expected inferred crashloop-demo, got %q", plan.Action.Target)
	}
}

func TestNormalizePlan_ActionTypeAlias(t *testing.T) {
	plan := models.RemediationPlan{
		Action:    models.Action{Type: "restart", Namespace: "kw-test", Target: "crashloop-demo", Rationale: "fix"},
		Diagnosis: models.Diagnosis{Confidence: 0.85},
		Risk:      models.Risk{BlastRadius: "single pod", EstimatedTimeToResolve: "1m"},
	}
	normalizePlan(&plan, kwTestAnomalies())
	if plan.Action.Type != "restart_pod" {
		t.Fatalf("expected restart_pod, got %q", plan.Action.Type)
	}
	if plan.Risk.BlastRadius != "single_pod" {
		t.Fatalf("expected single_pod blast radius, got %q", plan.Risk.BlastRadius)
	}
}

func TestNormalizePlan_SwappedTypeTarget(t *testing.T) {
	plan := models.RemediationPlan{
		Action: models.Action{Type: "crashloop-demo", Namespace: "kw-test", Target: "restart_pod", Rationale: "fix"},
		Risk:   models.Risk{},
	}
	normalizePlan(&plan, kwTestAnomalies())
	if plan.Action.Type != "restart_pod" {
		t.Fatalf("expected restart_pod, got %q", plan.Action.Type)
	}
}

func TestNormalizePlan_ValidatesAfterNormalize(t *testing.T) {
	c := &Correlator{cfg: RemediationConfig{MinConfidence: 0.7}}
	plan := models.RemediationPlan{
		Action:    models.Action{Type: "restart", Namespace: "", Target: "kw-test/pod", Rationale: "restart failing pod"},
		Diagnosis: models.Diagnosis{Confidence: 0.85, RootCause: "crash loop", Severity: "critical", Evidence: []string{"BackOff"}},
		Risk:      models.Risk{BlastRadius: "pod", Reversible: true, EstimatedTimeToResolve: "30s"},
	}
	anomalies := kwTestAnomalies()
	normalizePlan(&plan, anomalies)
	if err := c.validatePlan(plan, anomalies); err != nil {
		t.Fatalf("expected validation pass after normalize: %v", err)
	}
}

func TestEscalateForIncompletePatch(t *testing.T) {
	plan := models.RemediationPlan{
		Action: models.Action{
			Type:      "patch_resources",
			Namespace: "monitoring",
			Target:    "prometheus",
			Rationale: "increase memory",
		},
		Steps: []models.RunbookStep{{
			Order:     1,
			Type:      "patch_resources",
			Namespace: "monitoring",
			Target:    "prometheus",
			Rationale: "increase memory",
		}},
		Diagnosis: models.Diagnosis{Confidence: 0.79, RootCause: "OOM risk"},
	}
	if !isIncompletePatchPlan(plan) {
		t.Fatal("expected incomplete patch plan")
	}
	escalateForIncompletePatch(&plan)
	if plan.Action.Type != "escalate" {
		t.Fatalf("expected escalate, got %q", plan.Action.Type)
	}
	if !strings.Contains(plan.Action.Rationale, incompletePatchEscalatePrefix) {
		t.Fatalf("expected operator guidance, got %q", plan.Action.Rationale)
	}
	if plan.Steps[0].Type != "escalate" {
		t.Fatalf("expected escalated runbook step, got %q", plan.Steps[0].Type)
	}
}

func TestIsIncompletePatchPlan_LegacyNoopDemotion(t *testing.T) {
	plan := models.RemediationPlan{
		Action: models.Action{
			Type:      "noop",
			Namespace: "monitoring",
			Target:    "prometheus",
			Rationale: "demoted from patch_resources: missing resource parameters",
		},
	}
	if !isIncompletePatchPlan(plan) {
		t.Fatal("expected legacy noop demotion to be treated as incomplete patch")
	}
}
