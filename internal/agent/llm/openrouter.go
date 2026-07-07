package llm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strings"
	"time"

	openrouter "github.com/OpenRouterTeam/go-sdk"
	"github.com/OpenRouterTeam/go-sdk/models/components"
	"github.com/OpenRouterTeam/go-sdk/models/operations"
	"github.com/OpenRouterTeam/go-sdk/optionalnullable"
)

// OpenRouterProvider sends structured requests via the OpenRouter API.
type OpenRouterProvider struct {
	sdk    *openrouter.OpenRouter
	apiKey string
	model  string
}

// NewOpenRouterProvider creates an OpenRouter backend.
func NewOpenRouterProvider(apiKey, model string) *OpenRouterProvider {
	apiKey = strings.TrimSpace(apiKey)
	if model == "" {
		model = DefaultModel
	}
	var sdk *openrouter.OpenRouter
	if apiKey != "" {
		sdk = openrouter.New(
			openrouter.WithSecurity(apiKey),
			openrouter.WithHTTPReferer(appReferer),
			openrouter.WithXTitle(appTitle),
			openrouter.WithTimeout(180*time.Second),
		)
	}
	return &OpenRouterProvider{sdk: sdk, apiKey: apiKey, model: model}
}

func (p *OpenRouterProvider) Name() string { return "openrouter" }

func (p *OpenRouterProvider) HasAPIKey() bool { return p.apiKey != "" }

func (p *OpenRouterProvider) ValidateKey(ctx context.Context) error {
	if p.apiKey == "" {
		return fmt.Errorf("openrouter API key is empty")
	}
	if p.sdk == nil {
		return fmt.Errorf("openrouter client not initialized")
	}
	if !strings.HasPrefix(p.apiKey, "sk-or-") {
		return fmt.Errorf("openrouter API key should start with sk-or-v1- (create one at https://openrouter.ai/settings/keys)")
	}
	if _, err := p.sdk.APIKeys.GetCurrentKeyMetadata(ctx); err != nil {
		return fmt.Errorf("openrouter auth failed: %w", err)
	}
	return nil
}

func (p *OpenRouterProvider) StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error {
	if p.sdk == nil {
		return fmt.Errorf("openrouter client not configured")
	}

	var schemaMap map[string]any
	if err := json.Unmarshal(schema, &schemaMap); err != nil {
		return fmt.Errorf("parse schema: %w", err)
	}

	var lastErr error
	chain := modelChain(p.model)
	for i, model := range chain {
		strict := !IsFreeModel(model)
		responseFormat := components.CreateResponseFormatJSONSchema(components.ChatFormatJSONSchemaConfig{
			Type: components.ChatFormatJSONSchemaConfigTypeJSONSchema,
			JSONSchema: components.ChatJSONSchemaConfig{
				Name:   "remediation_plan",
				Strict: optionalnullable.From(&strict),
				Schema: schemaMap,
			},
		})
		err := p.structuredOutputWithModel(ctx, model, systemPrompt, userContent, responseFormat, respPtr)
		if err == nil {
			if i > 0 {
				log.Printf("llm: openrouter fallback model %s succeeded", model)
			}
			return nil
		}
		lastErr = err
		if !isRetryableOpenRouterError(err) {
			return err
		}
		if i < len(chain)-1 {
			log.Printf("llm: openrouter model %s failed (%v), trying fallback", model, err)
		}
	}
	return lastErr
}

func (p *OpenRouterProvider) structuredOutputWithModel(
	ctx context.Context,
	model string,
	systemPrompt, userContent string,
	responseFormat components.ResponseFormat,
	respPtr interface{},
) error {
	maxTok := maxTokensForModel(model)
	res, err := p.sdk.Chat.Send(ctx, components.ChatRequest{
		Model:          openrouter.Pointer(model),
		Temperature:    optionalnullable.From(openrouter.Pointer(0.2)),
		MaxTokens:      optionalnullable.From(openrouter.Pointer(maxTok)),
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

func isRetryableOpenRouterError(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.Canceled) {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	msg := strings.ToLower(err.Error())
	for _, sub := range []string{
		"context deadline exceeded",
		"client.timeout",
		"timeout while",
		"429",
		"rate limit",
		"502",
		"503",
		"504",
		"empty message content",
	} {
		if strings.Contains(msg, sub) {
			return true
		}
	}
	return false
}

func maxTokensForModel(model string) int64 {
	if IsFreeModel(model) || strings.Contains(model, "120b") {
		return 2048
	}
	return 1024
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

	reasoningOpt := msg.GetReasoning()
	if reasoningOpt.IsSet() {
		if rp, ok := reasoningOpt.Get(); ok && rp != nil && *rp != "" {
			if jsonBody := extractJSONObject(*rp); jsonBody != "" {
				return jsonBody, nil
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
