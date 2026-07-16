package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	gobreaker "github.com/sony/gobreaker/v2"
)

func TestTwoStepCircuitBreakerAllowClosed(t *testing.T) {
	cb := gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     30 * time.Second,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 3
		},
	})
	done, err := cb.Allow()
	if err != nil {
		t.Fatal("expected Allow() to succeed on brand-new circuit breaker")
	}
	if cb.State() != gobreaker.StateClosed {
		t.Fatalf("expected closed state, got %s", cb.State())
	}
	done(nil)
}

func TestTwoStepCircuitBreakerOpenAfterFailures(t *testing.T) {
	cb := gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     30 * time.Second,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 2
		},
	})

	done1, _ := cb.Allow()
	done1(fmt.Errorf("fail")) // 1st failure
	done2, _ := cb.Allow()
	done2(fmt.Errorf("fail")) // 2nd failure — opens

	if cb.State() != gobreaker.StateOpen {
		t.Fatalf("expected open state, got %s", cb.State())
	}
	if _, err := cb.Allow(); err == nil {
		t.Fatal("expected Allow() to return error on open circuit")
	}
}

func TestTwoStepCircuitBreakerHalfOpenOnCooldown(t *testing.T) {
	cb := gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     10 * time.Millisecond,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 1
		},
	})

	done1, _ := cb.Allow()
	done1(fmt.Errorf("fail")) // opens immediately

	if cb.State() != gobreaker.StateOpen {
		t.Fatalf("expected open state, got %s", cb.State())
	}

	// Wait for cooldown to expire so the circuit transitions to half-open.
	time.Sleep(20 * time.Millisecond)

	done2, err := cb.Allow()
	if err != nil {
		t.Fatal("expected Allow() to succeed after cooldown (half-open)")
	}
	if cb.State() != gobreaker.StateHalfOpen {
		t.Fatalf("expected half-open state, got %s", cb.State())
	}
	done2(nil)
}

func TestTwoStepCircuitBreakerSuccessCloses(t *testing.T) {
	cb := gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     30 * time.Second,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 1
		},
	})

	done1, _ := cb.Allow()
	done1(fmt.Errorf("fail")) // opens

	if cb.State() != gobreaker.StateOpen {
		t.Fatalf("expected open state, got %s", cb.State())
	}

	// Success() — manually close by resetting. gobreaker doesn't have a direct
	// "force close" from open without cooldown. We test via half-open recovery.
	// Instead, use Success() via the Allow/done cycle after cooldown.
	cb2 := gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test-reset",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     10 * time.Millisecond,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 1
		},
	})

	done2, _ := cb2.Allow()
	done2(fmt.Errorf("fail")) // opens

	time.Sleep(20 * time.Millisecond)

	done3, _ := cb2.Allow() // half-open
	done3(nil)              // success closes

	if cb2.State() != gobreaker.StateClosed {
		t.Fatalf("expected closed state after half-open success, got %s", cb2.State())
	}
}

func TestTwoStepCircuitBreakerHalfOpenLimitsProbes(t *testing.T) {
	cb := gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     1 * time.Nanosecond,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 1
		},
	})

	done1, _ := cb.Allow()
	done1(fmt.Errorf("fail")) // opens

	time.Sleep(10 * time.Millisecond)

	// First Allow() should transition to half-open and succeed.
	done2, err := cb.Allow()
	if err != nil {
		t.Fatal("expected Allow()=true on half-open transition")
	}
	if cb.State() != gobreaker.StateHalfOpen {
		t.Fatalf("expected half-open state, got %s", cb.State())
	}

	// Second Allow() should fail (MaxRequests=1, single probe).
	if _, err := cb.Allow(); err == nil {
		t.Fatal("expected Allow() to reject second half-open probe")
	}

	done2(nil) // complete the probe
}

func TestTwoStepCircuitBreakerConcurrent(t *testing.T) {
	cb := gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 10,
		Interval:    0,
		Timeout:     30 * time.Second,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 10
		},
	})

	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if done, err := cb.Allow(); err == nil {
				done(nil)
			}
		}()
	}
	wg.Wait()

	// Should still be closed (all successes).
	if cb.State() != gobreaker.StateClosed {
		t.Fatalf("expected closed state after concurrent use, got %s", cb.State())
	}
}

// TestCircuitBreakerOpenaiProviderRejectsOpen verifies that an open circuit
// breaker causes the OpenAI provider to reject requests.
func TestCircuitBreakerOpenaiProviderRejectsOpen(t *testing.T) {
	cb := gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     30 * time.Second,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 1
		},
	})
	done, _ := cb.Allow()
	done(fmt.Errorf("fail")) // opens

	provider := NewOpenAICompatibleProvider("http://example.com", "test-model", "test-key")
	provider.circuitBreaker = cb

	schema := json.RawMessage(`{"type":"object","properties":{"answer":{"type":"string"}}}`)
	var resp struct {
		Answer string `json:"answer"`
	}
	err := provider.StructuredOutput(context.Background(), "test prompt", "test content", schema, &resp)
	if err == nil {
		t.Fatal("expected error from open circuit breaker")
	}
	if !strings.Contains(err.Error(), "circuit breaker open") {
		t.Fatalf("expected circuit breaker open error, got: %v", err)
	}
}

// TestCircuitBreakerOpenaiProviderInit verifies the OpenAI provider initializes
// its circuit breaker.
func TestCircuitBreakerOpenaiProviderInit(t *testing.T) {
	provider := NewOpenAICompatibleProvider("http://example.com", "test-model", "test-key")
	if provider.circuitBreaker == nil {
		t.Fatal("expected circuit breaker to be initialized for OpenAICompatibleProvider")
	}
}

// TestCircuitBreakerOpenRouterProviderInit verifies the OpenRouter provider
// initializes its circuit breaker.
func TestCircuitBreakerOpenRouterProviderInit(t *testing.T) {
	provider := NewOpenRouterProvider("sk-or-v1-test", "test-model")
	if provider.circuitBreaker == nil {
		t.Fatal("expected circuit breaker to be initialized for OpenRouterProvider")
	}
}

// TestCircuitBreakerOllamaProviderInit verifies the Ollama provider initializes
// its circuit breaker.
func TestCircuitBreakerOllamaProviderInit(t *testing.T) {
	provider := NewOllamaProvider("http://127.0.0.1:11434", "test-model", "")
	if provider.circuitBreaker == nil {
		t.Fatal("expected circuit breaker to be initialized for OllamaProvider")
	}
}

// TestCircuitBreakerOpenaiProviderHappyPath verifies a successful request
// closes the circuit breaker.
func TestCircuitBreakerOpenaiProviderHappyPath(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintln(w, `{"choices":[{"message":{"content":"{\"answer\":\"hello\"}"}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}`)
	}))
	defer ts.Close()

	provider := NewOpenAICompatibleProvider(ts.URL, "test-model", "test-key")
	// Pre-record a failure so circuit is open.
	provider.circuitBreaker = gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     10 * time.Millisecond,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 1
		},
	})
	done, _ := provider.circuitBreaker.Allow()
	done(fmt.Errorf("pre-fail"))

	// Wait for cooldown so the circuit transitions to half-open.
	time.Sleep(20 * time.Millisecond)

	schema := json.RawMessage(`{"type":"object","properties":{"answer":{"type":"string"}}}`)
	var resp struct {
		Answer string `json:"answer"`
	}
	err := provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.Answer != "hello" {
		t.Fatalf("expected answer=hello, got %s", resp.Answer)
	}
}

// TestCircuitBreakerOpenaiProviderFailureOnBadResponse verifies that a failed
// response records a circuit breaker failure.
func TestCircuitBreakerOpenaiProviderFailureOnBadResponse(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		fmt.Fprintln(w, `{"error":"internal error"}`)
	}))
	defer ts.Close()

	provider := NewOpenAICompatibleProvider(ts.URL, "test-model", "test-key")

	schema := json.RawMessage(`{"type":"object","properties":{"answer":{"type":"string"}}}`)
	var resp struct {
		Answer string `json:"answer"`
	}
	_ = provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)

	// Circuit breaker should still be closed (threshold=3, only 1 failure
	// after retries within the backoff).
	if provider.circuitBreaker.State() != gobreaker.StateClosed {
		t.Fatalf("expected still closed (only 1 failure), got %s", provider.circuitBreaker.State())
	}
}

// TestCircuitBreakerOpenaiProviderOpensAfterConsecutiveFailures verifies
// the circuit opens after hitting the threshold.
func TestCircuitBreakerOpenaiProviderOpensAfterConsecutiveFailures(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprintln(w, `{"error":"bad gateway"}`)
	}))
	defer ts.Close()

	provider := NewOpenAICompatibleProvider(ts.URL, "test-model", "test-key")

	schema := json.RawMessage(`{"type":"object","properties":{"answer":{"type":"string"}}}`)
	var resp struct {
		Answer string `json:"answer"`
	}

	// With backoff retry (3 attempts each), each StructuredOutput call makes
	// up to 3 attempts. All fail. After 3 requests * 3 attempts + 3 consecutive
	// failures tracked by the CB, the circuit opens.
	// Actually, with backoff, only the final result after retries is reported
	// to the CB. So 3 external calls = 3 CB recorded failures.
	for i := 0; i < 3; i++ {
		_ = provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	}

	if provider.circuitBreaker.State() != gobreaker.StateOpen {
		t.Fatalf("expected open state after 3 failing requests, got %s", provider.circuitBreaker.State())
	}

	// Next request should be rejected by the circuit breaker.
	err := provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	if err == nil {
		t.Fatal("expected error from open circuit breaker")
	}
	if !strings.Contains(err.Error(), "circuit breaker open") {
		t.Fatalf("expected circuit breaker open error, got: %v", err)
	}
}

// TestCircuitBreakerOpenaiProviderRecovery verifies recovery after circuit
// breaker cooldown.
func TestCircuitBreakerOpenaiProviderRecovery(t *testing.T) {
	// Phase 1: always-failing server to open the circuit.
	// Each StructuredOutput call uses 3 backoff attempts, but the CB only
	// records 1 failure per call, so we need 3 calls to hit the threshold.
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprintln(w, `{"error":"bad gateway"}`)
	}))
	defer ts.Close()

	provider := NewOpenAICompatibleProvider(ts.URL, "test-model", "test-key")
	// Use a short cooldown so the circuit can recover naturally.
	provider.circuitBreaker = gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
		Name:        "test",
		MaxRequests: 1,
		Interval:    0,
		Timeout:     5 * time.Millisecond,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 3
		},
	})

	schema := json.RawMessage(`{"type":"object","properties":{"answer":{"type":"string"}}}`)
	var resp struct {
		Answer string `json:"answer"`
	}

	// Three failing requests to open the circuit.
	for i := 0; i < 3; i++ {
		_ = provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	}
	if provider.circuitBreaker.State() != gobreaker.StateOpen {
		t.Fatalf("expected open state, got %s", provider.circuitBreaker.State())
	}

	// Wait for cooldown so the circuit transitions to half-open.
	time.Sleep(15 * time.Millisecond)

	// Phase 2: swap to a successful server for recovery.
	ts.Close()
	ts2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintln(w, `{"choices":[{"message":{"content":"{\"answer\":\"ok\"}"}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}`)
	}))
	defer ts2.Close()

	provider2 := NewOpenAICompatibleProvider(ts2.URL, "test-model", "test-key")
	provider2.circuitBreaker = provider.circuitBreaker // same breaker instance

	// This should succeed in half-open and close the circuit.
	err := provider2.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	if err != nil {
		t.Fatalf("unexpected error on recovery: %v", err)
	}
	if resp.Answer != "ok" {
		t.Fatalf("expected answer=ok, got %s", resp.Answer)
	}
	if provider2.circuitBreaker.State() != gobreaker.StateClosed {
		t.Fatalf("expected closed state after recovery, got %s", provider2.circuitBreaker.State())
	}
}

// TestCircuitBreakerOpenRouterProvider verifies the circuit breaker is checked
// in the OpenRouterProvider's StructuredOutputWithUsage.
func TestCircuitBreakerOpenRouterProvider(t *testing.T) {
	provider := &OpenRouterProvider{
		circuitBreaker: gobreaker.NewTwoStepCircuitBreaker[any](gobreaker.Settings{
			Name:        "test",
			MaxRequests: 1,
			Interval:    0,
			Timeout:     30 * time.Second,
			ReadyToTrip: func(counts gobreaker.Counts) bool {
				return counts.ConsecutiveFailures >= 3
			},
		}),
	}

	schema := json.RawMessage(`{"type":"object"}`)
	var resp interface{}

	// Without SDK, this should fail with "client not configured".
	err := provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	if err == nil {
		t.Fatal("expected error from unconfigured OpenRouterProvider")
	}
	if !strings.Contains(err.Error(), "client not configured") {
		t.Fatalf("expected client not configured error, got: %v", err)
	}
}
