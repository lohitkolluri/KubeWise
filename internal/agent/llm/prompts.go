package llm

import (
	"fmt"
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

You can recommend these remediation actions:
- restart_pod: Restart a single pod. Low risk, fast recovery.
- scale_replicas: Scale a deployment up or down. Use for resource pressure or redundancy.
- rollback_deployment: Roll back a deployment to a previous revision. For bad rollouts.
- patch_resources: Adjust resource requests/limits. For OOM or CPU throttling.
- delete_pod: Force-delete a stuck pod. Only for pods stuck in Pending/CrashLoopBackOff.
- escalate: Raise to a human operator. Use when confidence is low or risk is high.
- noop: Take no action. Use for transient spikes, benign anomalies, or insufficient data.

CONSTRAINTS
1. Only operate in namespaces explicitly listed in the context. Never touch kube-system, kube-public, or kube-node-lease.
2. Prefer the smallest, most reversible change that addresses the root cause.
3. If confidence is below 0.7, recommend escalate instead of guessing an action.
4. Never recommend deleting Deployments, StatefulSets, Services, Namespaces, or CRDs.
5. noop is always a safe option — prefer it for transient metrics spikes or single-sample anomalies.
6. Always include specific, quantified evidence. "High CPU" is insufficient — use "CPU throttled 45% over 5m" or similar.

FORMAT
Return ONLY a valid JSON object matching the provided schema. No preamble, no explanation, no markdown formatting.`
}

// BuildUserPrompt constructs the user prompt from anomaly records and recent metrics.
func BuildUserPrompt(anomalies []models.AnomalyRecord, metricsSummary string) string {
	var b strings.Builder

	b.WriteString("Analyze the following Kubernetes cluster anomalies and produce a remediation plan.\n\n")

	b.WriteString("## Current Anomalies\n\n")
	if len(anomalies) == 0 {
		b.WriteString("No active anomalies detected.\n")
	} else {
		for i, a := range anomalies {
			b.WriteString(fmt.Sprintf("%d. Entity: %s | Namespace: %s | Metric: %s | Score: %.2f | Pattern: %s | Status: %s\n",
				i+1, sanitizeField(a.Entity), sanitizeField(a.Namespace), sanitizeField(a.MetricName),
				a.Score, sanitizeField(a.Pattern), sanitizeField(a.Status)))
		}
	}

	if metricsSummary != "" {
		b.WriteString("\n## Recent Metric Context\n\n")
		b.WriteString(metricsSummary)
	}

	b.WriteString("\n\nProduce a remediation plan in the specified JSON format. Base your diagnosis on the evidence above.")
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
		b.WriteString(fmt.Sprintf("- %s: %d samples, last value %.2f (trend: %s)\n",
			s.Name, s.SampleCount, s.LastValue, s.Trend))
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
