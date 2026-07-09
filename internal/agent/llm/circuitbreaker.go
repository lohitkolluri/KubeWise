package llm

import (
	"sync"
	"sync/atomic"
	"time"
)

// circuitState tracks the health of an LLM provider connection.
type circuitState int32

const (
	stateClosed    circuitState = iota // normal operation
	stateOpen                          // failing — reject fast
	stateHalfOpen                      // testing — allow one request
)

// CircuitBreaker prevents cascading failures by fast-rejecting requests
// after a threshold of consecutive failures, with automatic recovery.
type CircuitBreaker struct {
	state     atomic.Int32
	failures  atomic.Int32
	threshold int32
	cooldown  time.Duration
	lastOpen  atomic.Int64 // unix nanos of last state→open transition

	mu   sync.Mutex
	half bool // set when transitioning half-open; reset on request or success
}

// newCircuitBreaker creates a circuit breaker with the given threshold and cooldown.
func newCircuitBreaker(threshold int32, cooldown time.Duration) *CircuitBreaker {
	cb := &CircuitBreaker{
		threshold: threshold,
		cooldown:  cooldown,
	}
	cb.state.Store(int32(stateClosed))
	return cb
}

// Allow reports whether a request should proceed. Returns true for closed or half-open states.
func (cb *CircuitBreaker) Allow() bool {
	s := circuitState(cb.state.Load())
	switch s {
	case stateClosed:
		return true
	case stateOpen:
		// Check if cooldown has elapsed.
		last := time.Unix(0, cb.lastOpen.Load())
		if time.Since(last) >= cb.cooldown {
			cb.mu.Lock()
			// Double-check under lock to avoid race.
			if circuitState(cb.state.Load()) == stateOpen {
				cb.state.Store(int32(stateHalfOpen))
				cb.half = true // the current request consumes the probe
			}
			cb.mu.Unlock()
			return true
		}
		return false
	case stateHalfOpen:
		cb.mu.Lock()
		// Only allow one probe request.
		if cb.half {
			cb.mu.Unlock()
			return false
		}
		cb.half = true
		cb.mu.Unlock()
		return true
	}
	return true
}

// Success records a successful call, resetting the failure count and closing the circuit.
func (cb *CircuitBreaker) Success() {
	cb.failures.Store(0)
	if circuitState(cb.state.Load()) != stateClosed {
		cb.state.Store(int32(stateClosed))
	}
}

// Failure records a failed call and potentially opens the circuit.
func (cb *CircuitBreaker) Failure() {
	f := cb.failures.Add(1)
	if f >= cb.threshold {
		prev := circuitState(cb.state.Swap(int32(stateOpen)))
		if prev != stateOpen {
			cb.lastOpen.Store(time.Now().UnixNano())
		}
	}
}

// State returns the current circuit state.
func (cb *CircuitBreaker) State() circuitState {
	return circuitState(cb.state.Load())
}

// Reset forcibly closes the circuit and resets the failure count.
func (cb *CircuitBreaker) Reset() {
	cb.failures.Store(0)
	cb.state.Store(int32(stateClosed))
}

func (s circuitState) String() string {
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
