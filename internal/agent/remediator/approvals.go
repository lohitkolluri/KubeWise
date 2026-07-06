package remediator

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// RemediationState returns the active remediation mode.
func (c *Correlator) RemediationState() models.RemediationModeView {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return models.RemediationModeView{
		Mode:   c.cfg.Mode,
		DryRun: c.cfg.DryRun,
		Live:   !c.cfg.DryRun,
	}
}

// ApplyRemediationConfig syncs from persisted agent config.
func (c *Correlator) ApplyRemediationConfig(cfg models.RemediationConfig) {
	rem := RemediationConfig{
		Mode:          cfg.Mode,
		DryRun:        cfg.DryRun,
		Allowlist:     cfg.Allowlist,
		Denylist:      cfg.NamespaceDenylist,
		MinConfidence: cfg.MinConfidence,
		RateLimit:     cfg.RateLimit,
	}
	if rem.MinConfidence <= 0 {
		rem.MinConfidence = c.cfg.MinConfidence
	}
	if rem.RateLimit <= 0 {
		rem.RateLimit = c.cfg.RateLimit
	}
	c.UpdateRemediationConfig(rem)
}

// ListPendingApprovals returns audit records awaiting human approval.
func (c *Correlator) ListPendingApprovals(limit int) ([]models.AuditRecord, error) {
	return c.store.ListAuditRecordsByStatus(models.AuditPending, limit)
}

// ApproveRecord executes a pending-approval remediation after operator sign-off.
func (c *Correlator) ApproveRecord(ctx context.Context, id string) error {
	record, err := c.store.GetAuditRecord(id)
	if err != nil {
		return err
	}
	if record.Status != models.AuditPending {
		return fmt.Errorf("record %s is not pending approval (status=%s)", record.ID, record.Status)
	}
	if c.executor == nil {
		return fmt.Errorf("k8s executor unavailable")
	}

	plan := record.Plan
	result, err := c.executor.ExecuteForce(ctx, plan)
	now := time.Now()
	record.Reason = "approved by operator"
	if err != nil {
		record.Status = models.AuditFailed
		record.Error = err.Error()
		record.K8sResult = result
		_ = c.store.UpdateAuditRecord(record)
		return fmt.Errorf("execute approved plan: %w", err)
	}

	steps := plan.EffectiveSteps()
	if record.RiskTier == models.RiskTier2 {
		for _, step := range steps {
			if step.Type != waitActionType {
				c.tierAssigner.SetCooldown(step.Namespace, step.Type)
			}
		}
	}

	verifyNote := c.verifyAfterRemediation(ctx, plan, result)
	anomalies, _ := c.store.ListAnomalies(100)
	matched := c.anomaliesMatchingPlan(anomalies, plan)

	if verifyNote == "" {
		record.Status = models.AuditVerified
		record.VerificationNote = verifyNote
		record.ExecutedAt = &now
		record.VerifiedAt = &now
		record.K8sResult = result
		if err := c.store.UpdateAuditRecord(record); err != nil {
			return err
		}
		c.markAnomalyStatus(matched, models.AnomalyStatusResolved, &now)
		log.Printf("remediator: approved, executed & verified %d-step runbook %s/%s", len(steps), plan.Action.Namespace, plan.Action.Target)
		return nil
	}

	record.Status = models.AuditVerifyFailed
	record.VerificationNote = verifyNote
	record.ExecutedAt = &now
	record.K8sResult = result
	if err := c.store.UpdateAuditRecord(record); err != nil {
		return err
	}
	c.markAnomalyStatus(matched, models.AnomalyStatusRemediated, &now)
	log.Printf("remediator: approved & executed but verification failed: %s", verifyNote)
	return nil
}

// RejectRecord declines a pending remediation.
func (c *Correlator) RejectRecord(id, reason string) error {
	record, err := c.store.GetAuditRecord(id)
	if err != nil {
		return err
	}
	if record.Status != models.AuditPending {
		return fmt.Errorf("record %s is not pending approval (status=%s)", record.ID, record.Status)
	}
	if reason == "" {
		reason = "rejected by operator"
	}
	record.Status = models.AuditRejected
	record.Reason = reason
	return c.store.UpdateAuditRecord(record)
}

// SetLiveMode enables or disables dry-run execution.
func (c *Correlator) SetLiveMode(live bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.cfg.DryRun = !live
	if live && c.cfg.Mode == models.RemediationModeDryRun {
		c.cfg.Mode = models.RemediationModeAuto
	}
	if !live {
		c.cfg.Mode = models.RemediationModeDryRun
	}
	if c.executor != nil {
		c.executor.SetDryRun(c.cfg.DryRun)
	}
	log.Printf("remediator: live mode=%v (mode=%s dry_run=%v)", live, c.cfg.Mode, c.cfg.DryRun)
}

// UpdateRemediationConfig applies runtime config and syncs the executor dry-run flag.
func (c *Correlator) UpdateRemediationConfig(cfg RemediationConfig) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if cfg.Mode == "" {
		cfg.Mode = c.cfg.Mode
	}
	c.cfg = cfg
	if c.executor != nil {
		c.executor.SetDryRun(cfg.DryRun)
	}
	log.Printf("remediator: config updated mode=%s dry_run=%v", cfg.Mode, cfg.DryRun)
}
