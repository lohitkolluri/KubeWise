package remediator

import (
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func testAnomalies() []models.AnomalyRecord {
	return []models.AnomalyRecord{
		{ID: "1", Entity: "default/pod-1", Namespace: "default", Status: models.AnomalyStatusDetected},
	}
}

func TestFilterNewAnomalies_SkipsCorrelated(t *testing.T) {
	c := &Correlator{}
	records := []models.AnomalyRecord{
		{ID: "1", Status: models.AnomalyStatusDetected},
		{ID: "2", Status: models.AnomalyStatusCorrelated},
		{ID: "3", Status: models.AnomalyStatusRemediated},
	}
	filtered := c.filterNewAnomalies(records)
	if len(filtered) != 1 || filtered[0].ID != "1" {
		t.Fatalf("expected only detected anomaly, got %v", filtered)
	}
}

func TestValidatePlan_MinConfidence(t *testing.T) {
	c := &Correlator{cfg: RemediationConfig{MinConfidence: 0.7}}
	plan := models.RemediationPlan{
		Action:    models.Action{Type: "restart_pod", Namespace: "default", Target: "pod-1", Rationale: "test"},
		Diagnosis: models.Diagnosis{Confidence: 0.5},
		Risk:      models.Risk{BlastRadius: "single_pod"},
	}
	if err := c.validatePlan(plan, testAnomalies()); err == nil {
		t.Fatal("expected min confidence validation error")
	}
}

func TestValidatePlan_Allowlist(t *testing.T) {
	c := &Correlator{cfg: RemediationConfig{MinConfidence: 0.5, Allowlist: []string{"restart_pod"}}}
	plan := models.RemediationPlan{
		Action:    models.Action{Type: "delete_pod", Namespace: "default", Target: "pod-1", Rationale: "test"},
		Diagnosis: models.Diagnosis{Confidence: 0.9},
		Risk:      models.Risk{BlastRadius: "single_pod"},
	}
	if err := c.validatePlan(plan, testAnomalies()); err == nil {
		t.Fatal("expected allowlist validation error")
	}
}

func TestValidatePlan_AllowlistPass(t *testing.T) {
	c := &Correlator{cfg: RemediationConfig{MinConfidence: 0.5, Allowlist: []string{"restart_pod"}}}
	plan := models.RemediationPlan{
		Action:    models.Action{Type: "restart_pod", Namespace: "default", Target: "pod-1", Rationale: "test"},
		Diagnosis: models.Diagnosis{Confidence: 0.9},
		Risk:      models.Risk{BlastRadius: "single_pod"},
	}
	if err := c.validatePlan(plan, testAnomalies()); err != nil {
		t.Fatalf("expected plan to pass validation: %v", err)
	}
}

func TestValidatePlan_ProtectedNamespace(t *testing.T) {
	c := &Correlator{cfg: RemediationConfig{MinConfidence: 0.5}}
	plan := models.RemediationPlan{
		Action:    models.Action{Type: "restart_pod", Namespace: "kube-system", Target: "pod-1", Rationale: "test"},
		Diagnosis: models.Diagnosis{Confidence: 0.9},
		Risk:      models.Risk{BlastRadius: "single_pod"},
	}
	if err := c.validatePlan(plan, testAnomalies()); err == nil {
		t.Fatal("expected protected namespace error")
	}
}

func TestAnomalyMatchesTarget(t *testing.T) {
	a := models.AnomalyRecord{Entity: "default/web-1", Namespace: "default"}
	if !matchTarget(a, "default", "web-1", "restart_pod") {
		t.Fatal("expected match")
	}
	if matchTarget(a, "kube-system", "web-1", "restart_pod") {
		t.Fatal("expected namespace mismatch")
	}
	a2 := models.AnomalyRecord{Entity: "default/nginx-7d4f8b-xk2lm", Namespace: "default"}
	if !matchTarget(a2, "default", "nginx", "rollback_deployment") {
		t.Fatal("expected pod to match deployment action")
	}
}
