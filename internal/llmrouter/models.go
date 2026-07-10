// Package llmrouter provides multi-model tiered routing for LLM requests.
//
// The router wraps an llm.Client and dispatches requests to the appropriate
// model tier based on task complexity: extraction/classification (T1), RCA (T2),
// code generation (T3), with a fallback (T4). Each tier maps to a specific
// OpenRouter model ID chosen for cost/quality tradeoff.
package llmrouter

import (
	"log/slog"
	"sync"
	"time"
)

// TaskType classifies how complex an LLM task is, which determines the model tier.
type TaskType int

const (
	// TaskExtraction extracts signals from raw data (T1).
	TaskExtraction TaskType = iota
	// TaskClassification classifies anomaly type (T1).
	TaskClassification
	// TaskRCA performs root cause analysis (T2).
	TaskRCA
	// TaskRemediation generates YAML/runbook remediation plans (T3).
	TaskRemediation
	// TaskValidation validates structured LLM output (T1).
	TaskValidation
)

// String returns a human-readable task name (used in metrics, logs).
func (t TaskType) String() string {
	switch t {
	case TaskExtraction:
		return "extraction"
	case TaskClassification:
		return "classification"
	case TaskRCA:
		return "rca"
	case TaskRemediation:
		return "remediation"
	case TaskValidation:
		return "validation"
	default:
		return "unknown"
	}
}

// RouterConfig holds the model ID per tier and fallback options.
type RouterConfig struct {
	ExtractionModel     string // T1: mistralai/mistral-nemo
	ClassificationModel string // T1: mistralai/mistral-nemo
	RCAModel            string // T2: deepseek/deepseek-v4-flash
	RemediationModel    string // T3: qwen/qwen3-coder (or qwen/qwen3-coder-flash)
	ValidationModel     string // T1: mistralai/mistral-nemo
	FallbackModel       string // T4: openai/gpt-oss-120b
	SessionID           string // incident ID for sticky routing via x-session-id
}

// DefaultRouterConfig returns a RouterConfig with verified OpenRouter model IDs.
// Prices as of July 2026 — see docs/V2_MIGRATION.md for cost estimates.
func DefaultRouterConfig() RouterConfig {
	return RouterConfig{
		ExtractionModel:     "mistralai/mistral-nemo",
		ClassificationModel: "mistralai/mistral-nemo",
		RCAModel:            "deepseek/deepseek-v4-flash",
		RemediationModel:    "qwen/qwen3-coder",
		ValidationModel:     "mistralai/mistral-nemo",
		FallbackModel:       "openai/gpt-oss-120b",
	}
}

// ModelForTask maps a task type to the configured model ID.
func (c RouterConfig) ModelForTask(task TaskType) string {
	switch task {
	case TaskExtraction:
		return c.ExtractionModel
	case TaskClassification:
		return c.ClassificationModel
	case TaskRCA:
		return c.RCAModel
	case TaskRemediation:
		return c.RemediationModel
	case TaskValidation:
		return c.ValidationModel
	default:
		return c.FallbackModel
	}
}

// FallbackChain returns the ordered list of models to try for a given task,
// starting with the primary model and ending with the fallback. Empty
// primary models fall back immediately to the configured FallbackModel.
func (c RouterConfig) FallbackChain(task TaskType) []string {
	primary := c.ModelForTask(task)
	if primary == "" {
		primary = c.FallbackModel
	}
	if primary == "" {
		return nil
	}
	seen := map[string]bool{primary: true}
	chain := []string{primary}
	if c.FallbackModel != "" && !seen[c.FallbackModel] {
		chain = append(chain, c.FallbackModel)
	}
	return chain
}

// FallbackRecord records a single fallback event for observability.
type FallbackRecord struct {
	Task       TaskType
	Model      string
	FallbackTo string
	Timestamp  time.Time
	Reason     string
}

// CostTracker accumulates token usage and cost estimates per model.
type CostTracker struct {
	mu sync.Mutex

	// Per-model counters keyed by model ID (e.g. "deepseek/deepseek-v4-flash").
	inputTokens  map[string]int64
	outputTokens map[string]int64
	cachedTokens map[string]int64
	fallbacks    []FallbackRecord
}

// NewCostTracker creates an empty CostTracker.
func NewCostTracker() *CostTracker {
	return &CostTracker{
		inputTokens:  make(map[string]int64),
		outputTokens: make(map[string]int64),
		cachedTokens: make(map[string]int64),
	}
}

// RecordUsage adds token counts for a model invocation.
func (ct *CostTracker) RecordUsage(model string, input, output, cached int64) {
	ct.mu.Lock()
	defer ct.mu.Unlock()
	ct.inputTokens[model] += input
	ct.outputTokens[model] += output
	ct.cachedTokens[model] += cached
}

// RecordFallback logs a fallback event.
func (ct *CostTracker) RecordFallback(task TaskType, from, to string, reason string) {
	ct.mu.Lock()
	defer ct.mu.Unlock()
	ct.fallbacks = append(ct.fallbacks, FallbackRecord{
		Task: task, Model: from, FallbackTo: to,
		Timestamp: time.Now(), Reason: reason,
	})
}

// InputTokens returns total input tokens consumed per model.
func (ct *CostTracker) InputTokens() map[string]int64 {
	ct.mu.Lock()
	defer ct.mu.Unlock()
	out := make(map[string]int64, len(ct.inputTokens))
	for k, v := range ct.inputTokens {
		out[k] = v
	}
	return out
}

// OutputTokens returns total output tokens consumed per model.
func (ct *CostTracker) OutputTokens() map[string]int64 {
	ct.mu.Lock()
	defer ct.mu.Unlock()
	out := make(map[string]int64, len(ct.outputTokens))
	for k, v := range ct.outputTokens {
		out[k] = v
	}
	return out
}

// CachedTokens returns total cached (prompt cache read) tokens per model.
func (ct *CostTracker) CachedTokens() map[string]int64 {
	ct.mu.Lock()
	defer ct.mu.Unlock()
	out := make(map[string]int64, len(ct.cachedTokens))
	for k, v := range ct.cachedTokens {
		out[k] = v
	}
	return out
}

// Fallbacks returns a copy of all fallback records.
func (ct *CostTracker) Fallbacks() []FallbackRecord {
	ct.mu.Lock()
	defer ct.mu.Unlock()
	out := make([]FallbackRecord, len(ct.fallbacks))
	copy(out, ct.fallbacks)
	return out
}

// LogFallbacks writes fallback events to the agent log.
func (ct *CostTracker) LogFallbacks() {
	ct.mu.Lock()
	defer ct.mu.Unlock()
	for _, f := range ct.fallbacks {
		slog.Info("llmrouter: fallback",
			"task", f.Task,
			"model", f.Model,
			"fallback_to", f.FallbackTo,
			"reason", f.Reason)
	}
}
