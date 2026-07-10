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
)

const defaultOllamaBaseURL = "http://127.0.0.1:11434"

// OllamaProvider calls a local or in-cluster Ollama server.
type OllamaProvider struct {
	baseURL        string
	model          string
	apiKey         string
	httpClient     *http.Client
	circuitBreaker *CircuitBreaker
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
		baseURL:        baseURL,
		model:          model,
		apiKey:         strings.TrimSpace(apiKey),
		circuitBreaker: newCircuitBreaker(3, 30*time.Second),
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
	if !p.circuitBreaker.Allow() {
		return Usage{}, fmt.Errorf("ollama circuit breaker open")
	}

	var schemaMap map[string]any
	if err := json.Unmarshal(schema, &schemaMap); err != nil {
		return Usage{}, fmt.Errorf("parse schema: %w", err)
	}

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
		return Usage{}, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.baseURL+"/api/chat", bytes.NewReader(body))
	if err != nil {
		return Usage{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	p.setAuth(req)

	resp, err := p.httpClient.Do(req)
	if err != nil {
		p.circuitBreaker.Failure()
		return Usage{}, fmt.Errorf("ollama chat: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		p.circuitBreaker.Failure()
		return Usage{}, err
	}
	if resp.StatusCode >= 400 {
		p.circuitBreaker.Failure()
		return Usage{}, fmt.Errorf("ollama chat: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
	}

	var chatResp struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
		PromptEvalCount int64 `json:"prompt_eval_count"`
		EvalCount       int64 `json:"eval_count"`
	}
	if err := json.Unmarshal(respBody, &chatResp); err != nil {
		p.circuitBreaker.Failure()
		return Usage{}, fmt.Errorf("parse ollama response: %w", err)
	}
	content := strings.TrimSpace(chatResp.Message.Content)
	if content == "" {
		p.circuitBreaker.Failure()
		return Usage{}, fmt.Errorf("ollama: empty message content")
	}
	if err := json.Unmarshal([]byte(content), respPtr); err != nil {
		if jsonBody := extractJSONObject(content); jsonBody != "" {
			if err2 := json.Unmarshal([]byte(jsonBody), respPtr); err2 == nil {
				p.circuitBreaker.Success()
				return Usage{InputTokens: chatResp.PromptEvalCount, OutputTokens: chatResp.EvalCount}, nil
			}
		}
		p.circuitBreaker.Failure()
		return Usage{}, fmt.Errorf("parse structured output: %w (raw: %s)", err, content)
	}
	p.circuitBreaker.Success()
	usage := Usage{InputTokens: chatResp.PromptEvalCount, OutputTokens: chatResp.EvalCount}
	if usage.InputTokens == 0 && usage.OutputTokens == 0 {
		usage = estimateUsage(systemPrompt, userContent, respPtr)
	}
	return usage, nil
}

func (p *OllamaProvider) setAuth(req *http.Request) {
	if p.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+p.apiKey)
	}
}
