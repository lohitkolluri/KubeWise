package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/cenkalti/backoff/v6"
	gobreaker "github.com/sony/gobreaker/v2"
)

// OpenAICompatibleProvider implements an OpenAPI/OpenAI-compatible Chat Completions backend.
// It aims for broad compatibility (OpenAI, vLLM, LM Studio, OpenAI-gateway proxies, etc.).
type OpenAICompatibleProvider struct {
	baseURL        string
	apiKey         string
	model          string
	client         *http.Client
	circuitBreaker *gobreaker.TwoStepCircuitBreaker[any]
}

// NewOpenAICompatibleProvider creates an OpenAI-compatible Chat Completions backend.
func NewOpenAICompatibleProvider(baseURL, model, apiKey string) *OpenAICompatibleProvider {
	baseURL = strings.TrimSpace(baseURL)
	if baseURL == "" {
		baseURL = "https://api.openai.com"
	}
	baseURL = strings.TrimRight(baseURL, "/")
	if model == "" {
		model = DefaultModel
	}
	apiKey = strings.TrimSpace(apiKey)
	return &OpenAICompatibleProvider{
		baseURL: baseURL,
		apiKey:  apiKey,
		model:   model,
		client:  &http.Client{Timeout: 120 * time.Second},
		circuitBreaker: gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
			Name:        "llm-circuit-breaker",
			MaxRequests: 1,
			Interval:    0,
			Timeout:     30 * time.Second,
			ReadyToTrip: func(counts gobreaker.Counts) bool {
				return counts.ConsecutiveFailures >= 3
			},
		}),
	}
}

// Name returns the config provider key ("openapi").
// Note: "openai" is accepted as an alias at config parsing time.
func (p *OpenAICompatibleProvider) Name() string { return "openapi" }

// HasAPIKey returns true when an API key is configured.
func (p *OpenAICompatibleProvider) HasAPIKey() bool { return p.apiKey != "" }

// SetModel changes the model used for subsequent requests.
func (p *OpenAICompatibleProvider) SetModel(model string) {
	if strings.TrimSpace(model) != "" {
		p.model = model
	}
}

// ValidateKey checks that the API key is valid against the provider's endpoints.
func (p *OpenAICompatibleProvider) ValidateKey(ctx context.Context) error {
	if p.apiKey == "" {
		return fmt.Errorf("openapi-compatible API key is empty")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.baseURL+"/v1/models", nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+p.apiKey)
	req.Header.Set("Accept", "application/json")
	resp, err := p.client.Do(req)
	if err != nil {
		return fmt.Errorf("openapi-compatible auth failed: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return fmt.Errorf("openapi-compatible auth failed: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return nil
}

type openAIChatReq struct {
	Model          string        `json:"model"`
	Messages       []openAIMsg   `json:"messages"`
	Temperature    float64       `json:"temperature,omitempty"`
	ResponseFormat *openAIFormat `json:"response_format,omitempty"`
}

type openAIFormat struct {
	Type string `json:"type"`
}

type openAIMsg struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type openAIChatResp struct {
	Choices []struct {
		Message openAIMsg `json:"message"`
	} `json:"choices"`
	Usage *struct {
		PromptTokens     int64 `json:"prompt_tokens"`
		CompletionTokens int64 `json:"completion_tokens"`
		TotalTokens      int64 `json:"total_tokens"`
	} `json:"usage"`
}

// StructuredOutput sends a prompt and returns a typed JSON response.
func (p *OpenAICompatibleProvider) StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error {
	_, err := p.StructuredOutputWithUsage(ctx, systemPrompt, userContent, schema, respPtr)
	return err
}

// StructuredOutputWithUsage sends a prompt and returns a typed response with token usage.
func (p *OpenAICompatibleProvider) StructuredOutputWithUsage(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) (Usage, error) {
	if p.apiKey == "" {
		return Usage{}, fmt.Errorf("openapi-compatible API key is empty")
	}
	if p.model == "" {
		return Usage{}, fmt.Errorf("openapi-compatible model is empty")
	}

	cbDone, err := p.circuitBreaker.Allow()
	if err != nil {
		return Usage{}, fmt.Errorf("openapi-compatible circuit breaker open")
	}

	// Broadest compatibility: request JSON object output, and also embed the JSON schema in the system prompt.
	// Many providers ignore response_format; the prompt constraint is the true enforcement layer.
	schemaHint := ""
	if len(schema) > 0 {
		schemaHint = "\n\nReturn ONLY valid JSON that matches this JSON Schema:\n" + string(schema)
	}

	// Exponential backoff for retry (max 3 attempts = 1 initial + 2 retries)
	b := backoff.NewExponentialBackOff()
	b.MaxInterval = 5 * time.Second

	var usage Usage
	_, retryErr := backoff.Retry(ctx, func() (struct{}, error) {
		reqBody := openAIChatReq{
			Model: p.model,
			Messages: []openAIMsg{
				{Role: "system", Content: systemPrompt + schemaHint},
				{Role: "user", Content: userContent},
			},
			Temperature:    0.2,
			ResponseFormat: &openAIFormat{Type: "json_object"},
		}
		b, err := json.Marshal(reqBody)
		if err != nil {
			return struct{}{}, backoff.Permanent(err)
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.baseURL+"/v1/chat/completions", bytes.NewReader(b))
		if err != nil {
			return struct{}{}, backoff.Permanent(err)
		}
		req.Header.Set("Authorization", "Bearer "+p.apiKey)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/json")

		resp, err := p.client.Do(req)
		if err != nil {
			return struct{}{}, err
		}
		defer func() { _ = resp.Body.Close() }()

		body, err := io.ReadAll(io.LimitReader(resp.Body, 10<<20))
		if err != nil {
			return struct{}{}, err
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return struct{}{}, fmt.Errorf("chat completion failed: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
		}

		var out openAIChatResp
		if err := json.Unmarshal(body, &out); err != nil {
			return struct{}{}, err
		}
		if len(out.Choices) == 0 || strings.TrimSpace(out.Choices[0].Message.Content) == "" {
			return struct{}{}, fmt.Errorf("openapi-compatible: empty message content")
		}

		content := strings.TrimSpace(out.Choices[0].Message.Content)
		content = strings.TrimPrefix(content, "```json")
		content = strings.TrimPrefix(content, "```")
		content = strings.TrimSuffix(content, "```")
		content = strings.TrimSpace(content)

		if err := json.Unmarshal([]byte(content), respPtr); err != nil {
			return struct{}{}, fmt.Errorf("parse structured output: %w (raw: %s)", err, content)
		}

		usage = estimateUsage(systemPrompt, userContent, respPtr)
		if out.Usage != nil {
			usage.InputTokens = out.Usage.PromptTokens
			usage.OutputTokens = out.Usage.CompletionTokens
		}
		return struct{}{}, nil
	}, backoff.WithMaxTries(3))

	if retryErr != nil {
		cbDone(retryErr)
		return Usage{}, retryErr
	}
	cbDone(nil)
	return usage, nil
}
