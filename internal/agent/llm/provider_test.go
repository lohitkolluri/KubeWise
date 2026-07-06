package llm

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNewProvider_OpenRouter(t *testing.T) {
	p, err := NewProvider(Config{Provider: "openrouter", APIKey: "sk-or-test", Model: "test-model"})
	if err != nil {
		t.Fatal(err)
	}
	if p.Name() != "openrouter" {
		t.Fatalf("expected openrouter, got %s", p.Name())
	}
	if !p.HasAPIKey() {
		t.Fatal("expected API key configured")
	}
}

func TestNewProvider_Ollama(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tags" {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"models":[]}`))
	}))
	defer srv.Close()

	p, err := NewProvider(Config{Provider: "ollama", BaseURL: srv.URL, Model: "llama3.1:8b"})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.ValidateKey(context.Background()); err != nil {
		t.Fatalf("validate: %v", err)
	}
}

func TestOllamaStructuredOutput(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/chat" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"message":{"content":"{\"action\":{\"type\":\"noop\",\"namespace\":\"demo\",\"target\":\"pod-a\",\"rationale\":\"wait\"},\"diagnosis\":{\"root_cause\":\"transient\",\"severity\":\"low\",\"confidence\":0.8,\"evidence\":[\"spike\"]},\"risk\":{\"blast_radius\":\"single_pod\",\"reversible\":true,\"estimated_time_to_resolve\":\"5m\"}}"}}`))
	}))
	defer srv.Close()

	p := NewOllamaProvider(srv.URL, "llama3.1:8b", "")
	schema := json.RawMessage(`{"type":"object","properties":{"action":{"type":"object"}}}`)
	var out map[string]interface{}
	if err := p.StructuredOutput(context.Background(), "sys", "user", schema, &out); err != nil {
		t.Fatalf("structured output: %v", err)
	}
	if out["action"] == nil {
		t.Fatalf("expected action in output: %v", out)
	}
}

func TestNewProvider_Unknown(t *testing.T) {
	_, err := NewProvider(Config{Provider: "unknown"})
	if err == nil {
		t.Fatal("expected error for unknown provider")
	}
}

func TestNewClient_OpenRouterCompat(t *testing.T) {
	c, err := NewOpenRouterClient("sk-or-test", "")
	if err != nil {
		t.Fatal(err)
	}
	if !c.HasAPIKey() {
		t.Fatal("expected key")
	}
}
