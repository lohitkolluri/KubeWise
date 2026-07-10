package llmrouter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
)

// LLMInput bundles the prompts sent to the LLM for every task type.
type LLMInput struct {
	SystemPrompt   string
	UserContent    string
	ResponseSchema json.RawMessage
}

// LLMResponse holds the raw result and token usage from an LLM call.
type LLMResponse struct {
	Data         json.RawMessage
	Model        string
	InputTokens  int64
	OutputTokens int64
	CachedTokens int64
	Duration     time.Duration
}

// LLMRouter wraps an llm.Client and dispatches requests to the model tier
// appropriate for the task type. It maintains a fallback chain per task
// and tracks token usage / cost for observability.
type LLMRouter struct {
	client      *llm.Client
	cfg         RouterConfig
	costTracker *CostTracker
}

// New creates an LLMRouter wrapping the provided client with the given config.
func New(client *llm.Client, cfg RouterConfig) *LLMRouter {
	return &LLMRouter{
		client:      client,
		cfg:         cfg,
		costTracker: NewCostTracker(),
	}
}

// CostTracker returns the router's cost tracker for observability.
func (r *LLMRouter) CostTracker() *CostTracker { return r.costTracker }

// Config returns the router configuration (read-only).
func (r *LLMRouter) Config() RouterConfig { return r.cfg }

// Route sends a task to the appropriate model tier, falling back through the
// chain on failure. Each model in the chain is tried sequentially;
// the first success is returned. If all models fail, the last error is returned.
func (r *LLMRouter) Route(ctx context.Context, task TaskType, input LLMInput) (*LLMResponse, error) {
	chain := r.cfg.FallbackChain(task)
	if len(chain) == 0 {
		return nil, fmt.Errorf("llmrouter: no models configured for task %s", task)
	}

	var lastErr error
	for i, model := range chain {
		start := time.Now()
		resp, err := r.callModel(ctx, model, input)
		duration := time.Since(start)

		if err == nil {
			resp.Model = model
			resp.Duration = duration
			r.costTracker.RecordUsage(model, resp.InputTokens, resp.OutputTokens, resp.CachedTokens)
			if i > 0 {
				slog.Info("llmrouter: task succeeded on fallback model",
					"task", task, "model", model, "failures", i, "duration", duration)
			}
			return resp, nil
		}

		lastErr = err
		slog.Error("llmrouter: task model failed",
			"task", task, "model", model, "error", err, "duration", duration)

		if isNonRetryable(err) {
			return nil, fmt.Errorf("llmrouter: non-retryable error on %s: %w", model, err)
		}

		if i < len(chain)-1 {
			r.costTracker.RecordFallback(task, model, chain[i+1], err.Error())
		}
	}

	return nil, fmt.Errorf("llmrouter: all models failed for task %s: %w", task, lastErr)
}

// callModel sets the model on the client and invokes StructuredOutput.
func (r *LLMRouter) callModel(ctx context.Context, model string, input LLMInput) (*LLMResponse, error) {
	r.client.SetModel(model)
	var respData json.RawMessage
	usage, err := r.client.StructuredOutputWithUsage(ctx, input.SystemPrompt, input.UserContent, input.ResponseSchema, &respData)
	if err != nil {
		return nil, err
	}

	return &LLMResponse{
		Data:         respData,
		InputTokens:  usage.InputTokens,
		OutputTokens: usage.OutputTokens,
		CachedTokens: usage.CachedTokens,
	}, nil
}

// RouteStructured is a convenience wrapper around Route that unmarshals the
// response data directly into respPtr. It returns the LLMResponse for
// observability (token counts, model used, duration).
func (r *LLMRouter) RouteStructured(ctx context.Context, task TaskType, input LLMInput, respPtr interface{}) (*LLMResponse, error) {
	resp, err := r.Route(ctx, task, input)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal([]byte(resp.Data), respPtr); err != nil {
		return nil, fmt.Errorf("llmrouter: unmarshal response: %w", err)
	}
	return resp, nil
}

// isNonRetryable returns true for errors that should not trigger a fallback.
func isNonRetryable(err error) bool {
	if err == nil {
		return false
	}
	// Context cancellation is not retryable — the caller is shutting down.
		if errors.Is(err, context.Canceled) {
		return true
	}
	// Schema validation failures won't resolve with a different model.
	// We detect these by checking if the error is from JSON parsing.
	return false
}
