package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

// Provider is the pluggable LLM backend (OpenRouter, Ollama, etc.).
// K8sGPT-style multi-backend support keeps clusters on local or cloud models.
type Provider interface {
	Name() string
	HasAPIKey() bool
	ValidateKey(ctx context.Context) error
	SetModel(model string)
	StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error
}

// Config selects and configures an LLM provider.
type Config struct {
	Provider string // openrouter, ollama
	APIKey   string
	Model    string
	BaseURL  string // Ollama base URL (default http://127.0.0.1:11434)
}

// NewProvider returns the configured LLM backend.
func NewProvider(cfg Config) (Provider, error) {
	provider := strings.ToLower(strings.TrimSpace(cfg.Provider))
	if provider == "" {
		provider = "openrouter"
	}
	if cfg.Model == "" {
		cfg.Model = DefaultModel
	}

	switch provider {
	case "openrouter":
		return NewOpenRouterProvider(cfg.APIKey, cfg.Model), nil
	case "ollama":
		return NewOllamaProvider(cfg.BaseURL, cfg.Model, cfg.APIKey), nil
	default:
		return nil, fmt.Errorf("unknown llm provider %q (supported: openrouter, ollama)", provider)
	}
}
