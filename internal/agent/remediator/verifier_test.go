package remediator

import (
	"testing"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestDefaultVerificationChecks_RestartPodUsesDeployment(t *testing.T) {
	plan := models.RemediationPlan{
		Action: models.Action{
			Type:      "restart_pod",
			Namespace: "default",
			Target:    "api-6b9c7b9c8f-abcde",
		},
		Steps: []models.RunbookStep{{
			Order:     1,
			Type:      "restart_pod",
			Namespace: "default",
			Target:    "api-6b9c7b9c8f-abcde",
			Rationale: "test",
		}},
	}

	checks := defaultVerificationChecks(plan)
	if len(checks) != 1 {
		t.Fatalf("expected 1 check, got %d", len(checks))
	}
	if checks[0].Type != "deployment_ready" {
		t.Fatalf("expected deployment_ready, got %q", checks[0].Type)
	}
	if checks[0].Target != "api" {
		t.Fatalf("expected inferred deployment api, got %q", checks[0].Target)
	}
}

func TestVerifier_DefaultWaitLongerForDeployment(t *testing.T) {
	wait := defaultVerifyWait([]models.VerificationCheck{{Type: "deployment_ready"}})
	if wait < 40*time.Second {
		t.Fatalf("expected wait >=40s, got %s", wait)
	}
}

