// Package featureflags provides env-var-based feature gates for v2 capabilities.
// Every flag defaults to false — all v2 features are opt-in.
package featureflags

import "os"

// Flags holds all feature gate booleans for the agent.
// Each field corresponds to a KUBEWISE_FEATURE_<NAME> env var.
// Zero value = all features disabled (v1-compatible).
type Flags struct {
	RuleEngine     bool // deterministic rule engine in correlator
	ContextBuilder bool // promptctx compressor for LLM prompt reduction
	LLMRouter      bool // multi-model routing (cheap vs expensive LLM)
	SemanticCache  bool // semantic incident cache to deduplicate LLM calls
	EventsV2       bool // shared-informer-based event collector (replaces raw watch)
	ToolPlugins    bool // kubectl/helm/argocd tool plugin system
	Observability  bool // Loki + Tempo + Alloy observability stack
}

// Load reads all feature flag env vars and returns a Flags struct.
// Unset vars default to false. An explicitly set empty string is also false.
// The canonical false values are: "", "0", "false", "off", "no" (case-insensitive).
// Everything else is true.
func Load() Flags {
	return Flags{
		RuleEngine:     envBool("KUBEWISE_FEATURE_RULE_ENGINE"),
		ContextBuilder: envBool("KUBEWISE_FEATURE_CONTEXT_BUILDER"),
		LLMRouter:      envBool("KUBEWISE_FEATURE_LLM_ROUTER"),
		SemanticCache:  envBool("KUBEWISE_FEATURE_SEMANTIC_CACHE"),
		EventsV2:       envBool("KUBEWISE_FEATURE_EVENTS_V2"),
		ToolPlugins:    envBool("KUBEWISE_FEATURE_TOOL_PLUGINS"),
		Observability:  envBool("KUBEWISE_FEATURE_OBSERVABILITY"),
	}
}

// Any returns true if at least one feature flag is enabled.
func (f Flags) Any() bool {
	return f.RuleEngine || f.ContextBuilder || f.LLMRouter ||
		f.SemanticCache || f.EventsV2 || f.ToolPlugins || f.Observability
}

// Enabled returns true if the specific flag key is set.
// This is a safety check: callers should compare against the typed field instead,
// but Enabled is available for generic flag-lookup code paths.
func (f Flags) Enabled(key string) bool {
	switch key {
	case "rule_engine":
		return f.RuleEngine
	case "context_builder":
		return f.ContextBuilder
	case "llm_router":
		return f.LLMRouter
	case "semantic_cache":
		return f.SemanticCache
	case "events_v2":
		return f.EventsV2
	case "tool_plugins":
		return f.ToolPlugins
	case "observability":
		return f.Observability
	default:
		return false
	}
}

// envBool parses a KUBEWISE_FEATURE_* env var.
// Returns false when the variable is unset or set to: "", "0", "false", "off", "no".
// All other values are true.
func envBool(key string) bool {
	v, ok := os.LookupEnv(key)
	if !ok {
		return false
	}
	switch v {
	case "", "0", "false", "off", "no":
		return false
	default:
		return true
	}
}
