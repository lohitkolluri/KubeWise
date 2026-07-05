package remediator

import (
	"context"
	"fmt"
	"log"
	"math/rand"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Correlator orchestrates the LLM-based remediation pipeline:
// collect anomalies → call LLM → assign risk tier → execute → audit.
type Correlator struct {
	llmClient    *llm.Client
	tierAssigner *TierAssigner
	executor     *K8sExecutor
	store        *store.Store
	cfg          RemediationConfig
}

// RemediationConfig controls correlator behavior.
type RemediationConfig struct {
	Mode            string   // "dry-run", "auto", "off"
	DryRun          bool     // when true, log actions but don't execute
	Allowlist       []string // allowed action types (empty = all)
	Denylist        []string // denied namespaces
	MinConfidence   float64  // minimum LLM confidence to execute
}

// NewCorrelator creates the remediation pipeline.
func NewCorrelator(llmClient *llm.Client, executor *K8sExecutor, s *store.Store, cfg RemediationConfig) *Correlator {
	return &Correlator{
		llmClient:    llmClient,
		tierAssigner: NewTierAssigner(5 * time.Minute),
		executor:     executor,
		store:        s,
		cfg:          cfg,
	}
}

// RunOnce executes one remediation cycle: fetch anomalies, correlate via LLM, execute.
func (c *Correlator) RunOnce(ctx context.Context) error {
	if c.cfg.Mode == "off" {
		return nil
	}

	// 1. Fetch recent un-remediated anomalies
	anomalies, err := c.store.ListAnomalies(10)
	if err != nil {
		return fmt.Errorf("list anomalies: %w", err)
	}

	if len(anomalies) == 0 {
		return nil // nothing to do
	}

	correlatable := c.filterNewAnomalies(anomalies)
	if len(correlatable) == 0 {
		return nil
	}

	log.Printf("remediator: analyzing %d anomaly(s)", len(correlatable))

	// 2. Build the LLM prompt
	userPrompt := llm.BuildUserPrompt(correlatable, "")
	systemPrompt := llm.SystemPrompt()
	schema := llm.RemediationSchema()

	// 3. Call LLM
	var plan models.RemediationPlan
	if err := c.llmClient.StructuredOutput(ctx, systemPrompt, userPrompt, schema, &plan); err != nil {
		return fmt.Errorf("llm correlation: %w", err)
	}

	// 4. Validate the plan
	if err := c.validatePlan(plan); err != nil {
		c.logAudit(&plan, "", models.RiskTier4, models.AuditRejected, fmt.Sprintf("validation failed: %v", err), userPrompt, "")
		return fmt.Errorf("plan validation: %w", err)
	}

	// 5. Assign risk tier
	tier := c.tierAssigner.AssignTier(plan)
	log.Printf("remediator: plan action=%s target=%s/%s tier=%s confidence=%.2f",
		plan.Action.Type, plan.Action.Namespace, plan.Action.Target, tier, plan.Diagnosis.Confidence)

	// 6. Gate by risk tier
	reason := c.gateByTier(tier, plan)
	if reason != "" {
		c.logAudit(&plan, "", tier, models.AuditRejected, reason, userPrompt, "")
		log.Printf("remediator: rejected - %s", reason)
		return nil // rejected isn't a pipeline error
	}

	// 7. Execute
	anomalyID := ""
	if len(correlatable) > 0 {
		anomalyID = correlatable[0].ID
	}

	status := models.AuditApproved
	if c.cfg.DryRun {
		status = models.AuditDryRun
		log.Printf("remediator: [dry-run] would execute %s %s/%s", plan.Action.Type, plan.Action.Namespace, plan.Action.Target)
		c.logAudit(&plan, anomalyID, tier, status, "dry-run mode", userPrompt, "")
		return nil
	}

	result, err := c.executor.Execute(ctx, plan)
	if err != nil {
		status = models.AuditFailed
		c.logAudit(&plan, anomalyID, tier, status, err.Error(), userPrompt, result)
		return fmt.Errorf("execute %s: %w", plan.Action.Type, err)
	}

	// 8. Set cooldown for T2 actions
	if tier == models.RiskTier2 {
		c.tierAssigner.SetCooldown(plan.Action.Namespace, plan.Action.Type)
	}

	status = models.AuditExecuted
	c.logAudit(&plan, anomalyID, tier, status, reason, userPrompt, result)
	log.Printf("remediator: executed %s %s/%s: %s", plan.Action.Type, plan.Action.Namespace, plan.Action.Target, result)
	return nil
}

// filterNewAnomalies returns anomalies that haven't been remediated yet.
func (c *Correlator) filterNewAnomalies(records []models.AnomalyRecord) []models.AnomalyRecord {
	var filtered []models.AnomalyRecord
	for _, r := range records {
		if r.Status == "remediated" || r.Status == "resolved" {
			continue
		}
		// Skip denied namespaces
		denied := false
		for _, d := range c.cfg.Denylist {
			if r.Namespace == d {
				denied = true
				break
			}
		}
		if denied {
			continue
		}
		filtered = append(filtered, r)
	}
	return filtered
}

// validatePlan checks the LLM's plan against basic constraints.
func (c *Correlator) validatePlan(plan models.RemediationPlan) error {
	if plan.Action.Type == "" {
		return fmt.Errorf("action type is empty")
	}
	if plan.Action.Namespace == "" {
		return fmt.Errorf("namespace is empty")
	}
	if plan.Action.Target == "" && plan.Action.Type != "noop" && plan.Action.Type != "escalate" {
		return fmt.Errorf("target is empty for action type %s", plan.Action.Type)
	}
	if plan.Diagnosis.Confidence < 0 || plan.Diagnosis.Confidence > 1 {
		return fmt.Errorf("confidence out of range: %f", plan.Diagnosis.Confidence)
	}
	if plan.Action.Rationale == "" {
		return fmt.Errorf("rationale is empty")
	}
	return nil
}

// gateByTier returns a rejection reason if the plan should not be executed.
// Empty string means approved.
func (c *Correlator) gateByTier(tier models.RiskTier, plan models.RemediationPlan) string {
	switch tier {
	case models.RiskTier1:
		return "" // auto-execute
	case models.RiskTier2:
		if !c.tierAssigner.CheckCooldown(plan.Action.Namespace, plan.Action.Type) {
			return fmt.Sprintf("cooldown active for %s/%s", plan.Action.Namespace, plan.Action.Type)
		}
		return "" // execute after cooldown check
	case models.RiskTier3:
		return fmt.Sprintf("T3 action requires human approval: %s %s/%s", plan.Action.Type, plan.Action.Namespace, plan.Action.Target)
	case models.RiskTier4:
		return fmt.Sprintf("T4 action rejected: %s %s/%s (blast radius: %s)", plan.Action.Type, plan.Action.Namespace, plan.Action.Target, plan.Risk.BlastRadius)
	default:
		return fmt.Sprintf("unknown tier: %s", tier)
	}
}

// logAudit records a remediation decision to the store.
func (c *Correlator) logAudit(plan *models.RemediationPlan, anomalyID string, tier models.RiskTier, status models.AuditStatus, reason, prompt, k8sResult string) {
	now := time.Now()
	record := &models.AuditRecord{
		ID:          shortID(),
		AnomalyID:   anomalyID,
		Plan:        *plan,
		RiskTier:    tier,
		Status:      status,
		Reason:      reason,
		Prompt:      prompt,
		LLMResponse: fmt.Sprintf("%+v", *plan),
		K8sResult:   k8sResult,
		CreatedAt:   now,
	}

	if status == models.AuditExecuted {
		record.ExecutedAt = &now
	}

	if err := c.store.SaveAuditRecord(record); err != nil {
		log.Printf("remediator: failed to save audit record: %v", err)
	}
}

// shortID generates a short unique identifier for audit records.
func shortID() string {
	const chars = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, 12)
	for i := range b {
		b[i] = chars[rand.Intn(len(chars))]
	}
	now := time.Now().UnixMilli()
	return fmt.Sprintf("%x-%s", now, string(b))
}
