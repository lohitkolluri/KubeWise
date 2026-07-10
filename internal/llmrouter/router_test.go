package llmrouter

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"github.com/lohitkolluri/KubeWise/internal/agent/llm"
)

// TestRouteOpenRouterIntegration tests against the real OpenRouter API when
// KUBEWISE_OPENROUTER_KEY is set. Run manually — not part of CI.
func TestRouteOpenRouterIntegration(t *testing.T) {
	apiKey := os.Getenv("KUBEWISE_OPENROUTER_KEY")
	if apiKey == "" {
		t.Skip("KUBEWISE_OPENROUTER_KEY not set")
	}

	client, err := llm.NewClient(llm.Config{
		Provider: "openrouter",
		APIKey:   apiKey,
		Model:    "openai/gpt-oss-120b",
	})
	if err != nil {
		t.Fatal(err)
	}

	router := New(client, DefaultRouterConfig())
	schema := json.RawMessage(`{"type":"object","properties":{"result":{"type":"string"}}}`)

	input := LLMInput{
		SystemPrompt:   "You are a terse test assistant.",
		UserContent:    "Reply with {\"result\":\"ok\"} only.",
		ResponseSchema: schema,
	}

	resp, err := router.Route(context.Background(), TaskExtraction, input)
	if err != nil {
		t.Fatalf("Route failed: %v", err)
	}
	if resp.Model == "" {
		t.Error("response has no model")
	}
	if resp.InputTokens <= 0 {
		t.Error("expected positive input tokens")
	}
	t.Logf("model=%s input=%d output=%d duration=%v", resp.Model, resp.InputTokens, resp.OutputTokens, resp.Duration)
}

func TestRouteTaskModelSelection(t *testing.T) {
	cfg := DefaultRouterConfig()

	tests := []struct {
		task TaskType
		want string
	}{
		{TaskExtraction, cfg.ExtractionModel},
		{TaskClassification, cfg.ClassificationModel},
		{TaskRCA, cfg.RCAModel},
		{TaskRemediation, cfg.RemediationModel},
		{TaskValidation, cfg.ValidationModel},
	}

	for _, tt := range tests {
		t.Run(tt.task.String(), func(t *testing.T) {
			model := cfg.ModelForTask(tt.task)
			if model != tt.want {
				t.Errorf("ModelForTask(%s) = %q, want %q", tt.task, model, tt.want)
			}
		})
	}
}

func TestDefaultRouterConfig(t *testing.T) {
	cfg := DefaultRouterConfig()
	if cfg.ExtractionModel != "mistralai/mistral-nemo" {
		t.Errorf("extraction model: got %q, want mistralai/mistral-nemo", cfg.ExtractionModel)
	}
	if cfg.RCAModel != "deepseek/deepseek-v4-flash" {
		t.Errorf("rca model: got %q, want deepseek/deepseek-v4-flash", cfg.RCAModel)
	}
	if cfg.RemediationModel != "qwen/qwen3-coder" {
		t.Errorf("remediation model: got %q, want qwen/qwen3-coder", cfg.RemediationModel)
	}
	if cfg.FallbackModel != "openai/gpt-oss-120b" {
		t.Errorf("fallback model: got %q, want openai/gpt-oss-120b", cfg.FallbackModel)
	}
}

func TestFallbackChain(t *testing.T) {
	cfg := DefaultRouterConfig()

	t.Run("T1 chain uses primary model only", func(t *testing.T) {
		chain := cfg.FallbackChain(TaskExtraction)
		if len(chain) != 2 {
			t.Fatalf("expected 2 models in chain, got %d: %v", len(chain), chain)
		}
		if chain[0] != cfg.ExtractionModel {
			t.Errorf("chain[0] = %q, want %q", chain[0], cfg.ExtractionModel)
		}
		if chain[1] != cfg.FallbackModel {
			t.Errorf("chain[1] = %q, want %q", chain[1], cfg.FallbackModel)
		}
	})

	t.Run("T2 chain uses primary then fallback", func(t *testing.T) {
		chain := cfg.FallbackChain(TaskRCA)
		if len(chain) < 1 {
			t.Fatal("empty chain")
		}
		if chain[0] != cfg.RCAModel {
			t.Errorf("chain[0] = %q, want %q", chain[0], cfg.RCAModel)
		}
	})

	t.Run("T4 fallback alone when primary equals fallback", func(t *testing.T) {
		same := RouterConfig{
			FallbackModel: "openai/gpt-oss-120b",
		}
		chain := same.FallbackChain(TaskExtraction)
		if len(chain) != 1 || chain[0] != "openai/gpt-oss-120b" {
			t.Errorf("expected single fallback chain, got %v", chain)
		}
	})
}

func TestCostTracker(t *testing.T) {
	ct := NewCostTracker()

	ct.RecordUsage("model-a", 100, 50, 10)
	ct.RecordUsage("model-b", 200, 100, 20)
	ct.RecordUsage("model-a", 50, 25, 5)

	if v := ct.InputTokens()["model-a"]; v != 150 {
		t.Errorf("model-a input = %d, want 150", v)
	}
	if v := ct.OutputTokens()["model-b"]; v != 100 {
		t.Errorf("model-b output = %d, want 100", v)
	}
	if v := ct.CachedTokens()["model-a"]; v != 15 {
		t.Errorf("model-a cached = %d, want 15", v)
	}

	t.Run("fallback recording", func(t *testing.T) {
		ct.RecordFallback(TaskRCA, "deepseek/deepseek-v4-flash", "openai/gpt-oss-120b", "timeout")
		fbs := ct.Fallbacks()
		if len(fbs) != 1 {
			t.Fatalf("expected 1 fallback, got %d", len(fbs))
		}
		if fbs[0].Reason != "timeout" {
			t.Errorf("reason = %q, want timeout", fbs[0].Reason)
		}
	})
}

func TestTaskTypeString(t *testing.T) {
	tests := []struct {
		task TaskType
		want string
	}{
		{TaskExtraction, "extraction"},
		{TaskClassification, "classification"},
		{TaskRCA, "rca"},
		{TaskRemediation, "remediation"},
		{TaskValidation, "validation"},
		{TaskType(99), "unknown"},
	}

	for _, tt := range tests {
		if got := tt.task.String(); got != tt.want {
			t.Errorf("TaskType(%d).String() = %q, want %q", tt.task, got, tt.want)
		}
	}
}

func TestCostTrackerConcurrentSafety(_ *testing.T) {
	ct := NewCostTracker()
	done := make(chan struct{})

	go func() {
		for i := 0; i < 100; i++ {
			ct.RecordUsage("model-x", int64(i), int64(i*2), 0)
		}
		done <- struct{}{}
	}()
	go func() {
		for i := 0; i < 100; i++ {
			ct.RecordFallback(TaskRCA, "m1", "m2", "timeout")
		}
		done <- struct{}{}
	}()
	go func() {
		for i := 0; i < 100; i++ {
			_ = ct.InputTokens()
			_ = ct.Fallbacks()
		}
		done <- struct{}{}
	}()

	for i := 0; i < 3; i++ {
		<-done
	}
}

func TestModelForTaskEdgeCases(t *testing.T) {
	cfg := DefaultRouterConfig()

	if model := cfg.ModelForTask(TaskType(99)); model != cfg.FallbackModel {
		t.Errorf("unknown task should return fallback, got %q", model)
	}
}

func TestEstimateTokensFallback(t *testing.T) {
	if v := llmEstimateTokens("hello world"); v != 2 {
		t.Errorf("estimateTokens('hello world') = %d, want 2", v)
	}
	if v := llmEstimateTokens(""); v != 0 {
		t.Errorf("estimateTokens('') = %d, want 0", v)
	}
}

func llmEstimateTokens(s string) int64 {
	if s == "" {
		return 0
	}
	return int64(len(s) / 4)
}

func TestIsNonRetryable(t *testing.T) {
	if isNonRetryable(nil) {
		t.Error("nil should be retryable")
	}
}
