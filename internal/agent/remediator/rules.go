package remediator

import (
	"context"
	"log/slog"

	"github.com/lohitkolluri/KubeWise/internal/engine"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

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
		trend := metricTrendDirection(pts)
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
			slog.Info("remediator: rule matched but needs LLM", "rule", rr.RuleName)
		}
	}
	return processed, nil
}

// processRuleResult runs a rule-sourced plan through the existing pipeline:
// validate → tier assign → gate → dry-run → execute → verify.
// All safety gates apply: tier assignments, cooldowns, T3 approval, dry-run.
func (c *Correlator) processRuleResult(ctx context.Context, cfg RemediationConfig, plan models.RemediationPlan, rr engine.RuleResult, correlatable []models.AnomalyRecord) bool {
	if err := validateRunbookSteps(plan, correlatable, cfg); err != nil {
		slog.Error("remediator: rule plan validation failed", "rule", rr.RuleName, "error", err)
		return false
	}
	tier := c.tierAssigner.AssignTierPlan(plan)
	matched := c.anomaliesMatchingPlan(correlatable, plan)

	slog.Info("remediator: rule result", "rule", rr.RuleName, "action", plan.Action.Type, "target", plan.Action.Namespace+"/"+plan.Action.Target, "tier", tier, "confidence", plan.Diagnosis.Confidence)

	reason := c.gateByTierPlan(tier, plan)
	if reason != "" {
		c.logAudit(&plan, matched, tier, models.AuditRejected, reason, "rule_engine:"+rr.RuleName, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		slog.Warn("remediator: rule rejected", "rule", rr.RuleName, "reason", reason)
		return false
	}

	if tier == models.RiskTier3 {
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		if cfg.DryRun {
			slog.Info("remediator: dry-run rule T3 needs approval when live", "rule", rr.RuleName, "action", plan.Action.Type, "namespace", plan.Action.Namespace, "target", plan.Action.Target)
			c.logAudit(&plan, matched, tier, models.AuditDryRun, "dry-run T3 (rule engine)", "rule_engine:"+rr.RuleName, "")
			return true
		}
		c.logAudit(&plan, matched, tier, models.AuditPending,
			"awaiting human approval (T3, rule engine)", "rule_engine:"+rr.RuleName, "")
		slog.Info("remediator: rule T3 pending approval", "rule", rr.RuleName, "action", plan.Action.Type, "namespace", plan.Action.Namespace, "target", plan.Action.Target)
		return true
	}

	if plan.Action.Type == "escalate" {
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		c.logAudit(&plan, matched, tier, models.AuditEscalated, "escalated to human operator (rule engine)", "rule_engine:"+rr.RuleName, "")
		slog.Warn("remediator: rule escalated to human operator", "rule", rr.RuleName, "namespace", plan.Action.Namespace, "target", plan.Action.Target)
		return true
	}

	if cfg.DryRun {
		slog.Info("remediator: dry-run rule would execute", "rule", rr.RuleName, "namespace", plan.Action.Namespace, "target", plan.Action.Target)
		c.markAnomalyStatus(matched, models.AnomalyStatusCorrelated, nil)
		c.logAudit(&plan, matched, tier, models.AuditDryRun, "dry-run mode (rule engine)", "rule_engine:"+rr.RuleName, c.dryRunSummary(plan))
		return true
	}

	if c.executor == nil {
		c.logAudit(&plan, matched, tier, models.AuditRejected, "k8s executor unavailable", "rule_engine:"+rr.RuleName, "")
		c.markAnomalyStatus(matched, models.AnomalyStatusRejected, nil)
		slog.Error("remediator: rule rejected — k8s executor unavailable", "rule", rr.RuleName)
		return false
	}

	if err := c.executePlanAndVerify(ctx, plan, tier, matched, "rule_engine:"+rr.RuleName, ""); err != nil {
		slog.Error("remediator: rule execution failed", "rule", rr.RuleName, "error", err)
		return false
	}
	slog.Info("remediator: rule executed", "rule", rr.RuleName, "namespace", plan.Action.Namespace, "target", plan.Action.Target)
	return true
}
