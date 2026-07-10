package models

import (
	"encoding/json"
	"time"
)

// AgentConfig holds the agent's runtime configuration.
type AgentConfig struct {
	ScrapeInterval    string              `json:"scrape_interval" yaml:"scrape_interval"`
	PrometheusAddress string              `json:"prometheus_address" yaml:"prometheus_address"`
	LokiURL           string              `json:"loki_url,omitempty" yaml:"loki_url,omitempty"`
	TempoURL          string              `json:"tempo_url,omitempty" yaml:"tempo_url,omitempty"`
	LLMProvider       string              `json:"llm_provider" yaml:"llm_provider"`
	LLMModel          string              `json:"llm_model" yaml:"llm_model"`
	LLMBaseURL        string              `json:"llm_base_url,omitempty" yaml:"llm_base_url,omitempty"`
	Remediation       RemediationConfig   `json:"remediation" yaml:"remediation"`
	Notifications     NotificationsConfig `json:"notifications" yaml:"notifications"`
	WatchNamespaces   []string            `json:"watch_namespaces,omitempty" yaml:"watch_namespaces,omitempty"`
}

// RemediationConfig controls remediation behavior.
type RemediationConfig struct {
	Mode              string   `json:"mode" yaml:"mode"`
	DryRun            bool     `json:"dry_run" yaml:"dry_run"`
	RateLimit         int      `json:"rate_limit" yaml:"rate_limit"`
	NamespaceDenylist []string `json:"namespace_denylist,omitempty" yaml:"namespace_denylist,omitempty"`
	Allowlist         []string `json:"allowlist,omitempty" yaml:"allowlist,omitempty"`
	MinConfidence     float64  `json:"min_confidence,omitempty" yaml:"min_confidence,omitempty"`
	WatchNamespaces   []string `json:"watch_namespaces,omitempty" yaml:"watch_namespaces,omitempty"`
}

// DurationValue is a duration wrapper for JSON/YAML marshaling.
type DurationValue struct {
	time.Duration
}

// MarshalJSON encodes the duration as a JSON string.
func (d DurationValue) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.String())
}

// UnmarshalJSON decodes a JSON string into a DurationValue.
func (d *DurationValue) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	d.Duration = v
	return nil
}

// MarshalYAML encodes the duration as a YAML string.
func (d DurationValue) MarshalYAML() (interface{}, error) {
	return d.String(), nil
}

// UnmarshalYAML decodes a YAML string into a DurationValue.
func (d *DurationValue) UnmarshalYAML(unmarshal func(interface{}) error) error {
	var s string
	if err := unmarshal(&s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	d.Duration = v
	return nil
}

// RemediationModeView is the runtime remediation mode exposed via the agent API.
type RemediationModeView struct {
	Mode   string `json:"mode"`
	DryRun bool   `json:"dry_run"`
	Live   bool   `json:"live"`
}
