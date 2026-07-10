package remediator

import (
	"fmt"
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// validatePlan is a thin wrapper that delegates to validateRemediationPlan with the correlator's config.
func (c *Correlator) validatePlan(plan models.RemediationPlan, anomalies []models.AnomalyRecord) error {
	return validateRemediationPlan(c.cfg, plan, anomalies)
}

func (c *Correlator) shouldRetryAfterValidation(err error) bool {
	if err == nil {
		return false
	}
	// Keep this conservative: only retry for clearly fixable structural omissions.
	msg := strings.ToLower(err.Error())
	for _, sub := range []string{
		"patch_resources requires at least one resource parameter",
		"scale_replicas requires replicas parameter",
		"target is empty for action type",
	} {
		if strings.Contains(msg, sub) {
			return true
		}
	}
	return false
}

// validateRemediationPlan checks a single action/plan for policy and structural validity.
func validateRemediationPlan(cfg RemediationConfig, plan models.RemediationPlan, anomalies []models.AnomalyRecord) error {
	if plan.Action.Type == "" {
		return fmt.Errorf("action type is empty")
	}
	if !knownActionTypes[plan.Action.Type] {
		return fmt.Errorf("unknown action type %q", plan.Action.Type)
	}
	if plan.Action.Namespace == "" {
		return fmt.Errorf("namespace is empty")
	}
	if isBuiltInProtectedNamespace(plan.Action.Namespace) {
		return fmt.Errorf("namespace %s is protected", plan.Action.Namespace)
	}
	for _, d := range cfg.Denylist {
		if plan.Action.Namespace == d {
			return fmt.Errorf("namespace %s is denied", plan.Action.Namespace)
		}
	}
	if plan.Action.Target == "" && plan.Action.Type != "noop" && plan.Action.Type != "escalate" {
		return fmt.Errorf("target is empty for action type %s", plan.Action.Type)
	}
	if plan.Diagnosis.Confidence < 0 || plan.Diagnosis.Confidence > 1 {
		return fmt.Errorf("confidence out of range: %f", plan.Diagnosis.Confidence)
	}
	// Only enforce MinConfidence for actions that would actually change the cluster.
	// Low-confidence outcomes should be able to return noop/escalate safely.
	if plan.Action.Type != "noop" && plan.Action.Type != "escalate" {
		if plan.Diagnosis.Confidence < cfg.MinConfidence {
			return fmt.Errorf("confidence %.2f below minimum %.2f", plan.Diagnosis.Confidence, cfg.MinConfidence)
		}
	}
	if plan.Action.Type != "noop" && plan.Action.Type != "escalate" {
		if !validBlastRadii[plan.Risk.BlastRadius] {
			return fmt.Errorf("invalid blast_radius %q", plan.Risk.BlastRadius)
		}
		if !planMatchesAnomalies(plan, anomalies) {
			return fmt.Errorf("plan target %s/%s does not match any input anomaly", plan.Action.Namespace, plan.Action.Target)
		}
	}
	if len(cfg.Allowlist) > 0 {
		allowed := false
		for _, a := range cfg.Allowlist {
			if a == plan.Action.Type {
				allowed = true
				break
			}
		}
		if !allowed {
			return fmt.Errorf("action type %q not in allowlist", plan.Action.Type)
		}
	}
	if plan.Action.Rationale == "" {
		return fmt.Errorf("rationale is empty")
	}
	switch plan.Action.Type {
	case "scale_replicas":
		if _, ok := plan.Action.Parameters["replicas"]; !ok {
			return fmt.Errorf("scale_replicas requires replicas parameter")
		}
	case "patch_resources":
		hasResource := false
		for k := range plan.Action.Parameters {
			switch k {
			case "cpu_request", "cpu_limit", "memory_request", "memory_limit":
				hasResource = true
			}
		}
		if !hasResource {
			return fmt.Errorf("patch_resources requires at least one resource parameter")
		}
	}
	return nil
}

// validateRunbookSteps validates every non-wait step in a remediation runbook.
func validateRunbookSteps(plan models.RemediationPlan, anomalies []models.AnomalyRecord, cfg RemediationConfig) error {
	steps := plan.EffectiveSteps()
	if len(steps) > maxRunbookSteps {
		return fmt.Errorf("runbook exceeds max %d steps", maxRunbookSteps)
	}
	for i, step := range steps {
		if step.Type == waitActionType {
			continue
		}
		sub := models.RemediationPlan{
			Action:    models.StepToAction(step),
			Diagnosis: plan.Diagnosis,
			Risk:      plan.Risk,
		}
		if err := validateRemediationPlan(cfg, sub, anomalies); err != nil {
			return fmt.Errorf("step %d: %w", i+1, err)
		}
	}
	return nil
}

func planMatchesAnomalies(plan models.RemediationPlan, anomalies []models.AnomalyRecord) bool {
	target := plan.Action.Target
	if deploymentActions[plan.Action.Type] {
		target = deploymentFromPlan(plan)
	}
	for _, a := range anomalies {
		if matchTarget(a, plan.Action.Namespace, target, plan.Action.Type) {
			return true
		}
	}
	return false
}
