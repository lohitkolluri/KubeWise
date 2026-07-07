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

// Free OpenRouter models (zero cost, rate-limited). Good for local dev with rich prompts.
const (
	FreeModelGPTOSS  = "openai/gpt-oss-120b:free"  // 131K ctx, strong reasoning
	FreeModelLaguna  = "poolside/laguna-m.1:free"    // 262K ctx, coding agent
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
