// Package remediator plans and executes remediation actions against the cluster.
package remediator

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
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
		Mode:            cfg.Mode,
		DryRun:          cfg.DryRun,
		Allowlist:       cfg.Allowlist,
		Denylist:        cfg.NamespaceDenylist,
		MinConfidence:   cfg.MinConfidence,
		RateLimit:       cfg.RateLimit,
		WatchNamespaces: cfg.WatchNamespaces,
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
	cfg := c.snapshotConfig()

	anomalies, err := c.store.ListAnomalies(100)
	if err != nil {
		return fmt.Errorf("list anomalies for approval: %w", err)
	}
	matched := c.anomaliesForRecord(anomalies, record)
	if len(matched) == 0 {
		matched = c.anomaliesMatchingPlan(anomalies, plan)
	}

	if err := validateRunbookSteps(plan, matched, cfg); err != nil {
		return fmt.Errorf("approved plan no longer valid: %w", err)
	}
	tier := c.tierAssigner.AssignTierPlan(plan)
	if reason := c.gateByTierPlan(tier, plan); reason != "" {
		return fmt.Errorf("approved plan blocked by policy: %s", reason)
	}

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
		slog.Info("remediator: approved, executed and verified runbook", "steps", len(steps), "namespace", plan.Action.Namespace, "target", plan.Action.Target)
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
	slog.Warn("remediator: approved and executed but verification failed", "note", verifyNote)
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
	if err := c.store.UpdateAuditRecord(record); err != nil {
		return err
	}
	anomalies, err := c.store.ListAnomalies(100)
	if err == nil {
		matched := c.anomaliesForRecord(anomalies, record)
		if len(matched) == 0 {
			matched = c.anomaliesMatchingPlan(anomalies, record.Plan)
		}
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
	}
	return nil
}

func (c *Correlator) anomaliesForRecord(all []models.AnomalyRecord, record *models.AuditRecord) []models.AnomalyRecord {
	if record == nil || len(record.AnomalyIDs) == 0 {
		return nil
	}
	ids := make(map[string]struct{}, len(record.AnomalyIDs))
	for _, id := range record.AnomalyIDs {
		ids[id] = struct{}{}
	}
	var matched []models.AnomalyRecord
	for _, a := range all {
		if _, ok := ids[a.ID]; ok {
			matched = append(matched, a)
		}
	}
	return matched
}

// SetLiveMode enables or disables dry-run execution.
func (c *Correlator) SetLiveMode(live bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.cfg.DryRun = !live
	// Transition auto ↔ dry-run based on live flag, but preserve explicit
	// modes (semi, off) that were already applied via ApplyRemediationConfig.
	if live && c.cfg.Mode == models.RemediationModeDryRun {
		c.cfg.Mode = models.RemediationModeAuto
	} else if !live && c.cfg.Mode == models.RemediationModeAuto {
		c.cfg.Mode = models.RemediationModeDryRun
	}
	if c.executor != nil {
		c.executor.SetDryRun(c.cfg.DryRun)
	}
	slog.Info("remediator: live mode set", "live", live, "mode", c.cfg.Mode, "dry_run", c.cfg.DryRun)
}

// SetObservabilityURLs updates Loki/Tempo endpoints used during investigation.
func (c *Correlator) SetObservabilityURLs(lokiURL, tempoURL string) {
	if c == nil || c.investigator == nil {
		return
	}
	c.investigator.SetObservabilityURLs(lokiURL, tempoURL)
	slog.Info("remediator: observability URLs updated", "loki_url", strings.TrimSpace(lokiURL), "tempo_url", strings.TrimSpace(tempoURL))
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
	slog.Info("remediator: config updated", "mode", cfg.Mode, "dry_run", cfg.DryRun)
}
