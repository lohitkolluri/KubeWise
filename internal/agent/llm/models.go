package llm

import (
	"strings"
)

// DefaultModel is the primary OpenRouter model for remediation.
// Preference: avoid :free tier models due to rate limits.
const DefaultModel = ModelGPTOSS

// Paid OpenRouter models.
const (
	ModelGPTOSS = "openai/gpt-oss-120b"
)

// Tier-specific model constants for the LLM router (Phase 1.5).
// These are the recommended production model IDs as of July 2026.
const (
	// T1: extraction / classification — cheap and fast.
	ExtractionModel     = "mistralai/mistral-nemo"
	ClassificationModel = "mistralai/mistral-nemo"

	// T2: root cause analysis — strong reasoning at moderate cost.
	RCAModel = "deepseek/deepseek-v4-flash"

	// T3: code / YAML generation — best at structured output.
	RemediationModel = "qwen/qwen3-coder"

	// T4: universal fallback when all other tiers fail.
	FallbackModel = "openai/gpt-oss-120b"
)

// Free OpenRouter models (zero cost, rate-limited). Good for local dev with rich prompts.
const (
	FreeModelGPTOSS = "openai/gpt-oss-120b:free" // 131K ctx, strong reasoning
	FreeModelLaguna = "poolside/laguna-m.1:free" // 262K ctx, coding agent
)

// DevDefaultModel is the recommended model for kind/local dev.
// (User preference: use non-:free GPT-OSS for reliability.)
const DevDefaultModel = ModelGPTOSS

// FallbackModels are tried when the primary model times out or returns retryable errors.
var FallbackModels = []string{
	ModelGPTOSS,
}

// IsFreeModel reports OpenRouter :free tier models (relaxed JSON schema, higher token budget).
func IsFreeModel(model string) bool {
	return strings.Contains(model, ":free")
}

// modelChain returns primary first, then fallbacks (no duplicates).
func modelChain(primary string) []string {
	if primary == "" {
		primary = DefaultModel
	}
	seen := map[string]bool{primary: true}
	out := []string{primary}
	for _, m := range FallbackModels {
		if !seen[m] {
			seen[m] = true
			out = append(out, m)
		}
	}
	return out
}
