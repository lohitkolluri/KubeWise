package remediator

import (
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestEffectiveSteps_FallbackToAction(t *testing.T) {
	plan := models.RemediationPlan{
		Action: models.Action{Type: "restart_pod", Namespace: "default", Target: "pod-1"},
	}
	steps := plan.EffectiveSteps()
	if len(steps) != 1 || steps[0].Type != "restart_pod" {
		t.Fatalf("unexpected steps: %+v", steps)
	}
}

func TestValidateRunbookSteps_MultiStep(t *testing.T) {
	cfg := RemediationConfig{MinConfidence: 0.7}
	plan := models.RemediationPlan{
		Diagnosis: models.Diagnosis{Confidence: 0.9, RootCause: "oom", Severity: "critical", Evidence: []string{"mem"}},
		Action:    models.Action{Type: "patch_resources", Namespace: "default", Target: "web", Rationale: "raise limits"},
		Steps: []models.RunbookStep{
			{Order: 1, Type: "patch_resources", Namespace: "default", Target: "web", Parameters: map[string]string{"memory_limit": "512Mi"}, Rationale: "raise memory"},
			{Order: 2, Type: "wait", Namespace: "default", Target: "web", WaitSeconds: 10, Rationale: "allow rollout"},
			{Order: 3, Type: "restart_pod", Namespace: "default", Target: "web-abc", Rationale: "restart with new limits"},
		},
		Risk: models.Risk{BlastRadius: "single_pod", Reversible: true, EstimatedTimeToResolve: "2m"},
	}
	anomalies := []models.AnomalyRecord{
		{Entity: "default/web-abc", Namespace: "default", Status: models.AnomalyStatusDetected},
	}
	if err := validateRunbookSteps(plan, anomalies, cfg); err != nil {
		t.Fatalf("expected valid runbook: %v", err)
	}
}

func TestAssignTierPlan_UsesHighestStep(t *testing.T) {
	ta := NewTierAssigner(0)
	plan := models.RemediationPlan{
		Steps: []models.RunbookStep{
			{Type: "restart_pod", Namespace: "default", Target: "a"},
			{Type: "rollback_deployment", Namespace: "default", Target: "web"},
		},
		Risk: models.Risk{BlastRadius: "single_pod", Reversible: true, EstimatedTimeToResolve: "2m"},
	}
	tier := ta.AssignTierPlan(plan)
	if tier != models.RiskTier2 {
		t.Fatalf("expected T2 from rollback step, got %s", tier)
	}
}
