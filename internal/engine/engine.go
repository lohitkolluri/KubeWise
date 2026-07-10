package engine

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"sync"
)

// RuleEngine evaluates registered rules against input.
type RuleEngine struct {
	mu    sync.RWMutex
	rules []Rule
}

// New creates a rule engine with no rules registered.
func New() *RuleEngine {
	return &RuleEngine{}
}

// RegisterRule adds a rule to the engine.
func (re *RuleEngine) RegisterRule(r Rule) {
	re.mu.Lock()
	defer re.mu.Unlock()
	re.rules = append(re.rules, r)
}

// Evaluate runs all registered rules against the input.
// Returns all matched results sorted by confidence descending.
// Returns empty slice (not nil) when no rules match.
func (re *RuleEngine) Evaluate(ctx context.Context, input Input) ([]RuleResult, error) {
	re.mu.RLock()
	rules := make([]Rule, len(re.rules))
	copy(rules, re.rules)
	re.mu.RUnlock()

	var results []RuleResult
	for _, rule := range rules {
		select {
		case <-ctx.Done():
			return results, ctx.Err()
		default:
		}

		matches, err := rule.Evaluate(ctx, input)
		if err != nil {
			slog.Error("engine: rule error", "rule", rule.Name(), "error", err)
			continue
		}
		results = append(results, matches...)
	}

	sortResults(results)
	return results, nil
}

// RuleCount returns the number of registered rules.
func (re *RuleEngine) RuleCount() int {
	re.mu.RLock()
	defer re.mu.RUnlock()
	return len(re.rules)
}

func sortResults(results []RuleResult) {
	sort.Slice(results, func(i, j int) bool {
		return results[i].Confidence > results[j].Confidence
	})
}

// MustRegister is a convenience helper for registration in init() or main().
// Panics if r.Name() is empty (catches unnamed rules at startup).
func MustRegister(re *RuleEngine, r Rule) {
	if r.Name() == "" {
		panic(fmt.Errorf("engine: cannot register rule with empty name"))
	}
	re.RegisterRule(r)
}
