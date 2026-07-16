package remediator

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
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

var validBlastRadii = map[string]bool{
	"single_pod":    true,
	"multiple_pods": true,
	"service":       true,
	"cluster":       true,
}

var knownActionTypes = map[string]bool{
	"restart_pod": true, "delete_pod": true, "scale_replicas": true,
	"rollback_deployment": true, "patch_resources": true, "view_logs": true, "noop": true, "escalate": true,
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
	Mode                    string   // "dry-run", "auto", "off"
	DryRun                  bool     // when true, log actions but don't execute
	Allowlist               []string // allowed action types (empty = all)
	Denylist                []string // denied namespaces
	MinConfidence           float64  // minimum LLM confidence to execute
	RateLimit               int      // max anomalies per LLM call (0 = unlimited)
	WatchNamespaces         []string // empty = all namespaces (minus denylist)
	RuleConfidenceThreshold float64  // minimum confidence for rule engine to auto-remediate (default 0.9)
}

// NewCorrelator creates the remediation pipeline.
func NewCorrelator(llmClient *llm.Client, executor *K8sExecutor, s *store.Store, cfg RemediationConfig, lokiURL, tempoURL string, ff featureflags.Flags, ruleEngine *engine.RuleEngine, llmRouter *llmrouter.LLMRouter) *Correlator {
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
		investigator: newInvestigatorFromExecutor(executor, lokiURL, tempoURL),
		verifier:     newVerifierFromExecutor(executor),
		store:        s,
		cfg:          cfg,
		featureFlags: ff,
		ruleEngine:   ruleEngine,
		semCache: semcache.New(semcache.Config{
			Persist: store.NewSemCacheBackend(s),
		}),
	}
}

func newInvestigatorFromExecutor(exec *K8sExecutor, lokiURL, tempoURL string) *Investigator {
	if exec == nil {
		return nil
	}
	if strings.TrimSpace(lokiURL) != "" || strings.TrimSpace(tempoURL) != "" {
		return NewInvestigatorWithObservability(exec.Clientset(), lokiURL, tempoURL)
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
		slog.Error("remediator: skipping — LLM client unavailable or no API key")
		return nil
	}
	if cfg.RateLimit > 0 && len(correlatable) > cfg.RateLimit {
		correlatable = correlatable[:cfg.RateLimit]
	}

	slog.Info("remediator: analyzing anomalies", "count", len(correlatable))

	// Prune expired cache entries before any cache operations.
	if c.featureFlags.SemanticCache && c.semCache != nil {
		if removed := c.semCache.Prune(); removed > 0 {
			slog.Info("remediator: pruned expired cache entries", "count", removed)
		}
	}

	// Extract metric series once and share between rule engine and LLM code paths,
	// avoiding duplicate N+1 SQLite queries per RunOnce cycle.
	metricsData := c.extractMetricSeries(correlatable)

	// Rule engine fast path (gated by feature flag). Runs before investigator/LLM to short-circuit
	// known failure patterns that don't need LLM reasoning.
	if c.featureFlags.RuleEngine && c.ruleEngine != nil {
		match, err := c.evaluateRules(ctx, cfg, correlatable, metricsData)
		if err != nil {
			slog.Error("remediator: rule engine error", "error", err)
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
			slog.Info("remediator: semantic cache hit — reusing cached plan")
		}
	}

	metricsSummary := c.buildMetricsSummary(metricsData)
	systemPrompt := llm.SystemPrompt()
	schema := llm.RemediationSchema()

	plan, userPrompt, fromCache, err := c.resolvePlan(ctx, correlatable, cachedPlan, metricsSummary, systemPrompt, schema)
	if err != nil {
		return err
	}

	if err := c.validateAndRepairPlan(ctx, &plan, correlatable, cfg, userPrompt, systemPrompt, schema); err != nil {
		return nil // validation failures are non-fatal — anomalies stay correlatable
	}

	if isIncompletePatchPlan(plan) {
		escalateForIncompletePatch(&plan)
		if err := validateRunbookSteps(plan, correlatable, cfg); err != nil {
			c.logAudit(&plan, correlatable, models.RiskTier4, models.AuditFailed, fmt.Sprintf("plan validation failed: %v", err), userPrompt, "")
			c.markAnomalyStatus(correlatable, models.AnomalyStatusCorrelated, nil)
			slog.Error("remediator: plan validation failed", "error", err)
			return nil
		}
	}

	// Cache the validated plan so future identical anomalies skip the LLM.
	if c.featureFlags.SemanticCache && !fromCache {
		c.storeCachedPlan(correlatable, plan)
	}

	tier := c.tierAssigner.AssignTierPlan(plan)
	steps := plan.EffectiveSteps()
	slog.Info("remediator: plan", "steps", len(steps), "action", plan.Action.Type, "target", plan.Action.Namespace+"/"+plan.Action.Target, "tier", tier, "confidence", plan.Diagnosis.Confidence)

	matched := c.anomaliesMatchingPlan(correlatable, plan)

	reason := c.gateByTierPlan(tier, plan)
	if reason != "" {
		c.logAudit(&plan, matched, tier, models.AuditRejected, reason, userPrompt, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		slog.Warn("remediator: rejected", "reason", reason)
		return nil
	}

	if plan.Action.Type == "escalate" {
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		escalateReason := plan.Action.Rationale
		if escalateReason == "" {
			escalateReason = "escalated to human operator"
		}
		c.logAudit(&plan, matched, tier, models.AuditEscalated, escalateReason, userPrompt, "")
		slog.Warn("remediator: escalated to human operator", "namespace", plan.Action.Namespace, "target", plan.Action.Target, "reason", escalateReason)
		return nil
	}

	if tier == models.RiskTier3 || tier == models.RiskTier4 {
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		msg := "awaiting human approval (" + string(tier) + ")"
		if cfg.DryRun {
			msg = "dry-run: would require human approval (" + string(tier) + ")"
			slog.Info("remediator: dry-run needs approval when live", "tier", tier, "action", plan.Action.Type, "namespace", plan.Action.Namespace, "target", plan.Action.Target)
			c.logAudit(&plan, matched, tier, models.AuditDryRun, msg, userPrompt, "")
			return nil
		}
		c.logAudit(&plan, matched, tier, models.AuditPending, msg, userPrompt, "")
		slog.Info("remediator: pending approval", "tier", tier, "action", plan.Action.Type, "namespace", plan.Action.Namespace, "target", plan.Action.Target)
		return nil
	}

	if cfg.DryRun {
		slog.Info("remediator: dry-run would execute runbook", "steps", len(steps), "namespace", plan.Action.Namespace, "target", plan.Action.Target)
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		c.logAudit(&plan, matched, tier, models.AuditDryRun, "dry-run mode", userPrompt, c.dryRunSummary(plan))
		return nil
	}

	if c.executor == nil {
		c.logAudit(&plan, matched, tier, models.AuditRejected, "k8s executor unavailable", userPrompt, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		slog.Error("remediator: rejected - k8s executor unavailable")
		return nil
	}

	return c.executePlanAndVerify(ctx, plan, tier, matched, userPrompt, reason)
}

// resolvePlan returns a plan from cache or by calling the LLM.
// Returns the plan, user prompt text, whether it came from cache, and any error.
func (c *Correlator) resolvePlan(ctx context.Context, correlatable []models.AnomalyRecord, cachedPlan *models.RemediationPlan, metricsSummary, systemPrompt string, schema json.RawMessage) (models.RemediationPlan, string, bool, error) {
	if cachedPlan != nil {
		plan := *cachedPlan
		normalizePlan(&plan, correlatable)
		return plan, "", true, nil
	}

	var investigation string
	if c.investigator != nil {
		invCtx, invCancel := gatherTimeout(ctx)
		investigation = c.investigator.Gather(invCtx, correlatable)
		invCancel()
		if investigation != "" {
			slog.Info("remediator: gathered cluster investigation context", "bytes", len(investigation))
		}
	}

	userPrompt := llm.BuildUserPrompt(correlatable, metricsSummary, investigation)
	var plan models.RemediationPlan

	if c.llmRouter != nil && c.featureFlags.LLMRouter {
		llmInput := llmrouter.LLMInput{
			SystemPrompt:   systemPrompt,
			UserContent:    userPrompt,
			ResponseSchema: schema,
		}
		if _, err := c.llmRouter.RouteStructured(ctx, llmrouter.TaskRCA, llmInput, &plan); err != nil {
			return plan, "", false, fmt.Errorf("llm router correlation: %w", err)
		}
	} else {
		if err := c.llmClient.StructuredOutput(ctx, systemPrompt, userPrompt, schema, &plan); err != nil {
			return plan, "", false, fmt.Errorf("llm correlation: %w", err)
		}
	}

	normalizePlan(&plan, correlatable)
	plan.Investigation = models.InvestigationContext{Summary: investigation}
	return plan, userPrompt, false, nil
}

// validateAndRepairPlan validates the plan and attempts one-shot repair for
// common structural issues. Returns nil if the plan is valid (or was repaired
// to validity); on failure the anomalies remain correlatable for a future cycle.
func (c *Correlator) validateAndRepairPlan(ctx context.Context, plan *models.RemediationPlan, correlatable []models.AnomalyRecord, cfg RemediationConfig, userPrompt, systemPrompt string, schema json.RawMessage) error {
	if err := validateRunbookSteps(*plan, correlatable, cfg); err != nil {
		if c.shouldRetryAfterValidation(err) {
			repaired := c.repairAfterFailure(ctx, err, *plan, correlatable, cfg, userPrompt, systemPrompt, schema)
			*plan = repaired
		}

		if err != nil && (isIncompletePatchError(err) || isIncompletePatchPlan(*plan)) {
			escalateForIncompletePatch(plan)
			if verr := validateRunbookSteps(*plan, correlatable, cfg); verr == nil {
				err = nil
			} else {
				err = verr
			}
		}

		if err != nil {
			c.logAudit(plan, correlatable, models.RiskTier4, models.AuditFailed, fmt.Sprintf("plan validation failed: %v", err), userPrompt, "")
			c.markAnomalyStatus(correlatable, models.AnomalyStatusCorrelated, nil)
			slog.Error("remediator: plan validation failed", "error", err)
			return err
		}
	}
	return nil
}

// repairAfterFailure sends the plan back to the LLM with repair instructions.
func (c *Correlator) repairAfterFailure(ctx context.Context, _ error, plan models.RemediationPlan, correlatable []models.AnomalyRecord, cfg RemediationConfig, userPrompt, systemPrompt string, schema json.RawMessage) models.RemediationPlan {
	repairSystem := systemPrompt + "\n\nREPAIR INSTRUCTIONS:\n" +
		"- If you choose patch_resources, you MUST include at least one of cpu_request,cpu_limit,memory_request,memory_limit.\n" +
		"- If you cannot supply required parameters for patch_resources, choose escalate (not noop) so the operator can patch manually.\n" +
		"- If you cannot supply required parameters for scale_replicas, choose escalate or restart_pod when appropriate.\n" +
		"- Never output empty parameters for patch_resources or scale_replicas.\n"

	var repaired models.RemediationPlan
	var rerr error
	if c.llmRouter != nil && c.featureFlags.LLMRouter {
		llmInput := llmrouter.LLMInput{SystemPrompt: repairSystem, UserContent: userPrompt, ResponseSchema: schema}
		_, rerr = c.llmRouter.RouteStructured(ctx, llmrouter.TaskRemediation, llmInput, &repaired)
	} else {
		rerr = c.llmClient.StructuredOutput(ctx, repairSystem, userPrompt, schema, &repaired)
	}
	if rerr != nil {
		return plan
	}

	normalizePlan(&repaired, correlatable)
	repaired.Investigation = models.InvestigationContext{Summary: plan.Investigation.Summary}
	if verr := validateRunbookSteps(repaired, correlatable, cfg); verr != nil {
		return plan
	}
	return repaired
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

func (c *Correlator) verifyAfterRemediation(ctx context.Context, plan models.RemediationPlan, _ string) string {
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

func (c *Correlator) snapshotConfig() RemediationConfig {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.cfg
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
			slog.Error("remediator: update anomaly status", "id", records[i].ID, "status", status, "error", err)
		}
	}
}

func (c *Correlator) buildMetricsSummary(data []metricData) string {
	summaries := make([]llm.MetricSummary, 0, len(data))
	for _, d := range data {
		if len(d.Pts) == 0 {
			continue
		}
		last := d.Pts[len(d.Pts)-1]
		summaries = append(summaries, llm.MetricSummary{
			Name:        d.Name,
			SampleCount: len(d.Pts),
			LastValue:   last.Value,
			Trend:       metricTrendDirection(d.Pts),
		})
	}
	return llm.FormatMetricContext(summaries)
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
