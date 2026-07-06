package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	openrouter "github.com/OpenRouterTeam/go-sdk"
	"github.com/OpenRouterTeam/go-sdk/models/components"
	"github.com/OpenRouterTeam/go-sdk/models/operations"
	"github.com/OpenRouterTeam/go-sdk/optionalnullable"
)

const (
	defaultModel = "meta-llama/llama-3.1-8b-instruct"
	appReferer   = "https://github.com/lohitkolluri/KubeWise"
	appTitle     = "KubeWise"
)

// Client sends requests to OpenRouter via the official Go SDK.
// See https://openrouter.ai/docs/client-sdks/go/overview
type Client struct {
	sdk    *openrouter.OpenRouter
	apiKey string
	model  string
}

// NewClient creates an OpenRouter LLM client.
func NewClient(apiKey, model string) *Client {
	apiKey = strings.TrimSpace(apiKey)
	if model == "" {
		model = defaultModel
	}

	var sdk *openrouter.OpenRouter
	if apiKey != "" {
		sdk = openrouter.New(
			openrouter.WithSecurity(apiKey),
			openrouter.WithHTTPReferer(appReferer),
			openrouter.WithXTitle(appTitle),
			openrouter.WithTimeout(60*time.Second),
		)
	}

	return &Client{
		sdk:    sdk,
		apiKey: apiKey,
		model:  model,
	}
}

// ValidateKey checks the API key against OpenRouter's auth endpoint.
func (c *Client) ValidateKey(ctx context.Context) error {
	if c.apiKey == "" {
		return fmt.Errorf("openrouter API key is empty")
	}
	if c.sdk == nil {
		return fmt.Errorf("openrouter client not initialized")
	}
	if !strings.HasPrefix(c.apiKey, "sk-or-") {
		return fmt.Errorf("openrouter API key should start with sk-or-v1- (create one at https://openrouter.ai/settings/keys)")
	}
	if _, err := c.sdk.APIKeys.GetCurrentKeyMetadata(ctx); err != nil {
		return fmt.Errorf("openrouter auth failed: %w", err)
	}
	return nil
}

// HasAPIKey reports whether an API key is configured.
func (c *Client) HasAPIKey() bool {
	return c.apiKey != ""
}

// StructuredOutput sends a request with a JSON schema constraint and returns the parsed response.
// The respPtr must be a pointer to a struct matching the schema.
func (c *Client) StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error {
	if c.sdk == nil {
		return fmt.Errorf("openrouter client not configured")
	}

	var schemaMap map[string]any
	if err := json.Unmarshal(schema, &schemaMap); err != nil {
		return fmt.Errorf("parse schema: %w", err)
	}

	strict := true
	responseFormat := components.CreateResponseFormatJSONSchema(components.ChatFormatJSONSchemaConfig{
		Type: components.ChatFormatJSONSchemaConfigTypeJSONSchema,
		JSONSchema: components.ChatJSONSchemaConfig{
			Name:   "remediation_plan",
			Strict: optionalnullable.From(&strict),
			Schema: schemaMap,
		},
	})

	res, err := c.sdk.Chat.Send(ctx, components.ChatRequest{
		Model:          openrouter.Pointer(c.model),
		Temperature:    optionalnullable.From(openrouter.Pointer(0.2)),
		MaxTokens:      optionalnullable.From(openrouter.Pointer(int64(2000))),
		ResponseFormat: &responseFormat,
		Messages: []components.ChatMessages{
			components.CreateChatMessagesSystem(components.ChatSystemMessage{
				Role:    components.ChatSystemMessageRoleSystem,
				Content: components.CreateChatSystemMessageContentStr(systemPrompt),
			}),
			components.CreateChatMessagesUser(components.ChatUserMessage{
				Role:    components.ChatUserMessageRoleUser,
				Content: components.CreateChatUserMessageContentStr(userContent),
			}),
		},
	}, nil)
	if err != nil {
		return fmt.Errorf("chat completion: %w", err)
	}

	content, err := extractMessageContent(res)
	if err != nil {
		return err
	}

	if err := json.Unmarshal([]byte(content), respPtr); err != nil {
		return fmt.Errorf("parse structured output: %w (raw: %s)", err, content)
	}

	return nil
}

func extractMessageContent(res *operations.SendChatCompletionRequestResponse) (string, error) {
	if res == nil || res.ChatResult == nil {
		return "", fmt.Errorf("openrouter: empty response")
	}
	choices := res.ChatResult.GetChoices()
	if len(choices) == 0 {
		return "", fmt.Errorf("openrouter: no choices returned")
	}

	msg := choices[0].GetMessage()
	contentOpt := msg.GetContent()
	if contentOpt.IsSet() {
		contentPtr, _ := contentOpt.Get()
		if contentPtr != nil && contentPtr.Type == components.ChatAssistantMessageContentTypeStr &&
			contentPtr.Str != nil && *contentPtr.Str != "" {
			return *contentPtr.Str, nil
		}
	}

	// Some models (especially free tier) put JSON in reasoning instead of content.
	reasoningOpt := msg.GetReasoning()
	if reasoningOpt.IsSet() {
		if rp, ok := reasoningOpt.Get(); ok && rp != nil && *rp != "" {
			if json := extractJSONObject(*rp); json != "" {
				return json, nil
			}
		}
	}

	return "", fmt.Errorf("openrouter: empty message content")
}

func extractJSONObject(s string) string {
	start := strings.Index(s, "{")
	end := strings.LastIndex(s, "}")
	if start >= 0 && end > start {
		return s[start : end+1]
	}
	return ""
}
