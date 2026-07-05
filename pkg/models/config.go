package models

// AgentConfig holds the agent's runtime configuration.
type AgentConfig struct {
	ScrapeInterval    string            `json:"scrape_interval"`
	PrometheusAddress string            `json:"prometheus_address"`
	LLMProvider       string            `json:"llm_provider"`
	LLMModel          string            `json:"llm_model"`
	Remediation       RemediationConfig `json:"remediation"`
}

// RemediationConfig controls remediation behavior.
type RemediationConfig struct {
	Mode             string   `json:"mode"`
	DryRun           bool     `json:"dry_run"`
	RateLimit        int      `json:"rate_limit"`
	NamespaceDenylist []string `json:"namespace_denylist,omitempty"`
	Allowlist        []string `json:"allowlist,omitempty"`
}
