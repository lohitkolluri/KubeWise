package remediator

import (
	"testing"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

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
		Action:    models.Action{Type: "restart", Namespace: "default", Target: "pod-1", Rationale: "test"},
		Diagnosis: models.Diagnosis{Confidence: 0.5},
	}
	if err := c.validatePlan(plan); err == nil {
		t.Fatal("expected min confidence validation error")
	}
}

func TestValidatePlan_Allowlist(t *testing.T) {
	c := &Correlator{cfg: RemediationConfig{MinConfidence: 0.5, Allowlist: []string{"restart"}}}
	plan := models.RemediationPlan{
		Action:    models.Action{Type: "delete", Namespace: "default", Target: "pod-1", Rationale: "test"},
		Diagnosis: models.Diagnosis{Confidence: 0.9},
	}
	if err := c.validatePlan(plan); err == nil {
		t.Fatal("expected allowlist validation error")
	}
}

func TestValidatePlan_AllowlistPass(t *testing.T) {
	c := &Correlator{cfg: RemediationConfig{MinConfidence: 0.5, Allowlist: []string{"restart"}}}
	plan := models.RemediationPlan{
		Action:    models.Action{Type: "restart", Namespace: "default", Target: "pod-1", Rationale: "test"},
		Diagnosis: models.Diagnosis{Confidence: 0.9},
	}
	if err := c.validatePlan(plan); err != nil {
		t.Fatalf("expected plan to pass validation: %v", err)
	}
}
