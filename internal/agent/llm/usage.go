package llm

import (
	"context"
	"encoding/json"
)

// Usage reports token consumption from an LLM completion.
type Usage struct {
	InputTokens  int64
	OutputTokens int64
	CachedTokens int64
}

// usageProvider is implemented by backends that return real token counts.
type usageProvider interface {
	StructuredOutputWithUsage(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) (Usage, error)
}

// estimateUsage approximates token counts when the provider does not report usage.
func estimateUsage(systemPrompt, userContent string, respPtr interface{}) Usage {
	var outBytes []byte
	if respPtr != nil {
		outBytes, _ = json.Marshal(respPtr)
	}
	return Usage{
		InputTokens:  estimateTokens(systemPrompt + userContent),
		OutputTokens: estimateTokens(string(outBytes)),
	}
}

func estimateTokens(s string) int64 {
	if s == "" {
		return 0
	}
	return int64(len(s) / 4)
}
