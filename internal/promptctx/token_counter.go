package promptctx

import "encoding/json"

// EstimateTokens estimates the token count for a CompactContext.
// Uses a simple character-based approximation (~4 chars per token for JSON).
// For production use, replace with tiktoken-go or a model-specific tokenizer.
func EstimateTokens(cc CompactContext) int {
	data, err := json.Marshal(cc)
	if err != nil {
		return 0
	}
	// Conservative estimate: ~4 characters per token for JSON-serialized text.
	return len(data) / 4
}
