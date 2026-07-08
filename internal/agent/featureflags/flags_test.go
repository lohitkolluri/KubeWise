package featureflags

import (
	"testing"
)

func TestLoad_AllOffWhenUnset(t *testing.T) {
	// No KUBEWISE_FEATURE_* vars set → all flags false.
	// This is the v1-compatible default: all v2 features opt-in.
	for _, key := range []string{
		"KUBEWISE_FEATURE_RULE_ENGINE",
		"KUBEWISE_FEATURE_CONTEXT_BUILDER",
		"KUBEWISE_FEATURE_LLM_ROUTER",
		"KUBEWISE_FEATURE_SEMANTIC_CACHE",
		"KUBEWISE_FEATURE_EVENTS_V2",
		"KUBEWISE_FEATURE_TOOL_PLUGINS",
		"KUBEWISE_FEATURE_OBSERVABILITY",
	} {
		t.Setenv(key, "")
	}

	flags := Load()

	if flags.RuleEngine {
		t.Error("RuleEngine should be false when env unset")
	}
	if flags.ContextBuilder {
		t.Error("ContextBuilder should be false when env unset")
	}
	if flags.LLMRouter {
		t.Error("LLMRouter should be false when env unset")
	}
	if flags.SemanticCache {
		t.Error("SemanticCache should be false when env unset")
	}
	if flags.EventsV2 {
		t.Error("EventsV2 should be false when env unset")
	}
	if flags.ToolPlugins {
		t.Error("ToolPlugins should be false when env unset")
	}
	if flags.Observability {
		t.Error("Observability should be false when env unset")
	}
	if flags.Any() {
		t.Error("Any() should be false when all flags are off")
	}
}

func TestLoad_ExplicitTrue(t *testing.T) {
	t.Setenv("KUBEWISE_FEATURE_RULE_ENGINE", "true")
	t.Setenv("KUBEWISE_FEATURE_OBSERVABILITY", "1")

	flags := Load()

	if !flags.RuleEngine {
		t.Error("RuleEngine should be true when set to 'true'")
	}
	if !flags.Observability {
		t.Error("Observability should be true when set to '1'")
	}
	if flags.ContextBuilder {
		t.Error("ContextBuilder should remain false")
	}
	if !flags.Any() {
		t.Error("Any() should be true when at least one flag is on")
	}
}

func TestLoad_ExplicitFalse(t *testing.T) {
	t.Setenv("KUBEWISE_FEATURE_RULE_ENGINE", "false")
	t.Setenv("KUBEWISE_FEATURE_CONTEXT_BUILDER", "0")
	t.Setenv("KUBEWISE_FEATURE_LLM_ROUTER", "off")
	t.Setenv("KUBEWISE_FEATURE_SEMANTIC_CACHE", "no")

	flags := Load()

	if flags.RuleEngine {
		t.Error("RuleEngine should be false when set to 'false'")
	}
	if flags.ContextBuilder {
		t.Error("ContextBuilder should be false when set to '0'")
	}
	if flags.LLMRouter {
		t.Error("LLMRouter should be false when set to 'off'")
	}
	if flags.SemanticCache {
		t.Error("SemanticCache should be false when set to 'no'")
	}
}

func TestAny_FalseWhenAllOff(t *testing.T) {
	f := Flags{}
	if f.Any() {
		t.Error("Any() must be false when all fields are false")
	}
}

func TestAny_TrueWhenOneOn(t *testing.T) {
	f := Flags{RuleEngine: true}
	if !f.Any() {
		t.Error("Any() must be true when at least one flag is on")
	}

	f2 := Flags{Observability: true}
	if !f2.Any() {
		t.Error("Any() must be true when Observability is on")
	}
}

func TestEnabled_ByName(t *testing.T) {
	f := Flags{
		RuleEngine:     true,
		ContextBuilder: true,
	}

	cases := []struct {
		key  string
		want bool
	}{
		{"rule_engine", true},
		{"context_builder", true},
		{"llm_router", false},
		{"semantic_cache", false},
		{"events_v2", false},
		{"tool_plugins", false},
		{"observability", false},
		{"nonexistent", false},
	}

	for _, tc := range cases {
		got := f.Enabled(tc.key)
		if got != tc.want {
			t.Errorf("Enabled(%q) = %v, want %v", tc.key, got, tc.want)
		}
	}
}
