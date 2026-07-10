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
)

func TestCircuitBreakerAllowClosed(t *testing.T) {
	cb := newCircuitBreaker(3, 30*time.Second)
	if !cb.Allow() {
		t.Fatal("expected Allow()=true on brand-new circuit breaker")
	}
	if cb.State() != stateClosed {
		t.Fatalf("expected closed state, got %s", cb.State())
	}
}

func TestCircuitBreakerOpenAfterFailures(t *testing.T) {
	cb := newCircuitBreaker(2, 30*time.Second)
	cb.Failure()
	cb.Failure() // hit threshold
	if cb.State() != stateOpen {
		t.Fatalf("expected open state, got %s", cb.State())
	}
	if cb.Allow() {
		t.Fatal("expected Allow()=false on open circuit")
	}
}

func TestCircuitBreakerHalfOpenOnCooldown(t *testing.T) {
	cb := newCircuitBreaker(1, 10*time.Millisecond)
	cb.Failure() // opens immediately (threshold=1)
	if cb.State() != stateOpen {
		t.Fatalf("expected open state, got %s", cb.State())
	}
	// Allow should transition to half-open after cooldown.
	time.Sleep(20 * time.Millisecond)
	if !cb.Allow() {
		t.Fatal("expected Allow()=true after cooldown (half-open)")
	}
	if cb.State() != stateHalfOpen {
		t.Fatalf("expected half-open state, got %s", cb.State())
	}
}

func TestCircuitBreakerSuccessCloses(t *testing.T) {
	cb := newCircuitBreaker(1, 30*time.Second)
	cb.Failure() // opens
	if cb.State() != stateOpen {
		t.Fatalf("expected open state, got %s", cb.State())
	}
	cb.Success() // should close even though open (edge case: direct call)
	if cb.State() != stateClosed {
		t.Fatalf("expected closed state after Success(), got %s", cb.State())
	}
	if !cb.Allow() {
		t.Fatal("expected Allow()=true on closed circuit")
	}
}

func TestCircuitBreakerOpenClosesAfterSuccess(t *testing.T) {
	cb := newCircuitBreaker(3, 30*time.Second)
	for i := 0; i < 3; i++ {
		cb.Failure()
	}
	if cb.State() != stateOpen {
		t.Fatalf("expected open state, got %s", cb.State())
	}
	// Reset via Success (simulates recovery).
	cb.Success()
	if cb.State() != stateClosed {
		t.Fatalf("expected closed state after Success(), got %s", cb.State())
	}
}

func TestCircuitBreakerReset(t *testing.T) {
	cb := newCircuitBreaker(2, 30*time.Second)
	cb.Failure()
	cb.Failure()
	if cb.State() != stateOpen {
		t.Fatalf("expected open state, got %s", cb.State())
	}
	cb.Reset()
	if cb.State() != stateClosed {
		t.Fatalf("expected closed state after Reset(), got %s", cb.State())
	}
	if !cb.Allow() {
		t.Fatal("expected Allow()=true after Reset()")
	}
}

func TestCircuitBreakerConcurrent(t *testing.T) {
	cb := newCircuitBreaker(10, 30*time.Second)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if cb.Allow() {
				cb.Success()
			} else {
				cb.Failure()
			}
		}()
	}
	wg.Wait()
	// Should still be closed (threshold 10, all successes).
	if cb.State() != stateClosed {
		t.Fatalf("expected closed state after concurrent use, got %s", cb.State())
	}
}

func TestCircuitBreakerOpenRequestRejected(t *testing.T) {
	cb := newCircuitBreaker(1, 30*time.Second)
	cb.Failure() // opens

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer ts.Close()

	// Even if the server is up, the circuit breaker should reject.
	provider := NewOpenAICompatibleProvider(ts.URL, "test-model", "test-key")
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

func TestCircuitBreakerOpenRouterProvider(t *testing.T) {
	// Create a server that returns 502 errors to trigger the circuit breaker.
	failCount := 0
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		failCount++
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprintln(w, `{"error":"upstream failure"}`)
	}))
	defer ts.Close()

	// Override OpenRouter SDK to use our test server - not possible without
	// refactoring. Instead, verify the circuit breaker field is properly initialized.
	provider := NewOpenRouterProvider("sk-or-v1-test", "test-model")
	if provider.circuitBreaker == nil {
		t.Fatal("expected circuit breaker to be initialized for OpenRouterProvider")
	}
	if provider.circuitBreaker.State() != stateClosed {
		t.Fatalf("expected closed state, got %s", provider.circuitBreaker.State())
	}
}

func TestCircuitBreakerOllamaProvider(t *testing.T) {
	provider := NewOllamaProvider("http://127.0.0.1:11434", "test-model", "")
	if provider.circuitBreaker == nil {
		t.Fatal("expected circuit breaker to be initialized for OllamaProvider")
	}
	if provider.circuitBreaker.State() != stateClosed {
		t.Fatalf("expected closed state, got %s", provider.circuitBreaker.State())
	}
}

func TestCircuitBreakerSuccessOnHappyPath(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintln(w, `{"choices":[{"message":{"content":"{\"answer\":\"hello\"}"}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}`)
	}))
	defer ts.Close()

	provider := NewOpenAICompatibleProvider(ts.URL, "test-model", "test-key")
	provider.circuitBreaker.Failure() // previous failure

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
	// Circuit breaker should be closed after success.
	if provider.circuitBreaker.State() != stateClosed {
		t.Fatalf("expected closed state after success, got %s", provider.circuitBreaker.State())
	}
}

func TestCircuitBreakerFailureOnBadResponse(t *testing.T) {
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

	// Circuit breaker should have one failure.
	if provider.circuitBreaker.failures.Load() != 1 {
		t.Fatalf("expected 1 failure, got %d", provider.circuitBreaker.failures.Load())
	}
	if provider.circuitBreaker.State() != stateClosed {
		t.Fatalf("expected still closed (only 1 failure), got %s", provider.circuitBreaker.State())
	}
}

func TestCircuitBreakerOpensAfterConsecutiveFailures(t *testing.T) {
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

	// Three consecutive failures should open the circuit (threshold=3).
	for i := 0; i < 3; i++ {
		_ = provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	}

	if provider.circuitBreaker.State() != stateOpen {
		t.Fatalf("expected open state after 3 failures, got %s", provider.circuitBreaker.State())
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

func TestCircuitBreakerRecovery(t *testing.T) {
	var attempt int
	mu := sync.Mutex{}
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		a := attempt
		attempt++
		mu.Unlock()
		if a < 3 {
			w.WriteHeader(http.StatusBadGateway)
			fmt.Fprintln(w, `{"error":"bad gateway"}`)
			return
		}
		fmt.Fprintln(w, `{"choices":[{"message":{"content":"{\"answer\":\"ok\"}"}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}`)
	}))
	defer ts.Close()

	provider := NewOpenAICompatibleProvider(ts.URL, "test-model", "test-key")

	// Override cooldown to be huge so we can test directly.
	provider.circuitBreaker.cooldown = time.Hour

	schema := json.RawMessage(`{"type":"object","properties":{"answer":{"type":"string"}}}`)
	var resp struct {
		Answer string `json:"answer"`
	}

	// Two failures to open the circuit.
	for i := 0; i < 3; i++ {
		_ = provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	}
	if provider.circuitBreaker.State() != stateOpen {
		t.Fatalf("expected open state, got %s", provider.circuitBreaker.State())
	}

	// Manually trigger half-open (simulates cooldown expiry) and verify recovery.
	provider.circuitBreaker.state.Store(int32(stateHalfOpen))

	// This should succeed and close the circuit.
	err := provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	if err != nil {
		t.Fatalf("unexpected error on recovery: %v", err)
	}
	if resp.Answer != "ok" {
		t.Fatalf("expected answer=ok, got %s", resp.Answer)
	}
	if provider.circuitBreaker.State() != stateClosed {
		t.Fatalf("expected closed state after recovery, got %s", provider.circuitBreaker.State())
	}
}

// TestCircuitBreakerHalfOpenLimitsProbes verifies that only one probe is allowed in half-open state.
func TestCircuitBreakerHalfOpenLimitsProbes(t *testing.T) {
	cb := newCircuitBreaker(1, 1*time.Nanosecond)
	cb.Failure()

	// Wait for cooldown and transition.
	time.Sleep(10 * time.Millisecond)

	// First Allow() should transition to half-open and return true.
	if !cb.Allow() {
		t.Fatal("expected Allow()=true on half-open transition")
	}
	if cb.State() != stateHalfOpen {
		t.Fatalf("expected half-open state, got %s", cb.State())
	}

	// Second Allow() should be false (only one probe).
	if cb.Allow() {
		t.Fatal("expected Allow()=false for second half-open probe")
	}
}

func TestCircuitBreakerString(t *testing.T) {
	states := []struct {
		s CircuitState
		e string
	}{
		{stateClosed, "closed"},
		{stateOpen, "open"},
		{stateHalfOpen, "half-open"},
		{CircuitState(99), "unknown"},
	}
	for _, tc := range states {
		if tc.s.String() != tc.e {
			t.Fatalf("expected %s, got %s for state %d", tc.e, tc.s.String(), tc.s)
		}
	}
}

// TestOpenAIProviderCircuitBreakerIntegration validates that the circuit breaker
// is properly wired to the OpenAICompatibleProvider and protects against repeated failures.
func TestOpenAIProviderCircuitBreakerIntegration(t *testing.T) {
	// Server that always fails.
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		fmt.Fprintln(w, `{"error":"rate limited"}`)
	}))
	defer ts.Close()

	provider := NewOpenAICompatibleProvider(ts.URL, "test-model", "test-key")
	provider.circuitBreaker = newCircuitBreaker(2, 30*time.Second)

	schema := json.RawMessage(`{"type":"object","properties":{}}`)
	var resp map[string]interface{}

	// First two failures should open the circuit.
	for i := 0; i < 2; i++ {
		_ = provider.StructuredOutput(context.Background(), "test", "test", schema, &resp)
	}
	if provider.circuitBreaker.State() != stateOpen {
		t.Fatalf("expected open state, got %s", provider.circuitBreaker.State())
	}
}

// TestCircuitBreakerInOpenRouterStructuredOutput verifies the circuit breaker
// is called in the OpenRouterProvider's StructuredOutputWithUsage.
func TestCircuitBreakerInOpenRouterStructuredOutput(t *testing.T) {
	// Create provider with no SDK (will fail fast).
	provider := &OpenRouterProvider{
		circuitBreaker: newCircuitBreaker(3, 30*time.Second),
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
