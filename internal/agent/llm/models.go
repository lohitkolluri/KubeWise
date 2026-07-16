package llm

import (
	"strings"
)

// DefaultModel is the primary OpenRouter model for remediation.
const DefaultModel = "deepseek/deepseek-v4-flash"

// FallbackModel is the universal fallback when the primary model fails.
const FallbackModel = "deepseek/deepseek-v4-flash"

// FreeModelGPTOSS is a free OpenRouter model good for local dev (131K ctx).
const FreeModelGPTOSS = "openai/gpt-oss-120b:free"

// FallbackModels are tried when the primary model times out or returns retryable errors.
var FallbackModels = []string{
	DefaultModel,
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
