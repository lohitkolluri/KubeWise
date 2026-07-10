package cli

import (
	"strings"
	"testing"

	"github.com/charmbracelet/x/ansi"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func TestFormatParamChipsSorted(t *testing.T) {
	got := ansi.Strip(formatParamChips(map[string]string{
		"memory_limit":   "2Gi",
		"cpu_limit":      "500m",
		"cpu_request":    "200m",
		"memory_request": "1Gi",
	}))
	if !strings.Contains(got, "cpu limit=500m") {
		t.Fatalf("expected cpu limit chip, got %q", got)
	}
	if strings.Index(got, "cpu limit") > strings.Index(got, "memory limit") {
		t.Fatalf("expected sorted chips, got %q", got)
	}
}

func TestWritePlanDetailRunbookLayout(t *testing.T) {
	var b strings.Builder
	writePlanSteps(&b, 80, []models.RunbookStep{
		{
			Order:     1,
			Type:      "patch_resources",
			Namespace: "monitoring",
			Target:    "prometheus-abc",
			Rationale: "Increase memory limits before OOM.",
			Parameters: map[string]string{
				"memory_limit": "2Gi",
				"cpu_limit":    "500m",
			},
		},
		{
			Order:       2,
			Type:        "wait",
			Namespace:   "monitoring",
			Target:      "prometheus-abc",
			WaitSeconds: 30,
			Rationale:   "Wait for rollout.",
		},
	})
	out := ansi.Strip(b.String())
	if strings.Contains(out, "patch_resources monitoring/") {
		t.Fatalf("runbook should not cram target into header line: %q", out)
	}
	if !strings.Contains(out, "Patch resources") {
		t.Fatalf("expected humanized action type: %q", out)
	}
	if !strings.Contains(out, "memory limit=2Gi") {
		t.Fatalf("expected param chips: %q", out)
	}
	if !strings.Contains(out, "Wait 30s") {
		t.Fatalf("expected wait step label: %q", out)
	}
}

func TestWritePlanVerificationHumanized(t *testing.T) {
	var b strings.Builder
	writePlanVerification(&b, 80, models.VerificationPlan{
		Checks: []models.VerificationCheck{{
			Type:      "pod_ready",
			Namespace: "monitoring",
			Target:    "prometheus-abc",
		}},
		WaitSeconds: 15,
	})
	out := b.String()
	if !strings.Contains(out, "Pod ready on monitoring/prometheus-abc") {
		t.Fatalf("expected humanized verification: %q", out)
	}
}
