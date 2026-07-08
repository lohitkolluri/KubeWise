package remediator

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"
	"math/big"
	"strings"
	"sync"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/featureflags"
	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
	"github.com/lohitkolluri/KubeWise/internal/agent/notify"
	"github.com/lohitkolluri/KubeWise/internal/agent/semcache"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/internal/engine"
	"github.com/lohitkolluri/KubeWise/internal/llmrouter"
	"github.com/lohitkolluri/KubeWise/pkg/models"
	nsutil "github.com/lohitkolluri/KubeWise/pkg/namespace"
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
	// Tool plugin action types.
	"helm_upgrade": true, "helm_rollback": true,
	"argocd_sync": true, "argocd_rollback": true,
	"github_create_pr": true, "github_merge_pr": true,
	"terraform_apply": true,
}

// Correlator orchestrates the LLM-based remediation pipeline:
// collect anomalies → rule engine (fast path) → semantic cache (fast path) → call LLM → assign risk tier → execute → audit.
type Correlator struct {
	llmClient    *llm.Client
	llmRouter    *llmrouter.LLMRouter
	tierAssigner *TierAssigner
	executor     *K8sExecutor
	investigator *Investigator
	verifier     *Verifier
	store        *store.Store
	cfg          RemediationConfig
	notifier     *notify.Notifier
	featureFlags featureflags.Flags
	ruleEngine   *engine.RuleEngine
	semCache     *semcache.Cache
	mu           sync.RWMutex
}

// RemediationConfig controls correlator behavior.
type RemediationConfig struct {
	Mode                  string   // "dry-run", "auto", "off"
	DryRun                bool     // when true, log actions but don't execute
	Allowlist             []string // allowed action types (empty = all)
	Denylist              []string // denied namespaces
	MinConfidence         float64  // minimum LLM confidence to execute
	RateLimit             int      // max anomalies per LLM call (0 = unlimited)
	WatchNamespaces       []string // empty = all namespaces (minus denylist)
	RuleConfidenceThreshold float64 // minimum confidence for rule engine to auto-remediate (default 0.9)
}

// NewCorrelator creates the remediation pipeline.
func NewCorrelator(llmClient *llm.Client, executor *K8sExecutor, s *store.Store, cfg RemediationConfig, ff featureflags.Flags, ruleEngine *engine.RuleEngine, llmRouter *llmrouter.LLMRouter) *Correlator {
	if cfg.Mode == "" {
		cfg.Mode = "dry-run"
	}
	if cfg.MinConfidence <= 0 {
		cfg.MinConfidence = 0.7
	}
	if cfg.RuleConfidenceThreshold <= 0 {
		cfg.RuleConfidenceThreshold = 0.9
	}
	return &Correlator{
		llmClient:    llmClient,
		llmRouter:    llmRouter,
		tierAssigner: NewTierAssigner(5 * time.Minute),
		executor:     executor,
		investigator: newInvestigatorFromExecutor(executor),
		verifier:     newVerifierFromExecutor(executor),
		store:        s,
		cfg:          cfg,
		featureFlags: ff,
		ruleEngine:   ruleEngine,
		semCache:     semcache.New(semcache.Config{}),
	}
}

func newInvestigatorFromExecutor(exec *K8sExecutor) *Investigator {
	if exec == nil {
		return nil
	}
	return NewInvestigator(exec.Clientset())
}

func newVerifierFromExecutor(exec *K8sExecutor) *Verifier {
	if exec == nil {
		return nil
	}
	return NewVerifier(exec.Clientset())
}

// SetNotifier wires outbound webhook/Slack notifications for remediation events.
func (c *Correlator) SetNotifier(n *notify.Notifier) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.notifier = n
}

func (c *Correlator) emitAuditNotification(record *models.AuditRecord) {
	c.mu.RLock()
	n := c.notifier
	c.mu.RUnlock()
	if n == nil || record == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	n.NotifyRemediation(ctx, *record)
}

// RunOnce executes one remediation cycle: fetch anomalies, correlate via rule engine (fast path) or LLM, execute.
func (c *Correlator) RunOnce(ctx context.Context) error {
	cfg := c.snapshotConfig()
	if cfg.Mode == "off" {
		return nil
	}

	anomalies, err := c.store.ListAnomalies(c.anomalyFetchLimit(cfg))
	if err != nil {
		return fmt.Errorf("list anomalies: %w", err)
	}

	if len(anomalies) == 0 {
		return nil
	}

	correlatable := c.filterNewAnomalies(anomalies, cfg)
	if len(correlatable) == 0 {
		return nil
	}
	if c.llmClient == nil || !c.llmClient.HasAPIKey() {
		log.Printf("remediator: skipping — LLM client unavailable or no API key")
		return nil
	}
	if cfg.RateLimit > 0 && len(correlatable) > cfg.RateLimit {
		correlatable = correlatable[:cfg.RateLimit]
	}

	log.Printf("remediator: analyzing %d anomaly(s)", len(correlatable))

	// Prune expired cache entries before any cache operations.
	if c.featureFlags.SemanticCache && c.semCache != nil {
		if removed := c.semCache.Prune(); removed > 0 {
			log.Printf("remediator: pruned %d expired cache entries", removed)
		}
	}

	// Rule engine fast path (gated by feature flag). Runs before investigator/LLM to short-circuit
	// known failure patterns that don't need LLM reasoning.
	if c.featureFlags.RuleEngine && c.ruleEngine != nil {
		match, err := c.evaluateRules(ctx, cfg, correlatable)
		if err != nil {
			log.Printf("remediator: rule engine error: %v", err)
		}
		if match {
			return nil // short-circuited via rule engine path (audited inside)
		}
	}

	// Semantic cache fast path (gated by feature flag). Skips investigation and LLM for
	// anomaly patterns that were recently processed with identical fingerprints.
	var cachedPlan *models.RemediationPlan
	if c.featureFlags.SemanticCache && c.semCache != nil {
		if entry := c.lookupCachedPlan(correlatable); entry != nil {
			cachedPlan = entry
			log.Printf("remediator: semantic cache hit — reusing cached plan")
		}
	}

	metricsSummary := c.buildMetricsSummary(correlatable)

	var plan models.RemediationPlan
	var userPrompt, investigation string
	systemPrompt := llm.SystemPrompt()
	schema := llm.RemediationSchema()
	fromCache := cachedPlan != nil

	if fromCache {
		plan = *cachedPlan
		normalizePlan(&plan, correlatable)
	} else {
		if c.investigator != nil {
			invCtx, invCancel := gatherTimeout(ctx)
			investigation = c.investigator.Gather(invCtx, correlatable)
			invCancel()
			if investigation != "" {
				log.Printf("remediator: gathered cluster investigation context (%d bytes)", len(investigation))
			}
		}

		userPrompt = llm.BuildUserPrompt(correlatable, metricsSummary, investigation)
		if c.llmRouter != nil && c.featureFlags.LLMRouter {
			llmInput := llmrouter.LLMInput{
				SystemPrompt:   systemPrompt,
				UserContent:    userPrompt,
				ResponseSchema: schema,
			}
			if _, err := c.llmRouter.RouteStructured(ctx, llmrouter.TaskRCA, llmInput, &plan); err != nil {
				return fmt.Errorf("llm router correlation: %w", err)
			}
		} else {
			if err := c.llmClient.StructuredOutput(ctx, systemPrompt, userPrompt, schema, &plan); err != nil {
				return fmt.Errorf("llm correlation: %w", err)
			}
		}

		normalizePlan(&plan, correlatable)
		plan.Investigation = models.InvestigationContext{Summary: investigation}
	}

	if err := validateRunbookSteps(plan, correlatable, cfg); err != nil {
		// One-shot repair attempt for common structural issues where the model
		// picked a valid action type but omitted required parameters.
		if c.shouldRetryAfterValidation(err) {
			var repaired models.RemediationPlan
			repairSystem := systemPrompt + "\n\nREPAIR INSTRUCTIONS:\n" +
				"- If you choose patch_resources, you MUST include at least one of cpu_request,cpu_limit,memory_request,memory_limit.\n" +
				"- If you cannot supply required parameters, choose restart_pod for CrashLoop/BackOff, otherwise choose noop or escalate.\n" +
				"- Never output empty parameters for patch_resources or scale_replicas.\n"
			if c.llmRouter != nil && c.featureFlags.LLMRouter {
				llmInput := llmrouter.LLMInput{
					SystemPrompt:   repairSystem,
					UserContent:    userPrompt,
					ResponseSchema: schema,
				}
				if _, rerr := c.llmRouter.RouteStructured(ctx, llmrouter.TaskRemediation, llmInput, &repaired); rerr == nil {
					normalizePlan(&repaired, correlatable)
					repaired.Investigation = models.InvestigationContext{Summary: investigation}
					if verr := validateRunbookSteps(repaired, correlatable, cfg); verr == nil {
						plan = repaired
					} else {
						err = verr
					}
				}
			} else {
				if rerr := c.llmClient.StructuredOutput(ctx, repairSystem, userPrompt, schema, &repaired); rerr == nil {
					normalizePlan(&repaired, correlatable)
					repaired.Investigation = models.InvestigationContext{Summary: investigation}
					if verr := validateRunbookSteps(repaired, correlatable, cfg); verr == nil {
						plan = repaired
					} else {
						err = verr
					}
				}
			}
		}

		if err != nil {
			// Validation failures are not operator actions; treat as an internal failure
			// and keep anomalies in a correlatable state (so a future cycle can retry
			// with new context / different plan).
			c.logAudit(&plan, correlatable, models.RiskTier4, models.AuditFailed, fmt.Sprintf("plan validation failed: %v", err), userPrompt, "")
			c.markAnomalyStatus(correlatable, models.AnomalyStatusCorrelated, nil)
			log.Printf("remediator: plan validation failed: %v", err)
			return nil
		}
	}

	// Cache the validated plan so future identical anomalies skip the LLM.
	if c.featureFlags.SemanticCache && !fromCache {
		c.storeCachedPlan(correlatable, plan)
	}

	tier := c.tierAssigner.AssignTierPlan(plan)
	steps := plan.EffectiveSteps()
	log.Printf("remediator: plan steps=%d primary=%s target=%s/%s tier=%s confidence=%.2f",
		len(steps), plan.Action.Type, plan.Action.Namespace, plan.Action.Target, tier, plan.Diagnosis.Confidence)

	matched := c.anomaliesMatchingPlan(correlatable, plan)

	reason := c.gateByTierPlan(tier, plan)
	if reason != "" {
		c.logAudit(&plan, matched, tier, models.AuditRejected, reason, userPrompt, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		log.Printf("remediator: rejected - %s", reason)
		return nil
	}

	if tier == models.RiskTier3 {
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		msg := "awaiting human approval (T3)"
		if cfg.DryRun {
			msg = "dry-run: would require human approval (T3)"
			log.Printf("remediator: [dry-run] T3 %s %s/%s — needs approval when live", plan.Action.Type, plan.Action.Namespace, plan.Action.Target)
			c.logAudit(&plan, matched, tier, models.AuditDryRun, msg, userPrompt, "")
			return nil
		}
		c.logAudit(&plan, matched, tier, models.AuditPending, msg, userPrompt, "")
		log.Printf("remediator: pending approval T3 %s %s/%s", plan.Action.Type, plan.Action.Namespace, plan.Action.Target)
		return nil
	}

	if plan.Action.Type == "escalate" {
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		c.logAudit(&plan, matched, tier, models.AuditEscalated, "escalated to human operator", userPrompt, "")
		log.Printf("remediator: escalated %s/%s to human operator", plan.Action.Namespace, plan.Action.Target)
		return nil
	}

	if cfg.DryRun {
		log.Printf("remediator: [dry-run] would execute %d-step runbook on %s/%s", len(steps), plan.Action.Namespace, plan.Action.Target)
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		c.logAudit(&plan, matched, tier, models.AuditDryRun, "dry-run mode", userPrompt, c.dryRunSummary(plan))
		return nil
	}

	if c.executor == nil {
		c.logAudit(&plan, matched, tier, models.AuditRejected, "k8s executor unavailable", userPrompt, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		log.Printf("remediator: rejected - k8s executor unavailable")
		return nil
	}

	result, err := c.executor.Execute(ctx, plan)
	if err != nil {
		c.logAudit(&plan, matched, tier, models.AuditFailed, err.Error(), userPrompt, result)
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
		c.logAuditVerified(&plan, matched, tier, reason, userPrompt, result, verifyNote, &now)
		log.Printf("remediator: executed and verified %d-step runbook %s/%s", len(steps), plan.Action.Namespace, plan.Action.Target)
	} else {
		c.markAnomalyStatus(matched, models.AnomalyStatusRemediated, &now)
		c.logAudit(&plan, matched, tier, models.AuditVerifyFailed, verifyNote, userPrompt, result)
		log.Printf("remediator: executed runbook but verification failed: %s", verifyNote)
	}
	return nil
}

func (c *Correlator) dryRunSummary(plan models.RemediationPlan) string {
	steps := plan.EffectiveSteps()
	var parts []string
	for i, s := range steps {
		if s.Type == waitActionType {
			parts = append(parts, fmt.Sprintf("[dry-run] step %d: wait", i+1))
			continue
		}
		parts = append(parts, fmt.Sprintf("[dry-run] step %d: %s %s/%s", i+1, s.Type, s.Namespace, s.Target))
	}
	if len(plan.Verification.Checks) > 0 || plan.Verification.WaitSeconds > 0 {
		parts = append(parts, "[dry-run] would run post-remediation verification")
	}
	return strings.Join(parts, "; ")
}

func (c *Correlator) verifyAfterRemediation(ctx context.Context, plan models.RemediationPlan, execResult string) string {
	if plan.Action.Type == "noop" || plan.Action.Type == "escalate" {
		return ""
	}
	if c.verifier == nil {
		return "verifier unavailable"
	}
	if err := c.verifier.Verify(ctx, plan); err != nil {
		return err.Error()
	}
	return ""
}

func (c *Correlator) logAuditVerified(plan *models.RemediationPlan, anomalies []models.AnomalyRecord, tier models.RiskTier, reason, prompt, k8sResult, verifyNote string, verifiedAt *time.Time) {
	now := time.Now()
	anomalyIDs := make([]string, 0, len(anomalies))
	for _, a := range anomalies {
		anomalyIDs = append(anomalyIDs, a.ID)
	}
	record := &models.AuditRecord{
		ID:               shortID(),
		AnomalyIDs:       anomalyIDs,
		Plan:             *plan,
		RiskTier:         tier,
		Status:           models.AuditVerified,
		Reason:           reason,
		Prompt:           prompt,
		LLMResponse:      fmt.Sprintf("%+v", *plan),
		K8sResult:        k8sResult,
		VerificationNote: verifyNote,
		CreatedAt:        now,
		ExecutedAt:       verifiedAt,
		VerifiedAt:       verifiedAt,
	}
	if err := c.store.SaveAuditRecord(record); err != nil {
		log.Printf("remediator: failed to save audit record: %v", err)
		return
	}
	c.emitAuditNotification(record)
}

func (c *Correlator) snapshotConfig() RemediationConfig {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.cfg
}

// buildMetricSummaries extracts metric summaries from anomalies for the rule engine.
func (c *Correlator) buildMetricSummaries(anomalies []models.AnomalyRecord) []engine.MetricSummary {
	seen := make(map[string]struct{})
	var summaries []engine.MetricSummary
	for _, a := range anomalies {
		if a.MetricName == "" {
			continue
		}
		key := a.Entity + "|" + a.MetricName
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		ns, name := models.ParseEntity(a.Entity)
		if a.Namespace != "" && ns == "" {
			ns = a.Namespace
		}
		labels := map[string]string{"namespace": ns, "pod": name}
		pts, err := c.store.GetMetricSeries(a.MetricName, labels, 15)
		if err != nil || len(pts) == 0 {
			summaries = append(summaries, engine.MetricSummary{
				Name:    a.MetricName + "@" + a.Entity,
				Current: a.Score,
				Trend:   "unknown",
			})
			continue
		}
		last := pts[len(pts)-1]
		var total float64
		maxVal := pts[0].Value
		for _, p := range pts {
			total += p.Value
			if p.Value > maxVal {
				maxVal = p.Value
			}
		}
		trend := "stable"
		if len(pts) >= 2 && last.Value > pts[0].Value*1.1 {
			trend = "rising"
		} else if len(pts) >= 2 && last.Value < pts[0].Value*0.9 {
			trend = "falling"
		}
		summaries = append(summaries, engine.MetricSummary{
			Name:        a.MetricName + "@" + a.Entity,
			Current:     last.Value,
			Average:     total / float64(len(pts)),
			Max:         maxVal,
			SampleCount: len(pts),
			Trend:       trend,
		})
	}
	return summaries
}

// evaluateRules runs the rule engine against correlatable anomalies.
// Returns true if a deterministic match was processed (short-circuit LLM).
func (c *Correlator) evaluateRules(ctx context.Context, cfg RemediationConfig, correlatable []models.AnomalyRecord) (bool, error) {
	ruleInput := engine.EngineInput{
		Anomalies: correlatable,
		Metrics:   c.buildMetricSummaries(correlatable),
	}
	ruleResults, err := c.ruleEngine.Evaluate(ctx, ruleInput)
	if err != nil {
		return false, err
	}
	if len(ruleResults) == 0 {
		return false, nil
	}

	processed := false
	for _, rr := range ruleResults {
		if rr.Confidence >= cfg.RuleConfidenceThreshold && !rr.NeedsLLM {
			plan := engine.RuleToPlan(rr)
			ok := c.processRuleResult(ctx, cfg, plan, rr, correlatable)
			if ok {
				processed = true
			}
		}
		if rr.NeedsLLM {
			log.Printf("remediator: rule %s matched but needs LLM — falling through to LLM path", rr.RuleName)
		}
	}
	return processed, nil
}

// processRuleResult runs a rule-sourced plan through the existing pipeline:
// validate → tier assign → gate → dry-run → execute → verify.
// All safety gates apply: tier assignments, cooldowns, T3 approval, dry-run.
func (c *Correlator) processRuleResult(ctx context.Context, cfg RemediationConfig, plan models.RemediationPlan, rr engine.RuleResult, correlatable []models.AnomalyRecord) bool {
	if err := validateRunbookSteps(plan, correlatable, cfg); err != nil {
		log.Printf("remediator: rule %s plan validation failed: %v", rr.RuleName, err)
		return false
	}
	tier := c.tierAssigner.AssignTierPlan(plan)
	matched := c.anomaliesMatchingPlan(correlatable, plan)

	log.Printf("remediator: rule %s action=%s target=%s/%s tier=%s confidence=%.2f",
		rr.RuleName, plan.Action.Type, plan.Action.Namespace, plan.Action.Target, tier, plan.Diagnosis.Confidence)

	reason := c.gateByTierPlan(tier, plan)
	if reason != "" {
		c.logAudit(&plan, matched, tier, models.AuditRejected, reason, "rule_engine:"+rr.RuleName, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		log.Printf("remediator: rule %s rejected — %s", rr.RuleName, reason)
		return false
	}

	if tier == models.RiskTier3 {
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		if cfg.DryRun {
			log.Printf("remediator: [dry-run] rule %s T3 %s %s/%s — needs approval when live",
				rr.RuleName, plan.Action.Type, plan.Action.Namespace, plan.Action.Target)
			c.logAudit(&plan, matched, tier, models.AuditDryRun, "dry-run T3 (rule engine)", "rule_engine:"+rr.RuleName, "")
			return true
		}
		c.logAudit(&plan, matched, tier, models.AuditPending,
			"awaiting human approval (T3, rule engine)", "rule_engine:"+rr.RuleName, "")
		log.Printf("remediator: rule %s T3 %s %s/%s — pending approval",
			rr.RuleName, plan.Action.Type, plan.Action.Namespace, plan.Action.Target)
		return true
	}

	if plan.Action.Type == "escalate" {
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		c.logAudit(&plan, matched, tier, models.AuditEscalated, "escalated to human operator (rule engine)", "rule_engine:"+rr.RuleName, "")
		log.Printf("remediator: rule %s escalated %s/%s to human operator", rr.RuleName, plan.Action.Namespace, plan.Action.Target)
		return true
	}

	if cfg.DryRun {
		log.Printf("remediator: [dry-run] rule %s would execute %s/%s", rr.RuleName, plan.Action.Namespace, plan.Action.Target)
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		c.logAudit(&plan, matched, tier, models.AuditDryRun, "dry-run mode (rule engine)", "rule_engine:"+rr.RuleName, c.dryRunSummary(plan))
		return true
	}

	if c.executor == nil {
		c.logAudit(&plan, matched, tier, models.AuditRejected, "k8s executor unavailable", "rule_engine:"+rr.RuleName, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		log.Printf("remediator: rule %s rejected — k8s executor unavailable", rr.RuleName)
		return false
	}

	result, err := c.executor.Execute(ctx, plan)
	if err != nil {
		c.logAudit(&plan, matched, tier, models.AuditFailed, err.Error(), "rule_engine:"+rr.RuleName, result)
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		log.Printf("remediator: rule %s execution failed: %v", rr.RuleName, err)
		return false
	}

	if tier == models.RiskTier2 {
		for _, step := range plan.EffectiveSteps() {
			if step.Type != waitActionType {
				c.tierAssigner.SetCooldown(step.Namespace, step.Type)
			}
		}
	}

	now := time.Now()
	verifyNote := c.verifyAfterRemediation(ctx, plan, result)
	if verifyNote == "" {
		c.markAnomalyStatus(matched, models.AnomalyStatusResolved, &now)
		c.logAuditVerified(&plan, matched, tier, "", "rule_engine:"+rr.RuleName, result, verifyNote, &now)
		log.Printf("remediator: rule %s executed and verified %s/%s", rr.RuleName, plan.Action.Namespace, plan.Action.Target)
	} else {
		c.markAnomalyStatus(matched, models.AnomalyStatusRemediated, &now)
		c.logAudit(&plan, matched, tier, models.AuditVerifyFailed, verifyNote, "rule_engine:"+rr.RuleName, result)
		log.Printf("remediator: rule %s executed but verification failed: %s", rr.RuleName, verifyNote)
	}
	return true
}

func (c *Correlator) filterNewAnomalies(records []models.AnomalyRecord, cfg RemediationConfig) []models.AnomalyRecord {
	var filtered []models.AnomalyRecord
	for _, r := range records {
		switch r.Status {
		case models.AnomalyStatusCorrelated, models.AnomalyStatusRejected,
			models.AnomalyStatusRemediated, models.AnomalyStatusResolved:
			continue
		}
		denied := false
		for _, d := range cfg.Denylist {
			if r.Namespace == d {
				denied = true
				break
			}
		}
		if denied {
			continue
		}
		if !nsutil.InScope(r.Namespace, cfg.WatchNamespaces) {
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
	// Only enforce MinConfidence for actions that would actually change the cluster.
	// Low-confidence outcomes should be able to return noop/escalate safely.
	if plan.Action.Type != "noop" && plan.Action.Type != "escalate" {
		if plan.Diagnosis.Confidence < c.cfg.MinConfidence {
			return fmt.Errorf("confidence %.2f below minimum %.2f", plan.Diagnosis.Confidence, c.cfg.MinConfidence)
		}
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

func (c *Correlator) planMatchesAnomalies(plan models.RemediationPlan, anomalies []models.AnomalyRecord) bool {
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

func (c *Correlator) anomaliesMatchingPlan(anomalies []models.AnomalyRecord, plan models.RemediationPlan) []models.AnomalyRecord {
	if plan.Action.Type == "noop" || plan.Action.Type == "escalate" {
		return anomalies
	}
	target := plan.Action.Target
	if deploymentActions[plan.Action.Type] {
		target = deploymentFromPlan(plan)
	}
	var matched []models.AnomalyRecord
	for _, a := range anomalies {
		if matchTarget(a, plan.Action.Namespace, target, plan.Action.Type) {
			matched = append(matched, a)
		}
	}
	return matched
}

func matchTarget(a models.AnomalyRecord, namespace, target, actionType string) bool {
	ns, name := models.ParseEntity(a.Entity)
	if a.Namespace != "" && ns == "" {
		ns = a.Namespace
	}
	if ns != namespace {
		return false
	}
	if name == target {
		return true
	}
	if deploymentActions[actionType] {
		return podBelongsToDeployment(name, target)
	}
	return false
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
		return "" // queued for human approval after tier gate
	case models.RiskTier4:
		return fmt.Sprintf("T4 action rejected: %s %s/%s (blast radius: %s)", plan.Action.Type, plan.Action.Namespace, plan.Action.Target, plan.Risk.BlastRadius)
	default:
		return fmt.Sprintf("unknown tier: %s", tier)
	}
}

func (c *Correlator) buildMetricsSummary(anomalies []models.AnomalyRecord) string {
	var summaries []llm.MetricSummary
	seen := make(map[string]struct{})
	for _, a := range anomalies {
		key := a.Entity + "|" + a.MetricName
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		ns, name := models.ParseEntity(a.Entity)
		if a.Namespace != "" && ns == "" {
			ns = a.Namespace
		}
		labels := map[string]string{"namespace": ns, "pod": name}
		pts, err := c.store.GetMetricSeries(a.MetricName, labels, 15)
		if err != nil || len(pts) == 0 {
			continue
		}
		last := pts[len(pts)-1]
		trend := "stable"
		if len(pts) >= 2 && pts[len(pts)-1].Value > pts[0].Value*1.1 {
			trend = "rising"
		} else if len(pts) >= 2 && pts[len(pts)-1].Value < pts[0].Value*0.9 {
			trend = "falling"
		}
		summaries = append(summaries, llm.MetricSummary{
			Name:        a.MetricName + "@" + a.Entity,
			SampleCount: len(pts),
			LastValue:   last.Value,
			Trend:       trend,
		})
	}
	return llm.FormatMetricContext(summaries)
}

func (c *Correlator) logAudit(plan *models.RemediationPlan, anomalies []models.AnomalyRecord, tier models.RiskTier, status models.AuditStatus, reason, prompt, k8sResult string) {
	now := time.Now()
	anomalyIDs := make([]string, 0, len(anomalies))
	for _, a := range anomalies {
		anomalyIDs = append(anomalyIDs, a.ID)
	}
	record := &models.AuditRecord{
		ID:               shortID(),
		AnomalyID:        "",
		AnomalyIDs:       anomalyIDs,
		Plan:             *plan,
		RiskTier:         tier,
		Status:           status,
		Reason:           reason,
		Prompt:           prompt,
		LLMResponse:      fmt.Sprintf("%+v", *plan),
		K8sResult:        k8sResult,
		VerificationNote: "",
		CreatedAt:        now,
	}

	if status == models.AuditExecuted {
		record.ExecutedAt = &now
	}

	if err := c.store.SaveAuditRecord(record); err != nil {
		log.Printf("remediator: failed to save audit record: %v", err)
		return
	}
	c.emitAuditNotification(record)
}

func shortID() string {
	const chars = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, 12)
	for i := range b {
		n, err := rand.Int(rand.Reader, big.NewInt(int64(len(chars))))
		if err != nil {
			b[i] = chars[i%len(chars)]
			continue
		}
		b[i] = chars[n.Int64()]
	}
	now := time.Now().UnixMilli()
	return fmt.Sprintf("%x-%s", now, string(b))
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

func (c *Correlator) anomalyFetchLimit(cfg RemediationConfig) int {
	limit := cfg.RateLimit * 5
	if limit < 20 {
		limit = 20
	}
	if limit > 100 {
		limit = 100
	}
	return limit
}

// lookupCachedPlan checks the semantic cache for a matching anomaly fingerprint.
// Returns a deserialized RemediationPlan on cache hit, nil otherwise.
func (c *Correlator) lookupCachedPlan(anomalies []models.AnomalyRecord) *models.RemediationPlan {
	fp := fingerprintAnomalyGroup(anomalies)
	if fp == "" {
		return nil
	}
	entry := c.semCache.Get(fp)
	if entry == nil {
		return nil
	}
	var plan models.RemediationPlan
	if err := json.Unmarshal([]byte(entry.PlanJSON), &plan); err != nil {
		log.Printf("remediator: semcache deserialize error: %v", err)
		return nil
	}
	return &plan
}

// storeCachedPlan serializes and stores a RemediationPlan in the semantic cache.
func (c *Correlator) storeCachedPlan(anomalies []models.AnomalyRecord, plan models.RemediationPlan) {
	fp := fingerprintAnomalyGroup(anomalies)
	if fp == "" {
		return
	}
	data, err := json.Marshal(plan)
	if err != nil {
		log.Printf("remediator: semcache serialize error: %v", err)
		return
	}
	c.semCache.Set(fp, string(data))
}

// fingerprintAnomalyGroup computes a combined fingerprint for a group of anomalies.
// Uses a combined SHA256 hash of all individual anomaly fingerprints.
func fingerprintAnomalyGroup(anomalies []models.AnomalyRecord) string {
	if len(anomalies) == 0 {
		return ""
	}
	if len(anomalies) == 1 {
		a := anomalies[0]
		return semcache.Fingerprint(a.Entity, a.Namespace, a.MetricName, a.Pattern, a.Score)
	}
	h := sha256.New()
	for _, a := range anomalies {
		fp := semcache.Fingerprint(a.Entity, a.Namespace, a.MetricName, a.Pattern, a.Score)
		h.Write([]byte(fp))
		h.Write([]byte{0})
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}
