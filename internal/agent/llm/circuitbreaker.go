// Package llm provides LLM provider abstractions and circuit breaker logic.
package llm

import (
	"errors"
	"sync"
	"time"

	"github.com/sony/gobreaker"
)

// CircuitState tracks the health of an LLM provider connection.
type CircuitState int32

const (
	stateClosed   CircuitState = iota // normal operation
	stateOpen                         // failing — reject fast
	stateHalfOpen                     // testing — allow one request
)

// CircuitBreaker prevents cascading failures by fast-rejecting requests
// after a threshold of consecutive failures, with automatic recovery.
// Wraps github.com/sony/gobreaker for sliding-window failure counting.
type CircuitBreaker struct {
	inner    *gobreaker.CircuitBreaker
	settings gobreaker.Settings

	mu            sync.Mutex
	halfProbeUsed bool // tracks whether the single half-open probe is consumed
}

// newCircuitBreaker creates a circuit breaker with the given threshold and cooldown.
func newCircuitBreaker(threshold uint32, cooldown time.Duration) *CircuitBreaker {
	if threshold <= 0 {
		threshold = 5
	}
	if cooldown <= 0 {
		cooldown = 30 * time.Second
	}
	settings := gobreaker.Settings{
		Name:        "llm-circuit-breaker",
		MaxRequests: 1, // single probe in half-open
		Interval:    0, // sliding window (not fixed interval)
		Timeout:     cooldown,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= threshold
		},
	}
	return &CircuitBreaker{
		inner:    gobreaker.NewCircuitBreaker(settings),
		settings: settings,
	}
}

// Allow reports whether a request should proceed.
func (cb *CircuitBreaker) Allow() bool {
	s := cb.inner.State()
	switch s {
	case gobreaker.StateClosed:
		return true
	case gobreaker.StateOpen:
		return false
	case gobreaker.StateHalfOpen:
		cb.mu.Lock()
		if cb.halfProbeUsed {
			cb.mu.Unlock()
			return false
		}
		cb.halfProbeUsed = true
		cb.mu.Unlock()
		return true
	}
	return true
}

// Success records a successful call, resetting the failure count and closing the circuit.
func (cb *CircuitBreaker) Success() {
	_, err := cb.inner.Execute(func() (interface{}, error) {
		return nil, nil
	})
	if errors.Is(err, gobreaker.ErrOpenState) {
		// Circuit is open but a success was reported — force-close by recreating.
		cb.mu.Lock()
		cb.inner = gobreaker.NewCircuitBreaker(cb.settings)
		cb.halfProbeUsed = false
		cb.mu.Unlock()
		return
	}
	// Reset half-open probe tracking if we've transitioned back to closed.
	if cb.inner.State() != gobreaker.StateHalfOpen {
		cb.mu.Lock()
		cb.halfProbeUsed = false
		cb.mu.Unlock()
	}
}

// Failure records a failed call and potentially opens the circuit.
func (cb *CircuitBreaker) Failure() {
	_, err := cb.inner.Execute(func() (interface{}, error) {
		return nil, errors.New("circuit breaker failure")
	})
	// If ErrOpenState, the circuit is already open — no need to track half-probe.
	if errors.Is(err, gobreaker.ErrOpenState) {
		cb.mu.Lock()
		cb.halfProbeUsed = false
		cb.mu.Unlock()
		return
	}
	// If the circuit is now open (triggered by ReadyToTrip or half-open→open),
	// reset the half-probe flag so next Allow() after cooldown can retry.
	state := cb.inner.State()
	if state == gobreaker.StateOpen || state == gobreaker.StateClosed {
		cb.mu.Lock()
		cb.halfProbeUsed = false
		cb.mu.Unlock()
	}
}

// State returns the current circuit state.
func (cb *CircuitBreaker) State() CircuitState {
	s := cb.inner.State()
	switch s {
	case gobreaker.StateClosed:
		return stateClosed
	case gobreaker.StateHalfOpen:
		return stateHalfOpen
	default:
		return stateOpen
	}
}

// Reset forcibly closes the circuit and resets the failure count.
func (cb *CircuitBreaker) Reset() {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	cb.inner = gobreaker.NewCircuitBreaker(cb.settings)
	cb.halfProbeUsed = false
}

// String returns a human-readable circuit state name.
func (s CircuitState) String() string {
	switch s {
	case stateClosed:
		return "closed"
	case stateOpen:
		return "open"
	case stateHalfOpen:
		return "half-open"
	}
	return "unknown"
}
