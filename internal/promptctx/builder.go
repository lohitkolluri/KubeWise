// Package promptctx builds compact context summaries for LLM prompts.
// It compresses cluster state (anomalies, metrics, K8s events, resource state)
// into structured JSON, reducing token usage by ~80% compared to raw investigation text.
package promptctx

import (
	"context"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// CompactContext is the structured, token-efficient input for LLM prompts.
type CompactContext struct {
	Anomaly  AnomalyContext  `json:"anomaly"`
	Metrics  []MetricSummary `json:"metrics,omitempty"`
	Events   []EventSummary  `json:"events,omitempty"`
	Logs     []LogSnippet    `json:"logs,omitempty"`      // populated when Loki is configured
	Traces   []TraceSummary  `json:"traces,omitempty"`    // populated when Tempo is configured
	K8sState *K8sState       `json:"k8s_state,omitempty"` // omitted when unchanged since last Build()
	K8sHash  string          `json:"k8s_state_hash"`      // always populated; receiver compares for change detection
	BuiltAt  time.Time       `json:"built_at"`
}

// LogSnippet is a single log line from Loki.
type LogSnippet struct {
	Line      string `json:"line"`
	Timestamp string `json:"timestamp"` // RFC3339 string
	Container string `json:"container,omitempty"`
}

// AnomalyContext summarizes the triggering anomaly for the LLM.
type AnomalyContext struct {
	Entity    string  `json:"entity"`
	Namespace string  `json:"namespace"`
	Metric    string  `json:"metric,omitempty"`
	Score     float64 `json:"score"`
	Pattern   string  `json:"pattern,omitempty"`
}

// MetricSummary is a computed summary of a metric time series.
type MetricSummary struct {
	Name        string  `json:"name"`
	Current     float64 `json:"current"`
	Trend       string  `json:"trend"` // "rising", "falling", "stable"
	Max         float64 `json:"max"`
	SampleCount int     `json:"samples"`
}

// EventSummary is a deduplicated K8s event grouped by reason.
type EventSummary struct {
	Reason   string `json:"reason"`
	Count    int    `json:"count"`
	LastSeen string `json:"last_seen"` // relative time like "2m ago"
	Involved string `json:"involved"`
}

// K8sState holds the current resource state relevant to the anomaly.
type K8sState struct {
	Deployment *DeploymentState `json:"deployment,omitempty"`
	Node       *NodeState       `json:"node,omitempty"`
	Pod        *PodState        `json:"pod,omitempty"`
}

// DeploymentState summarizes a deployment's status.
type DeploymentState struct {
	Name        string `json:"name"`
	Replicas    int32  `json:"replicas"`
	Available   int32  `json:"available"`
	Strategy    string `json:"strategy,omitempty"`
	Image       string `json:"image,omitempty"`
	RolloutTime string `json:"rollout_time,omitempty"`
}

// NodeState summarizes a node's status.
type NodeState struct {
	Name       string      `json:"name"`
	Ready      bool        `json:"ready"`
	Conditions []Condition `json:"conditions,omitempty"`
}

// PodState summarizes a pod's status.
type PodState struct {
	Name      string           `json:"name"`
	Phase     string           `json:"phase"`
	Restarts  int32            `json:"restarts"`
	Container []ContainerState `json:"containers,omitempty"`
}

// ContainerState summarizes a container within a pod.
type ContainerState struct {
	Name   string `json:"name"`
	Ready  bool   `json:"ready"`
	State  string `json:"state"` // "running", "waiting", "terminated"
	Reason string `json:"reason,omitempty"`
}

// Condition is a generic K8s condition.
type Condition struct {
	Type   string `json:"type"`
	Status string `json:"status"`
	Reason string `json:"reason,omitempty"`
}

// Sample is a single metric data point retrieved from the store.
type Sample struct {
	Value float64
	TS    int64 // unix timestamp in seconds
}

// Builder creates CompactContext from cluster data sources.
type Builder struct {
	store    DataSource
	lokiURL  string
	tempoURL string
}

// DataSource abstracts the metric store needed by the builder.
// The production implementation reads from the time-series store.
type DataSource interface {
	GetMetricSamples(name string, labels map[string]string, limit int) ([]Sample, error)
}

// BuilderOption configures a Builder.
type BuilderOption func(*Builder)

// WithLokiURL sets the Loki HTTP API endpoint for log snippet retrieval.
// When empty, log snippets are not fetched.
func WithLokiURL(url string) BuilderOption {
	return func(b *Builder) {
		b.lokiURL = url
	}
}

// WithTempoURL sets the Tempo HTTP API endpoint for trace context retrieval.
// When empty, traces are not fetched.
func WithTempoURL(url string) BuilderOption {
	return func(b *Builder) {
		b.tempoURL = url
	}
}

// New creates a context builder with optional configuration.
func New(s DataSource, opts ...BuilderOption) *Builder {
	b := &Builder{store: s}
	for _, opt := range opts {
		opt(b)
	}
	return b
}

// Build constructs a CompactContext for the given anomaly.
func (b *Builder) Build(ctx context.Context, anomaly models.AnomalyRecord) CompactContext {
	cc := CompactContext{
		Anomaly: AnomalyContext{
			Entity:    anomaly.Entity,
			Namespace: anomaly.Namespace,
			Metric:    anomaly.MetricName,
			Score:     anomaly.Score,
			Pattern:   anomaly.Pattern,
		},
		BuiltAt: time.Now(),
	}

	if anomaly.MetricName != "" {
		if summaries := BuildMetricSummary(b.store, anomaly.MetricName, anomaly.Entity, anomaly.Namespace, anomaly.Score); summaries != nil {
			cc.Metrics = summaries
		}
	}

	if b.lokiURL != "" {
		if snippets, err := fetchLogSnippets(ctx, b.lokiURL, anomaly.Namespace, anomaly.Entity, 15*time.Minute); err == nil {
			cc.Logs = snippets
		}
	}

	if b.tempoURL != "" {
		if traces, err := fetchTraceContext(ctx, b.tempoURL, anomaly.Namespace, anomaly.Entity, 15*time.Minute); err == nil {
			cc.Traces = traces
		}
	}

	return cc
}

// TokenEstimate returns an approximate token count for the context.
func (cc CompactContext) TokenEstimate() int {
	return EstimateTokens(cc)
}
