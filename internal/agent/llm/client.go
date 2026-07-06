package llm

import (
	"context"
	"encoding/json"
	"fmt"
)

const (
	defaultModel = "meta-llama/llama-3.1-8b-instruct"
	appReferer   = "https://github.com/lohitkolluri/KubeWise"
	appTitle     = "KubeWise"
)

// Client is the LLM facade used by the remediation correlator.
type Client struct {
	provider Provider
}

// NewClient creates an LLM client from provider configuration.
func NewClient(cfg Config) (*Client, error) {
	p, err := NewProvider(cfg)
	if err != nil {
		return nil, err
	}
	return &Client{provider: p}, nil
}

// NewOpenRouterClient is a convenience wrapper for OpenRouter-only setups.
func NewOpenRouterClient(apiKey, model string) (*Client, error) {
	return NewClient(Config{Provider: "openrouter", APIKey: apiKey, Model: model})
}

// ProviderName returns the active backend name.
func (c *Client) ProviderName() string {
	if c == nil || c.provider == nil {
		return ""
	}
	return c.provider.Name()
}

// ValidateKey checks provider credentials / reachability.
func (c *Client) ValidateKey(ctx context.Context) error {
	if c == nil || c.provider == nil {
		return fmt.Errorf("llm client not configured")
	}
	return c.provider.ValidateKey(ctx)
}

// HasAPIKey reports whether the provider is configured.
func (c *Client) HasAPIKey() bool {
	return c != nil && c.provider != nil && c.provider.HasAPIKey()
}

// StructuredOutput sends a schema-constrained request and unmarshals into respPtr.
func (c *Client) StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error {
	if c == nil || c.provider == nil {
		return fmt.Errorf("llm client not configured")
	}
	return c.provider.StructuredOutput(ctx, systemPrompt, userContent, schema, respPtr)
}
