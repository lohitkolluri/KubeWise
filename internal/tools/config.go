// Package tools provides configuration types for tool plugin runtime settings.
//
// ToolConfig defines per-tool allowlist/denylist, timeouts, and environment
// overrides loaded from the agent's config.yaml.
package tools

import "time"

// ToolConfig defines per-tool runtime configuration.
// Loaded from the agent's config.yaml under the `tools` section.
type ToolConfig struct {
	// Allowlist of tool names permitted to run. Empty means all registered tools.
	Allowlist []string `json:"allowlist,omitempty" yaml:"allowlist,omitempty"`

	// Denylist of tool names that are forbidden.
	Denylist []string `json:"denylist,omitempty" yaml:"denylist,omitempty"`

	// DefaultTimeout applied to tools that don't specify their own timeout.
	DefaultTimeout DurationValue `json:"default_timeout,omitempty" yaml:"default_timeout,omitempty"`

	// PerTool overrides for individual tools.
	PerTool map[string]PerToolConfig `json:"per_tool,omitempty" yaml:"per_tool,omitempty"`
}

// PerToolConfig is the per-tool override configuration.
type PerToolConfig struct {
	// Timeout overrides the default timeout for this tool.
	Timeout DurationValue `json:"timeout,omitempty" yaml:"timeout,omitempty"`

	// MaxConcurrent limits concurrent executions. 0 means unlimited.
	MaxConcurrent int `json:"max_concurrent,omitempty" yaml:"max_concurrent,omitempty"`

	// Environment variables to set for this tool.
	Env map[string]string `json:"env,omitempty" yaml:"env,omitempty"`
}

// DurationValue is a reusable duration wrapper for config serialization.
type DurationValue struct {
	time.Duration
}
