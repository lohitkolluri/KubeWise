package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Client sends requests to OpenRouter's chat completions API.
type Client struct {
	apiKey     string
	model      string
	httpClient *http.Client
	endpoint   string
}

// NewClient creates an OpenRouter LLM client.
func NewClient(apiKey, model string) *Client {
	if model == "" {
		model = "openrouter/free"
	}
	return &Client{
		apiKey:   apiKey,
		model:    model,
		endpoint: "https://openrouter.ai/api/v1/chat/completions",
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

// Message represents a chat message in the API request.
type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// chatRequest is the request body for OpenRouter chat completions.
type chatRequest struct {
	Model       string          `json:"model"`
	Messages    []Message       `json:"messages"`
	ResponseFormat *responseFormat `json:"response_format,omitempty"`
	Temperature float64         `json:"temperature"`
	MaxTokens   int             `json:"max_tokens,omitempty"`
}

type responseFormat struct {
	Type       string          `json:"type"`
	JSONSchema *jsonSchema     `json:"json_schema,omitempty"`
}

type jsonSchema struct {
	Name   string          `json:"name"`
	Strict bool            `json:"strict"`
	Schema json.RawMessage `json:"schema"`
}

// chatResponse is the response body from OpenRouter.
type chatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Error *struct {
		Message string `json:"message"`
		Code    string `json:"code,omitempty"`
	} `json:"error,omitempty"`
}

// chatCompletion sends a chat completion request and returns the response content.
func (c *Client) chatCompletion(ctx context.Context, req chatRequest) (string, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+c.apiKey)
	httpReq.Header.Set("HTTP-Referer", "https://github.com/lohitkolluri/KubeWise")
	httpReq.Header.Set("X-Title", "KubeWise")

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return "", fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("openrouter API error (status %d): %s", resp.StatusCode, string(respBody))
	}

	var chatResp chatResponse
	if err := json.Unmarshal(respBody, &chatResp); err != nil {
		return "", fmt.Errorf("unmarshal response: %w", err)
	}

	if chatResp.Error != nil {
		return "", fmt.Errorf("openrouter error: %s (code: %s)", chatResp.Error.Message, chatResp.Error.Code)
	}

	if len(chatResp.Choices) == 0 {
		return "", fmt.Errorf("openrouter: no choices returned")
	}

	return chatResp.Choices[0].Message.Content, nil
}

// StructuredOutput sends a request with a JSON schema constraint and returns the parsed response.
// The respPtr must be a pointer to a struct matching the schema.
func (c *Client) StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error {
	req := chatRequest{
		Model:       c.model,
		Temperature: 0.2,
		MaxTokens:   1000,
		Messages: []Message{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: userContent},
		},
		ResponseFormat: &responseFormat{
			Type: "json_schema",
			JSONSchema: &jsonSchema{
				Name:   "remediation_plan",
				Strict: true,
				Schema: schema,
			},
		},
	}

	content, err := c.chatCompletion(ctx, req)
	if err != nil {
		return fmt.Errorf("chat completion: %w", err)
	}

	if err := json.Unmarshal([]byte(content), respPtr); err != nil {
		return fmt.Errorf("parse structured output: %w (raw: %s)", err, content)
	}

	return nil
}
