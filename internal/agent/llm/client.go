package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
)

const (
	appReferer = "https://github.com/lohitkolluri/KubeWise"
	appTitle   = "KubeWise"
)

// Client is the LLM facade used by the remediation correlator.
type Client struct {
	provider Provider
	mu       sync.Mutex
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

// SetModel changes the model used by the underlying provider.
// This enables the LLM router to switch models without recreating the client.
func (c *Client) SetModel(model string) {
	if c != nil && c.provider != nil {
		c.mu.Lock()
		c.provider.SetModel(model)
		c.mu.Unlock()
	}
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
	_, err := c.StructuredOutputWithUsage(ctx, systemPrompt, userContent, schema, respPtr)
	return err
}

// StructuredOutputWithUsage sends a schema-constrained request and returns provider-reported
// token usage when available, falling back to a character-based estimate otherwise.
func (c *Client) StructuredOutputWithUsage(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) (Usage, error) {
	if c == nil || c.provider == nil {
		return Usage{}, fmt.Errorf("llm client not configured")
	}
	if up, ok := c.provider.(usageProvider); ok {
		return up.StructuredOutputWithUsage(ctx, systemPrompt, userContent, schema, respPtr)
	}
	if err := c.provider.StructuredOutput(ctx, systemPrompt, userContent, schema, respPtr); err != nil {
		return Usage{}, err
	}
	return estimateUsage(systemPrompt, userContent, respPtr), nil
}
