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
	baseURL    string
	model      string
	apiKey     string
	httpClient *http.Client
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
		httpClient: &http.Client{
			Timeout: 120 * time.Second,
		},
	}
}

func (p *OllamaProvider) Name() string { return "ollama" }

// SetModel changes the model used for subsequent requests.
func (p *OllamaProvider) SetModel(model string) { p.model = model }

// HasAPIKey is true when a reachable base URL is configured (Ollama often needs no key).
func (p *OllamaProvider) HasAPIKey() bool { return p.baseURL != "" }

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

func (p *OllamaProvider) StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error {
	var schemaMap map[string]any
	if err := json.Unmarshal(schema, &schemaMap); err != nil {
		return fmt.Errorf("parse schema: %w", err)
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
		return err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.baseURL+"/api/chat", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	p.setAuth(req)

	resp, err := p.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("ollama chat: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode >= 400 {
		return fmt.Errorf("ollama chat: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
	}

	var chatResp struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	}
	if err := json.Unmarshal(respBody, &chatResp); err != nil {
		return fmt.Errorf("parse ollama response: %w", err)
	}
	content := strings.TrimSpace(chatResp.Message.Content)
	if content == "" {
		return fmt.Errorf("ollama: empty message content")
	}
	if err := json.Unmarshal([]byte(content), respPtr); err != nil {
		if jsonBody := extractJSONObject(content); jsonBody != "" {
			if err2 := json.Unmarshal([]byte(jsonBody), respPtr); err2 == nil {
				return nil
			}
		}
		return fmt.Errorf("parse structured output: %w (raw: %s)", err, content)
	}
	return nil
}

func (p *OllamaProvider) setAuth(req *http.Request) {
	if p.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+p.apiKey)
	}
}
