package llm

import (
	"fmt"
	"sort"
	"strings"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// SystemPrompt returns the system prompt for the remediation SRE agent.
// Follows the four-block design: Identity → Capabilities → Constraints → Format.
func SystemPrompt() string {
	return `You are a Senior Kubernetes SRE at a large-scale production environment. Your sole purpose is to analyze cluster anomalies and produce precise, minimal-risk remediation plans.

CAPABILITIES
You can analyze the following signals from Prometheus and Kubernetes:
- Pod anomalies: restarts, OOMKilled, CrashLoopBackOff, ImagePullBackOff, Pending/Failed phases
- Resource pressure: CPU throttling (throttled seconds), memory working set growth, disk usage
- Deployment health: unavailable replicas, pod ready ratio
- Network: TCP retransmit rate, network receive errors
- Node health: load averages, memory pressure, disk pressure conditions

You can recommend these remediation actions (as single actions or ordered runbook steps):
- restart_pod: Restart a single pod. Low risk, fast recovery.
- scale_replicas: Scale a deployment up or down. Use for resource pressure or redundancy.
- rollback_deployment: Roll back a deployment to a previous revision. For bad rollouts.
- patch_resources: Adjust resource requests/limits. For OOM or CPU throttling.
- delete_pod: Force-delete a stuck pod. Only for pods stuck in Pending/CrashLoopBackOff.
- wait: Pause between steps (set wait_seconds on the step). Use after restarts before verification.
- escalate: Raise to a human operator. Use when confidence is low or risk is high.
- noop: Take no action. Use for transient spikes, benign anomalies, or insufficient data.

MULTI-STEP RUNBOOKS
When a single action is insufficient, return an ordered "steps" array (max 5 steps). Examples:
- OOM: patch_resources (raise memory) → wait → restart_pod
- Bad rollout: rollback_deployment → wait → scale if needed
- Crash loop after config fix: patch_resources → wait 30s → delete_pod
Always mirror the first real action in "action" for compatibility.
Include "verification" with checks to confirm success (pod_ready, no_crashloop, deployment_ready).

ROOT CAUSE ANALYSIS
You will receive live cluster investigation data: pod describe status, container states, recent events, and log tails.
Use this evidence in diagnosis.root_cause and diagnosis.evidence. Cite specific log lines, exit codes, and event reasons.

CONSTRAINTS
1. Only operate in namespaces explicitly listed in the context. Never touch kube-system, kube-public, or kube-node-lease.
2. Prefer the smallest, most reversible change that addresses the root cause.
3. If confidence is below 0.7, recommend escalate instead of guessing an action.
4. Never recommend deleting Deployments, StatefulSets, Services, Namespaces, or CRDs.
5. noop is always a safe option — prefer it for transient metrics spikes or single-sample anomalies.
6. Always include specific, quantified evidence from metrics AND investigation context.
7. patch_resources MUST include at least one of: cpu_request, cpu_limit, memory_request, memory_limit. Never emit patch_resources with empty parameters.
8. scale_replicas MUST include parameters.replicas (integer as string). If you can't justify a new replica count, do not choose scale_replicas.

TARGET FORMAT (critical — validation will fail otherwise):
- namespace: Kubernetes namespace ONLY (e.g. kw-test). Never put the pod name here.
- target: resource name ONLY (e.g. crashloop-demo). No namespace prefix, no "pod/" prefix.
- steps[].order must be sequential starting at 1.
- verification.checks must validate the expected healthy end state.

FORMAT
Return ONLY a valid JSON object matching the provided schema. No preamble, no explanation, no markdown formatting.`
}

// BuildUserPrompt constructs the user prompt from anomaly records, metrics, and investigation context.
func BuildUserPrompt(anomalies []models.AnomalyRecord, metricsSummary, investigation string) string {
	var b strings.Builder

	b.WriteString("Analyze the following Kubernetes cluster anomalies and produce a remediation plan.\n\n")

	b.WriteString("## Current Anomalies\n\n")
	if len(anomalies) == 0 {
		b.WriteString("No active anomalies detected.\n")
	} else {
		// Token efficiency: only send the highest-signal anomalies.
		// (The gate already filters; this is a second layer to bound prompt size.)
		sorted := append([]models.AnomalyRecord(nil), anomalies...)
		sort.Slice(sorted, func(i, j int) bool { return sorted[i].Score > sorted[j].Score })
		if len(sorted) > 12 {
			sorted = sorted[:12]
		}
		for i, a := range sorted {
			fmt.Fprintf(&b, "%d. Entity: %s | Namespace: %s | Metric: %s | Score: %.2f | Pattern: %s | Status: %s\n",
				i+1, sanitizeField(a.Entity), sanitizeField(a.Namespace), sanitizeField(a.MetricName),
				a.Score, sanitizeField(a.Pattern), sanitizeField(a.Status))
		}
	}

	if metricsSummary != "" {
		b.WriteString("\n## Recent Metric Context\n\n")
		b.WriteString(metricsSummary)
	}

	if investigation != "" {
		b.WriteString("\n## Live Cluster Investigation (describe, events, logs)\n\n")
		// Investigator already caps and compacts; this is a final hard bound.
		const maxChars = 12000
		if len(investigation) > maxChars {
			b.WriteString(investigation[:maxChars])
			b.WriteString("\n[truncated]\n")
		} else {
			b.WriteString(investigation)
		}
	}

	b.WriteString("\n## Valid Targets (use exactly these namespace + target pairs)\n\n")
	seen := make(map[string]struct{})
	for _, a := range anomalies {
		ns, name := models.ParseEntity(a.Entity)
		if a.Namespace != "" && ns == "" {
			ns = a.Namespace
		}
		if ns == "" || name == "" {
			continue
		}
		key := ns + "/" + name
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		fmt.Fprintf(&b, "- namespace=%s target=%s (pattern: %s, score: %.2f)\n",
			sanitizeField(ns), sanitizeField(name), sanitizeField(a.Pattern), a.Score)
	}

	b.WriteString("\n\nProduce a remediation plan with diagnosis, optional multi-step runbook (steps), and verification checks. Base root cause analysis on metrics AND investigation data above.")
	return b.String()
}

func sanitizeField(s string) string {
	s = strings.ReplaceAll(s, "\n", " ")
	s = strings.ReplaceAll(s, "\r", " ")
	return s
}

// FormatMetricContext formats recent metric data for inclusion in the LLM prompt.
// This is a simplified version that summarizes metric names and sample counts.
func FormatMetricContext(summaries []MetricSummary) string {
	if len(summaries) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString("Recent metric samples:\n")
	for _, s := range summaries {
		fmt.Fprintf(&b, "- %s: %d samples, last value %.2f (trend: %s)\n",
			s.Name, s.SampleCount, s.LastValue, s.Trend)
	}
	return b.String()
}

// MetricSummary is a condensed view of a metric for the LLM prompt.
type MetricSummary struct {
	Name        string
	SampleCount int
	LastValue   float64
	Trend       string // "rising", "falling", "stable"
}
