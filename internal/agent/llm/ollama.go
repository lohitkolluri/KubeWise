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

const defaultOllamaBaseURL = "http://127.0.0.1:11434"

// OllamaProvider calls a local or in-cluster Ollama server.
type OllamaProvider struct {
	baseURL        string
	model          string
	apiKey         string
	httpClient     *http.Client
	circuitBreaker *gobreaker.TwoStepCircuitBreaker[any]
}

// NewOllamaProvider creates an Ollama backend for air-gapped / local inference.
func NewOllamaProvider(baseURL, model, apiKey string) *OllamaProvider {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL == "" {
		baseURL = defaultOllamaBaseURL
	}
	if model == "" {
		model = "llama3.1:8b"
	}
	return &OllamaProvider{
		baseURL: baseURL,
		model:   model,
		apiKey:  strings.TrimSpace(apiKey),
		circuitBreaker: gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
			Name:        "llm-circuit-breaker",
			MaxRequests: 1,
			Interval:    0,
			Timeout:     30 * time.Second,
			ReadyToTrip: func(counts gobreaker.Counts) bool {
				return counts.ConsecutiveFailures >= 3
			},
		}),
		httpClient: &http.Client{
			Timeout: 120 * time.Second,
		},
	}
}

// Name returns "ollama" as the provider identifier.
func (p *OllamaProvider) Name() string { return "ollama" }

// SetModel changes the model used for subsequent requests.
func (p *OllamaProvider) SetModel(model string) { p.model = model }

// HasAPIKey is true when a reachable base URL is configured (Ollama often needs no key).
func (p *OllamaProvider) HasAPIKey() bool { return p.baseURL != "" }

// ValidateKey checks that the Ollama server is reachable and responding.
func (p *OllamaProvider) ValidateKey(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.baseURL+"/api/tags", nil)
	if err != nil {
		return err
	}
	p.setAuth(req)
	resp, err := p.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("ollama unreachable at %s: %w", p.baseURL, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("ollama tags: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return nil
}

// StructuredOutput sends a prompt and returns a typed JSON response.
func (p *OllamaProvider) StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error {
	_, err := p.StructuredOutputWithUsage(ctx, systemPrompt, userContent, schema, respPtr)
	return err
}

// StructuredOutputWithUsage sends a prompt and returns a typed response with token usage.
func (p *OllamaProvider) StructuredOutputWithUsage(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) (Usage, error) {
	cbDone, err := p.circuitBreaker.Allow()
	if err != nil {
		return Usage{}, fmt.Errorf("ollama circuit breaker open")
	}

	var schemaMap map[string]any
	if err := json.Unmarshal(schema, &schemaMap); err != nil {
		return Usage{}, fmt.Errorf("parse schema: %w", err)
	}

	// Exponential backoff for retry (max 3 attempts = 1 initial + 2 retries)
	b := backoff.NewExponentialBackOff()
	b.MaxInterval = 5 * time.Second

	var usage Usage
	_, retryErr := backoff.Retry(ctx, func() (struct{}, error) {
		payload := map[string]any{
			"model":  p.model,
			"stream": false,
			"format": schemaMap,
			"messages": []map[string]string{
				{"role": "system", "content": systemPrompt},
				{"role": "user", "content": userContent},
			},
			"options": map[string]any{
				"temperature": 0.2,
			},
		}
		body, err := json.Marshal(payload)
		if err != nil {
			return struct{}{}, backoff.Permanent(err)
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.baseURL+"/api/chat", bytes.NewReader(body))
		if err != nil {
			return struct{}{}, backoff.Permanent(err)
		}
		req.Header.Set("Content-Type", "application/json")
		p.setAuth(req)

		resp, err := p.httpClient.Do(req)
		if err != nil {
			return struct{}{}, err
		}
		defer func() { _ = resp.Body.Close() }()

		respBody, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		if err != nil {
			return struct{}{}, err
		}
		if resp.StatusCode >= 400 {
			return struct{}{}, fmt.Errorf("ollama chat: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
		}

		var chatResp struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
			PromptEvalCount int64 `json:"prompt_eval_count"`
			EvalCount       int64 `json:"eval_count"`
		}
		if err := json.Unmarshal(respBody, &chatResp); err != nil {
			return struct{}{}, err
		}
		content := strings.TrimSpace(chatResp.Message.Content)
		if content == "" {
			return struct{}{}, fmt.Errorf("ollama: empty message content")
		}
		if err := json.Unmarshal([]byte(content), respPtr); err != nil {
			if jsonBody := extractJSONObject(content); jsonBody != "" {
				if err2 := json.Unmarshal([]byte(jsonBody), respPtr); err2 == nil {
					usage = Usage{InputTokens: chatResp.PromptEvalCount, OutputTokens: chatResp.EvalCount}
					return struct{}{}, nil
				}
			}
			return struct{}{}, fmt.Errorf("parse structured output: %w (raw: %s)", err, content)
		}
		usage = Usage{InputTokens: chatResp.PromptEvalCount, OutputTokens: chatResp.EvalCount}
		if usage.InputTokens == 0 && usage.OutputTokens == 0 {
			usage = estimateUsage(systemPrompt, userContent, respPtr)
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

func (p *OllamaProvider) setAuth(req *http.Request) {
	if p.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+p.apiKey)
	}
}
