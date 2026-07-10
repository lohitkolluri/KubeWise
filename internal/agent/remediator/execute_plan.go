package remediator

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// executePlanAndVerify runs an approved remediation, applies T2 cooldowns, and records audit state.
func (c *Correlator) executePlanAndVerify(
	ctx context.Context,
	plan models.RemediationPlan,
	tier models.RiskTier,
	matched []models.AnomalyRecord,
	prompt string,
	auditReason string,
) error {
	if c.executor == nil {
		c.logAudit(&plan, matched, tier, models.AuditRejected, "k8s executor unavailable", prompt, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		return fmt.Errorf("k8s executor unavailable")
	}

	steps := plan.EffectiveSteps()
	result, err := c.executor.Execute(ctx, plan)
	if err != nil {
		c.logAudit(&plan, matched, tier, models.AuditFailed, err.Error(), prompt, result)
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		return fmt.Errorf("execute runbook: %w", err)
	}

	if tier == models.RiskTier2 {
		for _, step := range steps {
			if step.Type != waitActionType {
				c.tierAssigner.SetCooldown(step.Namespace, step.Type)
			}
		}
	}

	now := time.Now()
	verifyNote := c.verifyAfterRemediation(ctx, plan, result)
	if verifyNote == "" {
		c.markAnomalyStatus(matched, models.AnomalyStatusResolved, &now)
		c.logAuditVerified(&plan, matched, tier, auditReason, prompt, result, verifyNote, &now)
		slog.Info("remediator: executed and verified runbook", "steps", len(steps), "namespace", plan.Action.Namespace, "target", plan.Action.Target)
		return nil
	}

	c.markAnomalyStatus(matched, models.AnomalyStatusRemediated, &now)
	c.logAudit(&plan, matched, tier, models.AuditVerifyFailed, verifyNote, prompt, result)
	slog.Warn("remediator: executed runbook but verification failed", "note", verifyNote)
	return nil
}
