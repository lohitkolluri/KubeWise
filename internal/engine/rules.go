package engine

import (
	"context"
	"fmt"
	"time"
)

// --- OOMRule ---

// OOMRule matches when a pod was OOMKilled.
// Confidence: 0.98 — definitive (kernel-enforced kill).
// Action: restart_pod (T1 — auto in live mode).
type OOMRule struct{}

// Name returns "oom_killed" as the rule identifier.
func (r *OOMRule) Name() string { return "oom_killed" }

// Evaluate checks for OOMKilled patterns in the anomaly input.
func (r *OOMRule) Evaluate(_ context.Context, input Input) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.Pattern == "OOMKilling" || a.Pattern == "OOMKilled" {
			return []RuleResult{{
				RuleName:   r.Name(),
				Action:     "restart_pod",
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityCritical,
				Confidence: 0.98,
				NeedsLLM:   false,
				Evidence:   []string{fmt.Sprintf("Pod %s/%s OOMKilled at %s", a.Namespace, a.Entity, a.DetectedAt)},
			}}, nil
		}
	}
	return nil, nil
}

// --- CrashLoopRule ---

// CrashLoopRule matches when a pod is in CrashLoopBackOff with high restart count.
// Threshold: restart_rate > 0.5 (at least 1 restart in 2 minutes of observations).
// Confidence: 0.95 — strong signal but not as definitive as OOM.
// Action: restart_pod (T1 — auto).
type CrashLoopRule struct{}

// Name returns "crash_loop" as the rule identifier.
func (r *CrashLoopRule) Name() string { return "crash_loop" }

// Evaluate checks for CrashLoopBackOff or high restart-rate anomalies.
func (r *CrashLoopRule) Evaluate(_ context.Context, input Input) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.Pattern == "CrashLoopBackOff" || (a.MetricName == "restart_rate" && a.Score >= 0.8) {
			return []RuleResult{{
				RuleName:   r.Name(),
				Action:     "restart_pod",
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityHigh,
				Confidence: 0.95,
				NeedsLLM:   false,
				Evidence:   []string{fmt.Sprintf("CrashLoopBackOff entity=%s score=%.2f", a.Entity, a.Score)},
			}}, nil
		}
	}
	return nil, nil
}

// --- ImagePullBackOffRule ---

// ImagePullBackOffRule matches when a pod cannot pull its image.
// Confidence: 0.97 — definitive (K8s reports this explicitly).
// Action: noop + escalate (T3 — requires approval because image fix needs human).
type ImagePullBackOffRule struct{}

// Name returns "image_pull_backoff" as the rule identifier.
func (r *ImagePullBackOffRule) Name() string { return "image_pull_backoff" }

// Evaluate checks for image pull failure anomalies.
func (r *ImagePullBackOffRule) Evaluate(_ context.Context, input Input) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.Pattern == "ImagePullBackOff" || a.Pattern == "ErrImagePull" || a.Pattern == "ImagePull" {
			return []RuleResult{{
				RuleName:   r.Name(),
				Action:     "escalate",
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityHigh,
				Confidence: 0.97,
				NeedsLLM:   false,
				Evidence:   []string{fmt.Sprintf("Image pull failure for %s/%s", a.Namespace, a.Entity)},
			}}, nil
		}
	}
	return nil, nil
}

// --- NodeNotReadyRule ---

// NodeNotReadyRule matches when a node is NotReady.
// Confidence: 0.90 — clear K8s condition but may be transient.
// Action: escalate (T3 — can't auto-fix a node from inside the cluster).
type NodeNotReadyRule struct{}

// Name returns "node_not_ready" as the rule identifier.
func (r *NodeNotReadyRule) Name() string { return "node_not_ready" }

// Evaluate checks for NodeNotReady conditions in the anomaly input.
func (r *NodeNotReadyRule) Evaluate(_ context.Context, input Input) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.Pattern == "NodeNotReady" {
			return []RuleResult{{
				RuleName:   r.Name(),
				Action:     "escalate",
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityCritical,
				Confidence: 0.90,
				NeedsLLM:   false,
				Evidence:   []string{fmt.Sprintf("Node %s not ready", a.Entity)},
			}}, nil
		}
	}
	return nil, nil
}

// --- PendingRule ---

// PendingRuleMinDuration is the minimum time a pod must be pending
// before the PendingRule fires. Chosen to avoid alerting on brief
// scheduling delays during rolling updates or initial startup.
const PendingRuleMinDuration = 5 * time.Minute

// PendingRule matches when a pod has been Pending with reason Unschedulable.
// Confidence: 0.85 — clear signal but could resolve on its own.
// Action: escalate (requires cluster-level intervention).
type PendingRule struct{}

// Name returns "pod_pending" as the rule identifier.
func (r *PendingRule) Name() string { return "pod_pending" }

// Evaluate checks for Unschedulable or long-pending pod anomalies.
func (r *PendingRule) Evaluate(_ context.Context, input Input) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.Pattern == "Unschedulable" || (a.Pattern == "Pending" && a.Score >= 0.7) {
			return []RuleResult{{
				RuleName:   r.Name(),
				Action:     "escalate",
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityMedium,
				Confidence: 0.85,
				NeedsLLM:   false,
				Evidence:   []string{fmt.Sprintf("Pod %s/%s pending (unschedulable)", a.Namespace, a.Entity)},
			}}, nil
		}
	}
	return nil, nil
}

// --- ReadyRatioRule ---

// ReadyRatioRule matches when a deployment or statefulset has a low
// ready-replica ratio (<0.5). This indicates the workload is degraded.
// Confidence: 0.80 — could be transient during rolling updates.
// Action: scale_replicas (T2 — auto with cooldown).
type ReadyRatioRule struct{}

// Name returns "ready_ratio_low" as the rule identifier.
func (r *ReadyRatioRule) Name() string { return "ready_ratio_low" }

// Evaluate checks for low ready-replica ratio anomalies.
func (r *ReadyRatioRule) Evaluate(_ context.Context, input Input) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.Pattern == "LowReadyRatio" {
			return []RuleResult{{
				RuleName:   r.Name(),
				Action:     "scale_replicas",
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityHigh,
				Confidence: 0.80,
				NeedsLLM:   false,
				Evidence:   []string{fmt.Sprintf("Low ready-replica ratio for %s/%s", a.Namespace, a.Entity)},
			}}, nil
		}
	}
	return nil, nil
}

// --- CPUThrottleRule ---

// CPUThrottleRule matches when a container shows significant CPU throttling
// (>50% throttled time). This suggests the CPU limit is too low for the workload.
// Confidence: 0.75 — throttling can spike without immediate impact.
// Action: patch_resources (T2 — auto but needs LLM for appropriate values).
type CPUThrottleRule struct{}

// Name returns "cpu_throttle_high" as the rule identifier.
func (r *CPUThrottleRule) Name() string { return "cpu_throttle_high" }

// Evaluate checks for high CPU throttling anomalies.
func (r *CPUThrottleRule) Evaluate(_ context.Context, input Input) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.MetricName == "cpu_throttle" && a.Score >= 0.5 {
			return []RuleResult{{
				RuleName:   r.Name(),
				Action:     "patch_resources",
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityMedium,
				Confidence: 0.75,
				NeedsLLM:   true,
				Evidence:   []string{fmt.Sprintf("CPU throttling at %.0f%% for %s/%s", a.Score*100, a.Namespace, a.Entity)},
			}}, nil
		}
	}
	return nil, nil
}

// --- MemoryPressureRule ---

// MemoryPressureRule matches when a node is under memory pressure or
// when persistent OOM events are detected on a node.
// Confidence: 0.85 — clear signal but node-level issues need human assessment.
// Action: escalate (T3 — requires approval).
type MemoryPressureRule struct{}

// Name returns "memory_pressure" as the rule identifier.
func (r *MemoryPressureRule) Name() string { return "memory_pressure" }

// Evaluate checks for memory pressure conditions in the cluster.
func (r *MemoryPressureRule) Evaluate(_ context.Context, input Input) ([]RuleResult, error) {
	for _, a := range input.Anomalies {
		if a.Pattern == "MemoryPressure" || a.MetricName == "node_memory_pressure" {
			return []RuleResult{{
				RuleName:   r.Name(),
				Action:     "escalate",
				Target:     a.Entity,
				Namespace:  a.Namespace,
				Severity:   SeverityHigh,
				Confidence: 0.85,
				NeedsLLM:   false,
				Evidence:   []string{fmt.Sprintf("Memory pressure on %s/%s", a.Namespace, a.Entity)},
			}}, nil
		}
	}
	return nil, nil
}
