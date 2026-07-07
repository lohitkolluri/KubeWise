package remediator

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const maxRunbookSteps = 5

var waitActionType = "wait"

// ExecuteRunbook runs all steps in order and returns a combined result string.
func (e *K8sExecutor) ExecuteRunbook(ctx context.Context, plan models.RemediationPlan, dryRun bool) (string, error) {
	steps := plan.EffectiveSteps()
	if len(steps) == 0 {
		return "", fmt.Errorf("runbook has no steps")
	}

	var results []string
	for i, step := range steps {
		if step.Type == waitActionType {
			sec := step.WaitSeconds
			if sec <= 0 {
				if v, ok := step.Parameters["seconds"]; ok {
					fmt.Sscanf(v, "%d", &sec) //nolint:errcheck
				}
			}
			if sec <= 0 {
				sec = 10
			}
			msg := fmt.Sprintf("step %d: wait %ds (%s)", i+1, sec, step.Rationale)
			if dryRun {
				results = append(results, "[dry-run] would "+msg)
				continue
			}
			log.Printf("remediator: %s", msg)
			select {
			case <-ctx.Done():
				return strings.Join(results, "; "), ctx.Err()
			case <-time.After(time.Duration(sec) * time.Second):
				results = append(results, msg)
			}
			continue
		}

		sub := models.RemediationPlan{
			Action: models.StepToAction(step),
			Risk:   plan.Risk,
		}
		out, err := e.execute(ctx, sub, dryRun)
		prefix := fmt.Sprintf("step %d (%s)", i+1, step.Type)
		if err != nil {
			return strings.Join(results, "; "), fmt.Errorf("%s: %w", prefix, err)
		}
		results = append(results, fmt.Sprintf("%s: %s", prefix, out))

		if step.WaitSeconds > 0 && !dryRun {
			select {
			case <-ctx.Done():
				return strings.Join(results, "; "), ctx.Err()
			case <-time.After(time.Duration(step.WaitSeconds) * time.Second):
			}
		}
	}
	return strings.Join(results, "; "), nil
}

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
		c := &Correlator{cfg: cfg}
		if err := c.validatePlan(sub, anomalies); err != nil {
			return fmt.Errorf("step %d: %w", i+1, err)
		}
	}
	return nil
}

func (ta *TierAssigner) AssignTierPlan(plan models.RemediationPlan) models.RiskTier {
	steps := plan.EffectiveSteps()
	if len(steps) == 0 {
		return models.RiskTier4
	}
	max := models.RiskTier1
	for _, step := range steps {
		if step.Type == waitActionType {
			continue
		}
		sub := models.RemediationPlan{
			Action: models.StepToAction(step),
			Risk:   plan.Risk,
		}
		t := ta.AssignTier(sub)
		if tierToInt(t) > tierToInt(max) {
			max = t
		}
	}
	return max
}

func (c *Correlator) gateByTierPlan(tier models.RiskTier, plan models.RemediationPlan) string {
	if tier == models.RiskTier2 {
		for _, step := range plan.EffectiveSteps() {
			if step.Type == waitActionType {
				continue
			}
			if !c.tierAssigner.CheckCooldown(step.Namespace, step.Type) {
				return fmt.Sprintf("cooldown active for %s/%s", step.Namespace, step.Type)
			}
		}
	}
	return c.gateByTier(tier, plan)
}

func syncActionFromSteps(plan *models.RemediationPlan) {
	if plan == nil || len(plan.Steps) == 0 {
		return
	}
	first := plan.Steps[0]
	for i := range plan.Steps {
		if plan.Steps[i].Type != waitActionType {
			first = plan.Steps[i]
			break
		}
	}
	plan.Action = models.StepToAction(first)
}
