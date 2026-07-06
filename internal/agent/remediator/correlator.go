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

var protectedNamespaces = map[string]bool{
	"kube-system":     true,
	"kube-public":     true,
	"kube-node-lease": true,
}

var validBlastRadii = map[string]bool{
	"single_pod":    true,
	"multiple_pods": true,
	"service":       true,
	"cluster":       true,
}

var knownActionTypes = map[string]bool{
	"restart_pod": true, "delete_pod": true, "scale_replicas": true,
	"rollback_deployment": true, "patch_resources": true, "noop": true, "escalate": true,
}

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
	Mode          string   // "dry-run", "auto", "off"
	DryRun        bool     // when true, log actions but don't execute
	Allowlist     []string // allowed action types (empty = all)
	Denylist      []string // denied namespaces
	MinConfidence float64  // minimum LLM confidence to execute
	RateLimit     int      // max anomalies per LLM call (0 = unlimited)
}

// NewCorrelator creates the remediation pipeline.
func NewCorrelator(llmClient *llm.Client, executor *K8sExecutor, s *store.Store, cfg RemediationConfig) *Correlator {
	if cfg.Mode == "" {
		cfg.Mode = "dry-run"
	}
	if cfg.MinConfidence <= 0 {
		cfg.MinConfidence = 0.7
	}
	if cfg.RateLimit <= 0 {
		cfg.RateLimit = 10
	}
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

	anomalies, err := c.store.ListAnomalies(50)
	if err != nil {
		return fmt.Errorf("list anomalies: %w", err)
	}

	if len(anomalies) == 0 {
		return nil
	}

	correlatable := c.filterNewAnomalies(anomalies)
	if len(correlatable) == 0 {
		return nil
	}
	if c.cfg.RateLimit > 0 && len(correlatable) > c.cfg.RateLimit {
		correlatable = correlatable[:c.cfg.RateLimit]
	}

	log.Printf("remediator: analyzing %d anomaly(s)", len(correlatable))

	userPrompt := llm.BuildUserPrompt(correlatable, "")
	systemPrompt := llm.SystemPrompt()
	schema := llm.RemediationSchema()

	var plan models.RemediationPlan
	if err := c.llmClient.StructuredOutput(ctx, systemPrompt, userPrompt, schema, &plan); err != nil {
		return fmt.Errorf("llm correlation: %w", err)
	}

	if err := c.validatePlan(plan, correlatable); err != nil {
		c.logAudit(&plan, correlatable, models.RiskTier4, models.AuditRejected, fmt.Sprintf("validation failed: %v", err), userPrompt, "")
		return fmt.Errorf("plan validation: %w", err)
	}

	tier := c.tierAssigner.AssignTier(plan)
	log.Printf("remediator: plan action=%s target=%s/%s tier=%s confidence=%.2f",
		plan.Action.Type, plan.Action.Namespace, plan.Action.Target, tier, plan.Diagnosis.Confidence)

	matched := c.anomaliesMatchingPlan(correlatable, plan)

	reason := c.gateByTier(tier, plan)
	if reason != "" {
		c.logAudit(&plan, matched, tier, models.AuditRejected, reason, userPrompt, "")
		log.Printf("remediator: rejected - %s", reason)
		return nil
	}

	c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)

	if plan.Action.Type == "escalate" {
		c.logAudit(&plan, matched, tier, models.AuditEscalated, "escalated to human operator", userPrompt, "")
		log.Printf("remediator: escalated %s/%s to human operator", plan.Action.Namespace, plan.Action.Target)
		return nil
	}

	if c.cfg.DryRun {
		log.Printf("remediator: [dry-run] would execute %s %s/%s", plan.Action.Type, plan.Action.Namespace, plan.Action.Target)
		c.logAudit(&plan, matched, tier, models.AuditDryRun, "dry-run mode", userPrompt, "")
		return nil
	}

	if c.executor == nil {
		c.logAudit(&plan, matched, tier, models.AuditRejected, "k8s executor unavailable", userPrompt, "")
		log.Printf("remediator: rejected - k8s executor unavailable")
		return nil
	}

	result, err := c.executor.Execute(ctx, plan)
	if err != nil {
		c.logAudit(&plan, matched, tier, models.AuditFailed, err.Error(), userPrompt, result)
		return fmt.Errorf("execute %s: %w", plan.Action.Type, err)
	}

	if tier == models.RiskTier2 {
		c.tierAssigner.SetCooldown(plan.Action.Namespace, plan.Action.Type)
	}

	now := time.Now()
	c.markAnomalyStatus(matched, models.AnomalyStatusRemediated, &now)
	c.logAudit(&plan, matched, tier, models.AuditExecuted, reason, userPrompt, result)
	log.Printf("remediator: executed %s %s/%s: %s", plan.Action.Type, plan.Action.Namespace, plan.Action.Target, result)
	return nil
}

func (c *Correlator) filterNewAnomalies(records []models.AnomalyRecord) []models.AnomalyRecord {
	var filtered []models.AnomalyRecord
	for _, r := range records {
		switch r.Status {
		case models.AnomalyStatusCorrelated, models.AnomalyStatusRejected,
			models.AnomalyStatusRemediated, models.AnomalyStatusResolved:
			continue
		}
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

func (c *Correlator) markAnomalyStatus(records []models.AnomalyRecord, status string, remediatedAt *time.Time) {
	for i := range records {
		records[i].Status = status
		if remediatedAt != nil {
			records[i].RemediatedAt = remediatedAt
		}
		if err := c.store.UpdateAnomaly(&records[i]); err != nil {
			log.Printf("remediator: update anomaly %s status=%s: %v", records[i].ID, status, err)
		}
	}
}

func (c *Correlator) validatePlan(plan models.RemediationPlan, anomalies []models.AnomalyRecord) error {
	if plan.Action.Type == "" {
		return fmt.Errorf("action type is empty")
	}
	if !knownActionTypes[plan.Action.Type] {
		return fmt.Errorf("unknown action type %q", plan.Action.Type)
	}
	if plan.Action.Namespace == "" {
		return fmt.Errorf("namespace is empty")
	}
	if protectedNamespaces[plan.Action.Namespace] {
		return fmt.Errorf("namespace %s is protected", plan.Action.Namespace)
	}
	for _, d := range c.cfg.Denylist {
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
	if plan.Diagnosis.Confidence < c.cfg.MinConfidence {
		return fmt.Errorf("confidence %.2f below minimum %.2f", plan.Diagnosis.Confidence, c.cfg.MinConfidence)
	}
	if plan.Action.Type != "noop" && plan.Action.Type != "escalate" {
		if !validBlastRadii[plan.Risk.BlastRadius] {
			return fmt.Errorf("invalid blast_radius %q", plan.Risk.BlastRadius)
		}
		if !c.planMatchesAnomalies(plan, anomalies) {
			return fmt.Errorf("plan target %s/%s does not match any input anomaly", plan.Action.Namespace, plan.Action.Target)
		}
	}
	if len(c.cfg.Allowlist) > 0 {
		allowed := false
		for _, a := range c.cfg.Allowlist {
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
	return nil
}

func (c *Correlator) planMatchesAnomalies(plan models.RemediationPlan, anomalies []models.AnomalyRecord) bool {
	for _, a := range anomalies {
		if anomalyMatchesTarget(a, plan.Action.Namespace, plan.Action.Target) {
			return true
		}
	}
	return false
}

func (c *Correlator) anomaliesMatchingPlan(anomalies []models.AnomalyRecord, plan models.RemediationPlan) []models.AnomalyRecord {
	if plan.Action.Type == "noop" || plan.Action.Type == "escalate" {
		return anomalies
	}
	var matched []models.AnomalyRecord
	for _, a := range anomalies {
		if anomalyMatchesTarget(a, plan.Action.Namespace, plan.Action.Target) {
			matched = append(matched, a)
		}
	}
	return matched
}

func anomalyMatchesTarget(a models.AnomalyRecord, namespace, target string) bool {
	ns, name := models.ParseEntity(a.Entity)
	if a.Namespace != "" && ns == "" {
		ns = a.Namespace
	}
	return ns == namespace && name == target
}

func (c *Correlator) gateByTier(tier models.RiskTier, plan models.RemediationPlan) string {
	switch tier {
	case models.RiskTier1:
		return ""
	case models.RiskTier2:
		if !c.tierAssigner.CheckCooldown(plan.Action.Namespace, plan.Action.Type) {
			return fmt.Sprintf("cooldown active for %s/%s", plan.Action.Namespace, plan.Action.Type)
		}
		return ""
	case models.RiskTier3:
		if plan.Action.Type == "escalate" {
			return ""
		}
		return fmt.Sprintf("T3 action requires human approval: %s %s/%s", plan.Action.Type, plan.Action.Namespace, plan.Action.Target)
	case models.RiskTier4:
		return fmt.Sprintf("T4 action rejected: %s %s/%s (blast radius: %s)", plan.Action.Type, plan.Action.Namespace, plan.Action.Target, plan.Risk.BlastRadius)
	default:
		return fmt.Sprintf("unknown tier: %s", tier)
	}
}

func (c *Correlator) logAudit(plan *models.RemediationPlan, anomalies []models.AnomalyRecord, tier models.RiskTier, status models.AuditStatus, reason, prompt, k8sResult string) {
	now := time.Now()
	anomalyID := ""
	if len(anomalies) > 0 {
		anomalyID = anomalies[0].ID
	}
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

func shortID() string {
	const chars = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, 12)
	for i := range b {
		b[i] = chars[rand.Intn(len(chars))]
	}
	now := time.Now().UnixMilli()
	return fmt.Sprintf("%x-%s", now, string(b))
}
