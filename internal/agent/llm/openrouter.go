package llm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	openrouter "github.com/OpenRouterTeam/go-sdk"
	"github.com/OpenRouterTeam/go-sdk/models/components"
	"github.com/OpenRouterTeam/go-sdk/models/operations"
	"github.com/OpenRouterTeam/go-sdk/optionalnullable"
)

// OpenRouterProvider sends structured requests via the OpenRouter API.
type OpenRouterProvider struct {
	mu                         sync.RWMutex
	sessionMu                  sync.Mutex
	consecutiveRetryableErrors atomic.Int64

	sdk            *openrouter.OpenRouter
	apiKey         string
	model          string
	sessionID      string
	circuitBreaker *CircuitBreaker
}

// NewOpenRouterProvider creates an OpenRouter backend.
func NewOpenRouterProvider(apiKey, model string) *OpenRouterProvider {
	apiKey = strings.TrimSpace(apiKey)
	if model == "" {
		model = DefaultModel
	}
	var sdk *openrouter.OpenRouter
	p := &OpenRouterProvider{
		circuitBreaker: newCircuitBreaker(3, 30*time.Second),
	}
	if apiKey != "" {
		baseOpts := []openrouter.SDKOption{
			openrouter.WithSecurity(apiKey),
			openrouter.WithHTTPReferer(appReferer),
			openrouter.WithXTitle(appTitle),
			openrouter.WithTimeout(180 * time.Second),
		}
		// If the model carries a session ID (colon-separated suffix), extract it.
		// Format: "<model>:<session-id>" — enables sticky routing for prompt caching
		// without requiring a separate config field.
		if parts := strings.SplitN(model, ":", 2); len(parts) == 2 && parts[1] != "free" && parts[1] != "" {
			sessionID := parts[1]
			model = parts[0]
			httpClient := &http.Client{
				Timeout:   180 * time.Second,
				Transport: &sessionIDTransport{base: http.DefaultTransport, sessionID: sessionID},
			}
			sdk = openrouter.New(append(baseOpts,
				openrouter.WithClient(httpClient),
			)...)
			return &OpenRouterProvider{sdk: sdk, apiKey: apiKey, model: model, sessionID: sessionID, circuitBreaker: p.circuitBreaker}
		}

		sdk = openrouter.New(baseOpts...)
	}
	p.sdk = sdk
	p.apiKey = apiKey
	p.model = model
	return p
}

// sessionIDTransport injects the x-session-id header for OpenRouter prompt caching.
// This enables sticky routing to the same provider, which improves cache hit rates.
type sessionIDTransport struct {
	base      http.RoundTripper
	sessionID string
}

func (t *sessionIDTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if t.sessionID != "" {
		req.Header.Set("x-session-id", t.sessionID)
	}
	return t.base.RoundTrip(req)
}

// SetSessionID configures sticky routing for prompt caching.
// Call this with an incident ID before making requests for that incident.
func (p *OpenRouterProvider) SetSessionID(sessionID string) {
	p.sessionMu.Lock()
	defer p.sessionMu.Unlock()
	p.sessionID = sessionID
	if p.sdk == nil || sessionID == "" {
		return
	}
	httpClient := &http.Client{
		Timeout: 180 * time.Second,
		Transport: &sessionIDTransport{
			base:      http.DefaultTransport,
			sessionID: sessionID,
		},
	}
	p.sdk = openrouter.New(
		openrouter.WithSecurity(p.apiKey),
		openrouter.WithHTTPReferer(appReferer),
		openrouter.WithXTitle(appTitle),
		openrouter.WithTimeout(180*time.Second),
		openrouter.WithClient(httpClient),
	)
}

// SetModel changes the model used for subsequent requests.
func (p *OpenRouterProvider) SetModel(model string) {
	p.mu.Lock()
	p.model = model
	p.mu.Unlock()
}

// Name returns "openrouter" as the provider identifier.
func (p *OpenRouterProvider) Name() string { return "openrouter" }

// HasAPIKey returns true when an API key is configured.
func (p *OpenRouterProvider) HasAPIKey() bool { return p.apiKey != "" }

// ValidateKey checks that the OpenRouter API key is valid.
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

// StructuredOutput sends a prompt and returns a typed JSON response.
func (p *OpenRouterProvider) StructuredOutput(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) error {
	_, err := p.StructuredOutputWithUsage(ctx, systemPrompt, userContent, schema, respPtr)
	return err
}

// StructuredOutputWithUsage sends a prompt and returns a typed response with token usage.
func (p *OpenRouterProvider) StructuredOutputWithUsage(ctx context.Context, systemPrompt, userContent string, schema json.RawMessage, respPtr interface{}) (Usage, error) {
	p.sessionMu.Lock()
	if p.sdk == nil {
		p.sessionMu.Unlock()
		return Usage{}, fmt.Errorf("openrouter client not configured")
	}
	p.sessionMu.Unlock()

	if !p.circuitBreaker.Allow() {
		return Usage{}, fmt.Errorf("openrouter circuit breaker open")
	}

	var schemaMap map[string]any
	if err := json.Unmarshal(schema, &schemaMap); err != nil {
		return Usage{}, fmt.Errorf("parse schema: %w", err)
	}

	var lastErr error
	p.mu.RLock()
	chain := modelChain(p.model)
	p.mu.RUnlock()
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
		usage, err := p.structuredOutputWithModel(ctx, model, systemPrompt, userContent, responseFormat, respPtr)
		if err == nil {
			p.circuitBreaker.Success()
			p.consecutiveRetryableErrors.Store(0)
			if i > 0 {
				slog.Info("llm: openrouter fallback model succeeded", "model", model)
			}
			return usage, nil
		}
		lastErr = err
		if !isRetryableOpenRouterError(err) {
			p.circuitBreaker.Failure()
			p.consecutiveRetryableErrors.Store(0)
			return Usage{}, err
		}
		if p.consecutiveRetryableErrors.Add(1) >= 5 {
			p.circuitBreaker.Failure()
		}
		if i < len(chain)-1 {
			slog.Warn("llm: openrouter model failed, trying fallback", "model", model, "error", err)
		}
	}
	p.circuitBreaker.Failure()
	return Usage{}, lastErr
}

func (p *OpenRouterProvider) structuredOutputWithModel(
	ctx context.Context,
	model string,
	systemPrompt, userContent string,
	responseFormat components.ResponseFormat,
	respPtr interface{},
) (Usage, error) {
	p.sessionMu.Lock()
	defer p.sessionMu.Unlock()
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
		return Usage{}, fmt.Errorf("chat completion: %w", err)
	}

	content, err := extractMessageContent(res)
	if err != nil {
		return Usage{}, err
	}
	if err := json.Unmarshal([]byte(content), respPtr); err != nil {
		return Usage{}, fmt.Errorf("parse structured output: %w (raw: %s)", err, content)
	}
	return usageFromChatResult(res), nil
}

func usageFromChatResult(res *operations.SendChatCompletionRequestResponse) Usage {
	if res == nil || res.ChatResult == nil {
		return Usage{}
	}
	u := res.ChatResult.GetUsage()
	if u == nil {
		return Usage{}
	}
	usage := Usage{
		InputTokens:  u.GetPromptTokens(),
		OutputTokens: u.GetCompletionTokens(),
	}
	if details, ok := u.GetPromptTokensDetails().Get(); ok && details != nil {
		if cached := details.GetCachedTokens(); cached != nil {
			usage.CachedTokens = *cached
		}
	}
	return usage
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
