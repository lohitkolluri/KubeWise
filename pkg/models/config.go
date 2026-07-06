package models

// AgentConfig holds the agent's runtime configuration.
type AgentConfig struct {
	ScrapeInterval    string              `json:"scrape_interval" yaml:"scrape_interval"`
	PrometheusAddress string              `json:"prometheus_address" yaml:"prometheus_address"`
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

// RemediationModeView is the runtime remediation mode exposed via the agent API.
type RemediationModeView struct {
	Mode   string `json:"mode"`
	DryRun bool   `json:"dry_run"`
	Live   bool   `json:"live"`
}
